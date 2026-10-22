"""Opt-in test against a real, temporary Synapse with an SQLite database.

Run: SPACE_AUTOJOIN_INTEGRATION=1 python -m pytest tests/test_integration.py -q
"""
import hashlib
import hmac
import os
import socket
import subprocess
import sys
import time
from urllib.parse import quote

import pytest
import requests
import yaml

pytestmark = pytest.mark.skipif(
    os.environ.get('SPACE_AUTOJOIN_INTEGRATION') != '1',
    reason='Set SPACE_AUTOJOIN_INTEGRATION=1 to launch a real Synapse',
)


def test_real_synapse_space_join(tmp_path):
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    config_path = tmp_path / 'homeserver.yaml'
    subprocess.run([
        sys.executable, '-m', 'synapse.app.homeserver', '--generate-config',
        '--report-stats=no', '--server-name=test', '--config-path', str(config_path),
    ], cwd=tmp_path, check=True, capture_output=True)
    config = yaml.safe_load(config_path.read_text())
    config.update({
        'listeners': [{'port': port, 'type': 'http', 'tls': False,
                       'bind_addresses': ['127.0.0.1'],
                       'resources': [{'names': ['client'], 'compress': False}]}],
        'registration_shared_secret': 'temporary-local-test-only',
        'modules': [{'module': 'synapse_space_autojoin.SpaceAutoJoin',
                     'config': {'inviter': '@autojoin:test'}}],
        'federation_domain_whitelist': [],
        # Only the disposable test server gets higher provisioning limits.
        'rc_room_creation': {'per_second': 100, 'burst_count': 100},
        'rc_message': {'per_second': 100, 'burst_count': 1000},
        'rc_invites': {key: {'per_second': 100, 'burst_count': 100}
                       for key in ('per_room', 'per_user', 'per_issuer')},
    })
    config_path.write_text(yaml.safe_dump(config))
    base = f'http://127.0.0.1:{port}'
    with (tmp_path / 'process.log').open('w') as log:
        process = subprocess.Popen([
            sys.executable, '-m', 'synapse.app.homeserver', '-c', str(config_path)
        ], cwd=tmp_path, stdout=log, stderr=log)
        try:
            for _ in range(300):
                if process.poll() is not None:
                    pytest.fail((tmp_path / 'process.log').read_text())
                try:
                    if requests.get(base + '/_matrix/client/versions', timeout=1).ok:
                        break
                except requests.RequestException:
                    pass
                time.sleep(.1)
            else:
                pytest.fail('Temporary Synapse did not start')

            def register(name, admin=False):
                endpoint = base + '/_synapse/admin/v1/register'
                nonce = requests.get(endpoint, timeout=10).json()['nonce']
                password = 'local-integration-test-password'
                payload = '\0'.join([nonce, name, password, 'admin' if admin else 'notadmin'])
                mac = hmac.new(b'temporary-local-test-only', payload.encode(), hashlib.sha1).hexdigest()
                response = requests.post(endpoint, json={
                    'nonce': nonce, 'username': name, 'password': password,
                    'admin': admin, 'mac': mac,
                }, timeout=20)
                assert response.ok, response.text
                return response.json()['access_token']

            admin, bot, alice = register('owner', True), register('autojoin'), register('alice')

            def api(method, path, token, body=None):
                for attempt in range(6):
                    response = requests.request(method, base + '/_matrix/client/v3/' + path,
                        headers={'Authorization': 'Bearer ' + token}, json=body, timeout=30)
                    if response.status_code != 429 or attempt == 5:
                        break
                    time.sleep(response.json().get('retry_after_ms', 1000) / 1000 + .05)
                assert response.ok, response.text
                return response.json()

            def create(space=False, parent=None):
                body = {'preset': 'private_chat'}
                if space:
                    body['creation_content'] = {'type': 'm.space'}
                if parent:
                    body['initial_state'] = [{'type': 'm.space.parent', 'state_key': parent,
                                              'content': {'via': ['test']}}]
                room = api('POST', 'createRoom', admin, body)['room_id']
                api('POST', f'rooms/{quote(room, safe="")}/invite', admin,
                    {'user_id': '@autojoin:test'})
                api('POST', f'join/{quote(room, safe="")}', bot, {})
                return room

            root = create(True)
            rooms = [create(parent=root) for _ in range(3)]
            sub = create(True, root)
            leaf = create(parent=sub)
            for parent, child in [(root, r) for r in rooms + [sub]] + [(sub, leaf)]:
                api('PUT', f'rooms/{quote(parent, safe="")}/state/m.space.child/{quote(child, safe="")}',
                    admin, {'via': ['test']})
            api('POST', f'rooms/{quote(root, safe="")}/invite', admin, {'user_id': '@alice:test'})
            api('POST', f'join/{quote(root, safe="")}', alice, {})
            expected = {root, *rooms, sub, leaf}
            for _ in range(200):
                joined = set(api('GET', 'joined_rooms', alice)['joined_rooms'])
                if expected <= joined:
                    break
                time.sleep(.1)
            else:
                pytest.fail(f'Missing rooms: {expected - joined}; see logs in {tmp_path}')
            assert len(joined) == 6
        finally:
            process.terminate()
            try:
                process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=5)
