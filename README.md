# synapse-space-autojoin

Ein Synapse-Modul, das lokale Matrix-Benutzer beim **Beitritt zu einem Space**
automatisch in dessen Chaträume aufnimmt. Es gilt für alle verwalteten Spaces,
auch für neu angelegte; eine Liste von Space-IDs ist nicht erforderlich.

```text
Einladung in Space → Benutzer nimmt an → Modul lädt in Raum A/B/C ein
                                      → Modul nimmt diese Einladungen an
```

Bei `recursive: true` werden auch Unter-Spaces und deren Räume berücksichtigt.
Das Modul akzeptiert **nicht** die ursprüngliche Space-Einladung. Dafür kann
separat Synapses `auto_accept_invites` verwendet werden. Für die vom Modul
veranlassten Raumbeitritte ist diese globale Einstellung nicht erforderlich.

## Download

**[Fertiges Python-Modul herunterladen (.whl, v0.1.0)](https://github.com/Posiuke/synapse-space-autojoin/releases/download/v0.1.0/synapse_space_autojoin-0.1.0-py3-none-any.whl)**

Alternativ: [Quellpaket (.tar.gz)](https://github.com/Posiuke/synapse-space-autojoin/releases/download/v0.1.0/synapse_space_autojoin-0.1.0.tar.gz)
oder [alle Releases](https://github.com/Posiuke/synapse-space-autojoin/releases).

Das Wheel ist das fertige installierbare Python-Paket. Nach dem Download im
Synapse-Virtualenv installieren (Pfade an eure Installation anpassen):

```bash
/opt/synapse/venv/bin/python -m pip install ./synapse_space_autojoin-0.1.0-py3-none-any.whl
```

Anschließend die unten beschriebene Modulkonfiguration ergänzen und Synapse neu
starten. Alternativ lässt sich das Wheel direkt per URL installieren, siehe unten.

## Voraussetzungen

- Synapse ab 1.109.0, Python ab 3.10.
- Ein lokaler Dienstaccount, beispielsweise `@space-autojoin:example.org`.
- Dieser Account muss Mitglied jedes verwalteten Spaces **und seiner Zielräume**
  sein. In den Zielräumen braucht er die Berechtigung, Benutzer einzuladen.
  Synapse-Serveradminrechte und ein Access-Token in der Modulkonfiguration sind
  nicht erforderlich.
- Die Space-Raum-Verknüpfung muss in beide Richtungen vorhanden sein:
  `m.space.child` im Space und `m.space.parent` im zugehörigen Raum, jeweils mit
  einem nicht leeren `via`. Das Modul erstellt diese Verknüpfungen nicht selbst.

**„Alle Spaces“ bedeutet:** Jeder Space mit dem Dienstaccount wird bei neuen
Beitritten berücksichtigt. Den Account bei neuen Spaces und Räumen hinzuzufügen,
bleibt ein Einrichtungsschritt. Das Modul umgeht keine Raumrechte. Ein beliebiger
Raum darf nicht allein dadurch zugänglich werden, dass ihn jemand in einem Space
auflistet; deshalb wird auch die Rückverknüpfung geprüft.

## Installation in einer Python-Umgebung

Im selben Virtualenv installieren, in dem Synapse läuft, beispielsweise:

```bash
/opt/synapse/venv/bin/python -m pip install \
  'https://github.com/Posiuke/synapse-space-autojoin/releases/download/v0.1.0/synapse_space_autojoin-0.1.0-py3-none-any.whl'
```

Den Pfad an eure Installation anpassen. Anschließend den folgenden Eintrag zur
**vorhandenen** `modules`-Liste in der `homeserver.yaml` hinzufügen:

```yaml
modules:
  - module: synapse_space_autojoin.SpaceAutoJoin
    config:
      inviter: '@space-autojoin:example.org'
      recursive: true
      max_rooms: 1000
```

Andere Module beibehalten, den `modules`-Schlüssel nicht doppelt anlegen.
Den Dienstaccount zuvor wie einen normalen lokalen Benutzer erstellen und in die
Spaces/Räume aufnehmen. Danach Synapse neu starten, bei systemd beispielsweise:

```bash
sudo systemctl restart matrix-synapse
```

Der konkrete Dienstname hängt von der Installation ab.

## Docker

Ein eigenes Image bauen, damit das Modul auch nach einem Container-Neustart oder
Neuerstellen installiert bleibt. Im folgenden Dockerfile ein zur Installation
passendes, festes Synapse-Image übergeben:

```dockerfile
ARG SYNAPSE_IMAGE
FROM ${SYNAPSE_IMAGE}
COPY . /opt/synapse-space-autojoin
RUN /usr/local/bin/python -m pip install --no-deps /opt/synapse-space-autojoin
```

Zum Beispiel aus einem Checkout dieses Repositorys:

```bash
docker build \
  --build-arg SYNAPSE_IMAGE=matrixdotorg/synapse:v1.163.0 \
  -t synapse-with-space-autojoin:0.1.0 .
```

Das Synapse-Basisimage und dessen Python-Pfad müssen zu eurer Installation passen.
Das eigene Image im Deployment verwenden, die Modulkonfiguration im bestehenden
Config-Volume ergänzen und den Container neu erstellen.

## Konfiguration

| Option | Standard | Bedeutung |
| --- | --- | --- |
| `inviter` | erforderlich | Vollständige Matrix-ID des lokalen Dienstaccounts |
| `recursive` | `true` | Unter-Spaces und ihre Räume ebenfalls verarbeiten |
| `max_rooms` | `1000` | Maximale Anzahl besuchter Kindräume pro Space-Beitritt |

Unbekannte Optionen oder ungültige Werte führen zu einem Konfigurationsfehler.

## Verhalten und Grenzen

- Auslöser ist ein neuer Space-Beitritt, auch nach einer angenommenen Einladung.
  Profiländerungen innerhalb eines Spaces lösen keine erneute Verarbeitung aus.
- Nur Benutzer des eigenen Homeservers werden automatisch aufgenommen.
- Bestehende Raummitgliedschaften bleiben erhalten. Vorhandene Einladungen werden
  angenommen. Bei einer bisherigen Mitgliedschaft `leave`, `ban` oder `knock`
  wird der Raum übersprungen; Austritte, Kicks und Sperren bleiben wirksam.
- Ein normaler Matrix-Invite durch den Dienstaccount geht dem Join voraus.
  Fehlende Einladungsrechte, unbekannte Räume und andere Matrix-Fehler werden
  protokolliert; weitere Räume werden trotzdem verarbeitet.
- Bei Unter-Spaces wird der Dienstaccount auch dort als Mitglied benötigt.
  Zyklen und doppelte Verknüpfungen werden innerhalb eines Durchlaufs abgefangen.
- Bereits vorhandene Space-Mitglieder werden bei Installation nicht nachträglich
  synchronisiert. Später hinzugefügte Räume lösen ebenfalls keine rückwirkende
  Synchronisierung aus. Space-Austritte entfernen keine Raummitgliedschaften.
- Hintergrundaufträge werden nicht dauerhaft gespeichert. Einzelne
  Mitgliedschaftsänderungen werden bei Rate-Limits oder HTTP-5xx-Fehlern bis zu
  dreimal wiederholt; bei Rate-Limits wird `retry_after_ms` berücksichtigt. Neustarts während eines Durchlaufs sowie Rate-Limits oder zeitweise
  Serverfehler können unvollständige Beitritte hinterlassen; Logs prüfen.
- Getestet ist der Betrieb mit einem einzelnen Synapse-Prozess und SQLite.
  Worker-Deployments sind in dieser Version **nicht unterstützt**: Es gibt keine
  prozessübergreifende Auftragskoordination oder garantierte Ereigniszustellung
  an genau einen ausführenden Worker.

## Funktion prüfen

1. Einen Test-Space und drei private Chaträume anlegen, mit beidseitiger Verknüpfung.
2. Den Dienstaccount im Space und in den drei Räumen beitreten lassen.
3. Einladungsrechte des Dienstaccounts in allen drei Räumen prüfen.
4. Einen anderen lokalen Testbenutzer nur in den Space einladen.
5. Die Space-Einladung als Testbenutzer annehmen.
6. Der Testbenutzer sollte kurz danach Mitglied der drei Räume sein.

Fehlersuche: Im Synapse-Log nach `synapse_space_autojoin` suchen. Häufige Ursachen
sind eine fehlende `m.space.parent`-Rückverknüpfung, fehlende Mitgliedschaft des
Dienstaccounts oder fehlende Einladungsrechte. Bei großen Spaces kann
`max_rooms` greifen. Auch Synapses Einladungs-Rate-Limits gelten weiterhin.

## Entwicklung und Tests

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
SPACE_AUTOJOIN_INTEGRATION=1 python -m pytest -q
```

Der Integrationstest startet einen echten, temporären Synapse auf `127.0.0.1`
mit zufälligem Port und SQLite. Er erstellt Dienstaccount, Besitzer und Testnutzer,
einen Space mit drei Chaträumen, einen Unter-Space und dessen Chatraum. Dann
prüft er den automatischen Beitritt des Nutzers zu sämtlichen sechs Räumen/Spaces.
Es wird keine bestehende Matrix-Installation verändert.

## API-Referenzen

Das Modul verwendet öffentliche Synapse-Modul-APIs:

- [on_new_event](https://element-hq.github.io/synapse/latest/modules/third_party_rules_callbacks.html#on_new_event)
- [ModuleApi: get_room_state und update_room_membership](https://github.com/element-hq/synapse/blob/develop/synapse/module_api/__init__.py)
- [Matrix Spaces](https://spec.matrix.org/latest/client-server-api/#spaces)

Lizenz: MIT.
