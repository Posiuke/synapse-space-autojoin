from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from synapse.config import ConfigError

import synapse_space_autojoin as module
from synapse_space_autojoin import SpaceAutoJoin, MEMBER, CREATE, CHILD, PARENT

USER = '@alice:test'
BOT = '@autojoin:test'
ROOT = '!space:test'
ROOM = '!room:test'
SUB = '!sub:test'


def event(kind, key='', content=None, room=ROOT, unsigned=None):
    return SimpleNamespace(type=kind, state_key=key, content=content or {},
                           room_id=room, unsigned=unsigned or {})


def room_state(space=False, user=None, bot=True):
    result = {(CREATE, ''): event(CREATE, content={'type': 'm.space'} if space else {})}
    if bot:
        result[MEMBER, BOT] = event(MEMBER, BOT, {'membership': 'join'})
    if user:
        result[MEMBER, USER] = event(MEMBER, USER, {'membership': user})
    return result


def link(states, parent, child, backlink=True):
    states[parent][CHILD, child] = event(CHILD, child, {'via': ['test']})
    if backlink:
        states[child][PARENT, parent] = event(PARENT, parent, {'via': ['test']})


class API:
    def __init__(self, states):
        self.states = states
        self.calls = []
        self.sleep = AsyncMock()
        self.fail = set()

    def is_mine(self, user):
        return user.endswith(':test')

    def register_third_party_rules_callbacks(self, **callbacks):
        self.callback = callbacks['on_new_event']

    async def get_room_state(self, room, event_filter=None):
        return self.states.get(room, {})

    async def update_room_membership(self, sender, target, room_id, new_membership):
        self.calls.append((sender, target, room_id, new_membership))
        if room_id in self.fail:
            raise RuntimeError('Permission denied')
        self.states[room_id][MEMBER, target] = event(
            MEMBER, target, {'membership': new_membership}, room_id
        )


@pytest.fixture
def setup(monkeypatch):
    states = {ROOT: room_state(True, 'join'), ROOM: room_state()}
    link(states, ROOT, ROOM)
    api = API(states)
    tasks = []
    monkeypatch.setattr(module, 'run_as_background_process',
                        lambda name, func, *args: tasks.append((func, args)))
    instance = SpaceAutoJoin({'inviter': BOT}, api)
    return instance, api, states, tasks


async def trigger(setup):
    instance, api, states, tasks = setup
    await instance.on_new_event(event(MEMBER, USER, {'membership': 'join'}), states[ROOT])
    while tasks:
        func, args = tasks.pop(0)
        await func(*args)


async def test_invites_then_joins(setup):
    await trigger(setup)
    instance, api, states, _ = setup
    assert api.calls == [(BOT, USER, ROOM, 'invite'), (USER, USER, ROOM, 'join')]
    assert not instance._pending


@pytest.mark.parametrize('membership', ['join', 'leave', 'ban', 'knock'])
async def test_preserves_membership(setup, membership):
    _, api, states, _ = setup
    states[ROOM][MEMBER, USER] = event(MEMBER, USER, {'membership': membership})
    await trigger(setup)
    assert not api.calls


async def test_accepts_existing_invite(setup):
    _, api, states, _ = setup
    states[ROOM][MEMBER, USER] = event(MEMBER, USER, {'membership': 'invite'})
    await trigger(setup)
    assert api.calls == [(USER, USER, ROOM, 'join')]


async def test_missing_backlink_is_not_access_grant(setup):
    _, api, states, _ = setup
    del states[ROOM][PARENT, ROOT]
    await trigger(setup)
    assert not api.calls


@pytest.mark.parametrize('room', [ROOT, ROOM])
async def test_requires_inviter_membership(setup, room):
    _, api, states, _ = setup
    del states[room][MEMBER, BOT]
    await trigger(setup)
    assert not api.calls


async def test_recursion_and_cycles(setup):
    _, api, states, _ = setup
    states[SUB] = room_state(True)
    link(states, ROOT, SUB)
    link(states, SUB, ROOM)
    link(states, SUB, ROOT)
    await trigger(setup)
    assert len(api.calls) == 4
    assert states[SUB][MEMBER, USER].content['membership'] == 'join'


async def test_non_recursive(setup):
    instance, api, states, _ = setup
    instance.config['recursive'] = False
    states[SUB] = room_state(True)
    leaf = '!leaf:test'
    states[leaf] = room_state()
    link(states, ROOT, SUB)
    link(states, SUB, leaf)
    await trigger(setup)
    assert all(call[2] != leaf for call in api.calls)


async def test_independent_child_failures(setup):
    instance, api, states, _ = setup
    api.fail.add(ROOM)
    states[SUB] = room_state()
    link(states, ROOT, SUB)
    await trigger(setup)
    assert states[SUB][MEMBER, USER].content['membership'] == 'join'
    assert not instance._pending


async def test_room_limit(setup):
    instance, api, states, _ = setup
    instance.config['max_rooms'] = 1
    states[SUB] = room_state()
    link(states, ROOT, SUB)
    await trigger(setup)
    assert len(api.calls) == 2
    assert not instance._pending


async def test_removed_child_is_ignored(setup):
    _, api, states, _ = setup
    states[ROOT][CHILD, ROOM].content = {}
    await trigger(setup)
    assert not api.calls


@pytest.mark.parametrize('user,content,unsigned', [
    ('@remote:elsewhere', {'membership': 'join'}, {}),
    (BOT, {'membership': 'join'}, {}),
    (USER, {'membership': 'invite'}, {}),
    (USER, {'membership': 'join'}, {'prev_content': {'membership': 'join'}}),
])
async def test_ignores_non_join_triggers(setup, user, content, unsigned):
    instance, _, states, tasks = setup
    await instance.on_new_event(event(MEMBER, user, content, unsigned=unsigned), states[ROOT])
    assert tasks == []


async def test_deduplicates_pending_events(setup):
    instance, _, states, tasks = setup
    joined = event(MEMBER, USER, {'membership': 'join'})
    await instance.on_new_event(joined, states[ROOT])
    await instance.on_new_event(joined, states[ROOT])
    assert len(tasks) == 1
    func, args = tasks.pop()
    await func(*args)
    assert not instance._pending


async def test_leaving_space_before_job_aborts(setup):
    instance, api, states, _ = setup
    states[ROOT][MEMBER, USER].content = {'membership': 'leave'}
    await instance._sync(ROOT, USER)
    assert not api.calls


@pytest.mark.parametrize('config', [
    {}, {'inviter': 'bad'}, {'inviter': BOT, 'recursive': 'yes'},
    {'inviter': BOT, 'max_rooms': True}, {'inviter': BOT, 'max_rooms': 0},
    {'inviter': BOT, 'typo': True},
])
def test_invalid_config(config):
    with pytest.raises(ConfigError):
        SpaceAutoJoin.parse_config(config)


def test_remote_inviter_rejected():
    with pytest.raises(ConfigError):
        SpaceAutoJoin({'inviter': '@bot:elsewhere'}, API({}))


async def test_join_race_with_other_autoaccept_module(setup):
    _, api, states, _ = setup
    original = api.update_room_membership

    async def racing(**kwargs):
        await original(**kwargs)
        if kwargs['new_membership'] == 'join':
            raise RuntimeError('Already joined concurrently')

    api.update_room_membership = racing
    await trigger(setup)
    assert states[ROOM][MEMBER, USER].content['membership'] == 'join'


async def test_retries_rate_limits(setup):
    from synapse.api.errors import SynapseError
    instance, api, _, _ = setup
    error = SynapseError(429, 'Too many requests')
    error.retry_after_ms = 2000
    api.update_room_membership = AsyncMock(side_effect=[error, 'event'])
    result = await instance._update_membership(
        sender=BOT, target=USER, room_id=ROOM, new_membership='invite')
    assert result == 'event'
    api.sleep.assert_awaited_once_with(2.0)


async def test_does_not_retry_permission_errors(setup):
    from synapse.api.errors import SynapseError
    instance, api, _, _ = setup
    api.update_room_membership = AsyncMock(side_effect=SynapseError(403, 'Forbidden'))
    with pytest.raises(SynapseError):
        await instance._update_membership(
            sender=BOT, target=USER, room_id=ROOM, new_membership='invite')
    assert api.update_room_membership.await_count == 1
    api.sleep.assert_not_awaited()


async def test_retry_budget_is_finite(setup):
    from synapse.api.errors import SynapseError
    instance, api, _, _ = setup
    api.update_room_membership = AsyncMock(side_effect=SynapseError(503, 'Unavailable'))
    with pytest.raises(SynapseError):
        await instance._update_membership(
            sender=BOT, target=USER, room_id=ROOM, new_membership='invite')
    assert api.update_room_membership.await_count == 4
    assert api.sleep.await_count == 3
