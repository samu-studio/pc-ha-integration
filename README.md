# pc-ha-bridge

Steuert einen Linux-PC mit KDE Plasma 6 aus Home Assistant – per MQTT mit Auto-Discovery.
Alle Entities erscheinen in HA automatisch unter einem Gerät (Name = Hostname).

| Funktion | HA-Entities | Technik |
|---|---|---|
| Bildschirmhelligkeit pro Monitor | `number` (Slider) je Monitor | KDE `org.kde.ScreenBrightness` (D-Bus, DDC/CI) |
| Lautstärke / Stumm | `number`, `switch` | `wpctl` / `pactl subscribe` |
| Audioausgabe (alle Ausgabegeräte, Standardgerät umschalten) | `select` | `pactl` |
| Medien | Sensoren Status/Titel/Interpret/Player, Buttons Play/Pause/Weiter/Zurück | `playerctl` (MPRIS) |
| Eigene Befehle | `button` je Befehl | Shell-Befehle aus der Config |
| Systemwerte | CPU-/RAM-Auslastung, CPU-/GPU-Temperatur, GPU-Last | `psutil`, sysfs hwmon, NVML |
| Online-Status | alle Entities werden „nicht verfügbar“, wenn der PC aus ist | MQTT Last Will |

Fehlt ein Werkzeug (z. B. kein Plasma, kein `playerctl`, keine NVIDIA-GPU), wird nur die
betroffene Funktion deaktiviert – der Rest läuft weiter.

## Voraussetzungen

- Home Assistant mit MQTT-Broker (z. B. Mosquitto-Add-on) und eingerichteter MQTT-Integration
- Linux mit systemd und PipeWire, KDE Plasma ≥ 6.2 (für die Helligkeit), Python ≥ 3.12
- Pakete: `wireplumber` (`wpctl`), `pulseaudio-utils` (`pactl`), `playerctl`
- Für externe Monitore: DDC/CI im Monitor-Menü aktiviert. Nur Monitore, deren Helligkeit sich
  auch über den Plasma-Regler ändern lässt, erscheinen in HA.

## Installation

```bash
git clone https://github.com/samu-studio/pc-ha-integration.git pc-ha-bridge && cd pc-ha-bridge
./install.sh
```

Legt `.venv` an, kopiert `config.example.toml` nach `~/.config/pc-ha-bridge/config.toml`,
fragt nach dem MQTT-Broker (Host, Port, Benutzer, Passwort) und aktiviert den
systemd-User-Service `pc-ha-bridge` (startet mit der Desktop-Sitzung).

MQTT-Broker später ändern (interaktiv bzw. ohne Rückfragen, z. B. per Skript):

```bash
./install.sh --mqtt
```

```bash
PC_HA_MQTT_PASSWORD='geheim' ./install.sh --mqtt-host 192.168.1.10 --mqtt-port 1883 --mqtt-user pc
```

Ohne Terminal und ohne `--mqtt-host` bleibt die Beispiel-Config unverändert; dann `[mqtt]` in
der Config von Hand anpassen und `systemctl --user restart pc-ha-bridge` ausführen.

Aktualisieren: `git pull` und erneut `./install.sh` ausführen. Das ersetzt Paket und
Service-Datei und startet den Service neu; die Config bleibt unverändert.

Logs ansehen:

```bash
journalctl --user -u pc-ha-bridge -f
```

Deinstallieren (Config und `.venv` bleiben erhalten, mit `--purge` werden sie mitgelöscht):

```bash
./uninstall.sh
```

Die Entitäten bleiben danach in Home Assistant als „nicht verfügbar“ stehen; bei Bedarf das
Gerät in HA unter MQTT löschen.

## Stabilität und Ressourcen

Der Service ist auf Dauerbetrieb ausgelegt und belastet das System kaum (gemessen ~60–70 MB RAM inkl. Hilfsprozessen,
~0,2 % CPU; Änderungen werden ereignisgesteuert statt per Polling erkannt).
Zusätzlich begrenzt die systemd-Unit ihn hart (`MemoryMax=200M`, `CPUQuota=20%`, `TasksMax=64`,
`Nice=10`, idle-I/O) und startet ihn über einen Watchdog neu, falls er hängt. Ein Fehler im
Service kann den Rechner dadurch nicht ausbremsen oder zum Absturz bringen.
Eigene Befehle laufen als eigene systemd-Units und unterliegen diesen Limits nicht.

## Lizenz

MIT – siehe [LICENSE](LICENSE).
