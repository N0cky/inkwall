# Inkwall

ESP32-S3-Firmware fuer ein Waveshare 13.3" Spectra 6 E-Ink-Display (`EPD_13IN3E`).
Das Geraet verbindet sich per WLAN mit dem Inkwall-Server, prueft per Hash auf
neue Inhalte, laedt bei Bedarf das Bild, zeigt es an, meldet sich zurueck und geht
anschliessend wieder in den Deep Sleep.

Version: `1.3.0` (siehe `FIRMWARE_VERSION` in `Inkwall.ino`).

## Features

- ESP32-S3 + PSRAM-Unterstuetzung fuer grosse 1200x1600-Bilder
- Hash-basierter Polling-Ablauf, damit unveraenderte Inhalte nicht neu gerendert werden
- Kompaktes Bildformat `/current.epd` (4 bpp, 960 KB) direkt ins Display; 24-Bit-BMP als Fallback
- **Update ueber den Server (OTA)** mit MD5-Pruefung und automatischem Rollback
- Rueckmeldung mit Gesundheitsdaten (RSSI, Firmware, Boot-Zaehler, PSRAM, Zeiten, letzter Fehler)
- Serielles Log des Zyklus geht mit der Rueckmeldung an den Server (System-Seite → "Geraet")
- **Offline-Hinweis**: nach drei Zyklen ohne Serverkontakt (15 min) zeichnet das Geraet das letzte
  Bild aus dem Flash neu, mit schwarzem Balken "Keine Verbindung zum Server seit HH:MM" bzw.
  "Kein WLAN seit HH:MM". Kommt der Server zurueck, erscheint automatisch das normale Bild.
- Eingebettete Bitmap-Schrift (`font_data.h`, erzeugt von `esp32/tools/make_font.py`) fuer Statusanzeigen
- Uhr wird bei jedem Kontakt vom Server gestellt (`epoch`, `tz_offset_sec` aus `/meta.json`)
- Panelreinigung auf Zuruf des Servers (`clean_due`): einmal Schwarz, einmal Weiss, danach das Bild neu
- "Nicht eingerichtet"-Seite, wenn WLAN oder Server-Adresse fehlen
- Fallback-Sleep bei WLAN-, Server- oder Metadatenfehlern, Retry-Logik fuer alle Requests
- Saubere lokale Konfiguration ueber `config.example.h` und `config.private.h`

## Projektdateien

- `Inkwall.ino`: Hauptlogik fuer WLAN, HTTP, OTA, Bild-Download, Offline-Hinweis, ACK und Deep Sleep
- `epd.cpp` / `epd.h`: Low-Level-Treiber fuer das 13.3" Spectra-6-Display
- `text_draw.cpp` / `text_draw.h`: Text und Rechtecke im 4-bpp-Framebuffer (Statusanzeigen)
- `font_data.h`: zwei Bitmap-Schriften (44 px und 26 px), erzeugt mit `python esp32/tools/make_font.py`
- `config.example.h`: Sichere Standardwerte und Platzhalter
- `config.private.h`: Lokale, nicht weiterzugebende Overrides (nicht im Repo)
- `config.h`: Zentraler Einstiegspunkt, der private Overrides und Defaults zusammenfuehrt

## Konfiguration

Die Firmware bindet immer `config.h` ein. Diese Datei laedt zuerst `config.private.h`
und danach `config.example.h`, lokale Werte ueberschreiben also die Standardwerte.

```cpp
#define WIFI_SSID       "MeinWLAN"
#define WIFI_PASSWORD   "MeinPasswort"
#define SERVER_BASE_URL "http://192.168.178.47:8787"
#define DEVICE_ID       "esp32-eink-01"
```

Schalter in `config.example.h` (per `config.private.h` ueberschreibbar):

| Schalter | Standard | Bedeutung |
|---|---|---|
| `FIRMWARE_OTA_ENABLED` | `true` | Update einspielen, wenn der Server eine andere Version bereitstellt |
| `DEVICE_LOG_ENABLED` | `true` | Logzeilen des Zyklus mit dem ACK an den Server schicken |
| `PREFER_COMPACT_IMAGE` | `true` | `/current.epd` nutzen, wenn der Server es anbietet (sonst BMP) |
| `HTTP_SHORT_TIMEOUT_MS` / `ACK_TIMEOUT_MS` | `10000` / `20000` | Timeout fuer meta.json und hash / fuer die Rueckmeldung |
| `MIN_SLEEP_SEC` / `MAX_SLEEP_SEC` | `10` / `21600` | Grenzen jeder Schlafzeit |
| `IMAGE_RETRY_SEC` / `MAX_IMAGE_TRIES` | `120` / `3` | Neuer Versuch nach einem gescheiterten Bild, wie oft je Bild |
| `ACK_ENABLED` | `true` | Rueckmeldung nach jedem Zyklus |

## Ablauf je Wake-Zyklus

1. WLAN verbinden
2. `GET /meta.json` → Hash, `next_wake_sec`, `epd_url`, `firmware_version`/`firmware_md5`/`firmware_url`
   (Fallback fuer alte Server: `GET /hash`)
3. Laufende Firmware als gueltig markieren (Rollback-Schutz nach einem Update)
4. Bietet der Server eine andere Firmware-Version an → `POST /ack` mit `result=ota`, dann
   `/firmware.bin` in die zweite App-Partition laden (MD5 aus dem Header `x-MD5`), Neustart
5. Hash unveraendert → `POST /ack` mit `result=unchanged`, schlafen
6. `GET /current.epd` (4 bpp, direkt ins Display) oder `GET /current.bmp` (24 Bit, konvertieren)
7. Bild anzeigen, `POST /ack` mit `result=updated` (oder `error`), Gesundheitsdaten und Log
8. Deep Sleep fuer `next_wake_sec`

## Update ueber den Server (OTA)

1. Firmware bauen (siehe unten), die `.bin` auf der Geraet-Seite der Weboberflaeche hochladen.
   Der Server liest die Version aus dem Marker `INKWALL_FW_VERSION=…` in der Datei.
2. Beim naechsten Aufwachen vergleicht das Geraet die Version aus `/meta.json` mit seiner
   eigenen. Ab 1.3.0 spielt es nur eine **neuere** Version ein (ein per USB aufgespieltes
   1.4.0 faellt nicht auf ein bereitgestelltes 1.3.0 zurueck). Aeltere Versionen oder
   Test-Builds (`1.3.0-selftest`) nur, wenn beim Hochladen "Auch einspielen, wenn die
   Version nicht neuer ist" angehakt war (`firmware_force` in `/meta.json`).
3. Rollback-Schutz (in der Firmware, nicht im Bootloader – der Arduino-Bootloader markiert
   neue Firmware sofort als gueltig): Vor dem Einspielen merkt sich das Geraet im Flash (NVS)
   Zielversion, MD5, Zielpartition und "Bestaetigung ausstehend". Die neue Firmware zaehlt
   ihre Starts und zeichnet das Bild zur Probe neu; bestaetigt ist sie erst nach einem
   **vollstaendigen Zyklus** – Bild angezeigt (oder unveraendert), Rueckmeldung angekommen,
   Panel hat geantwortet. Eine Firmware, die beim Bildaufbau haengt oder abstuerzt, wird so
   erkannt. Drei Starts ohne vollstaendigen Zyklus → `Update.rollBack()` auf die alte
   Partition. Die alte Firmware meldet den Rollback als Fehler (Geraet-Seite) und laedt
   genau diese Datei (Version + MD5) nicht noch einmal. Laeuft nach einem Neustart gar nicht
   die Zielpartition (Strom weg mitten im Update), ist nichts zu pruefen.
4. Scheitert das Einspielen selbst (MD5 falsch, WLAN bricht ab), versucht es das Geraet
   hoechstens dreimal je Datei; nach 24 Stunden oder mit einer anderen Datei wieder.

Selbsttest des Rollbacks (absichtlich kaputte Firmware, die den Server nie erreicht):

```powershell
arduino-cli compile --fqbn "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB" --build-property "compiler.cpp.extra_flags=-DOTA_SELFTEST_FAIL" --build-path build-selftest .
```

Die Datei meldet sich als `<version>-selftest` und ist damit nicht neuer: beim Hochladen
"Auch einspielen, wenn die Version nicht neuer ist" anhaken. Nach dem Update und drei Starts
muss das Geraet mit der vorherigen Version und der Fehlermeldung "zurueckgerollt" zurueckkommen.
Getestet am 3. September 2026 mit 1.1.5.

Voraussetzungen: Partitionsschema mit zwei App-Slots (`16M Flash (3MB APP/9.9MB FATFS)`
hat `app0` und `app1` mit je 3 MB) und einmalig ein Flash per USB mit einer Firmware ab 1.1.0.

Hinweis zum USB-Port: Mit den Standard-Board-Einstellungen gehen die seriellen Ausgaben auf
UART0, nicht auf den USB-Port des ESP32-S3. Fuer Logs ohne Kabel ist das Geraetelog auf der
System-Seite gedacht; wer am USB-Port mitlesen will, setzt "USB CDC On Boot: Enabled".

## Rueckmeldung (`POST /ack`)

```json
{
  "device_id": "esp32-eink-01",
  "hash": "…",
  "result": "updated | unchanged | error | ota",
  "fw_version": "1.1.0",
  "rssi": -68, "ip": "192.168.178.50",
  "boot_count": 123, "free_psram_kb": 7890,
  "cycle_ms": 6200, "download_ms": 900, "refresh_ms": 4100,
  "image_format": "epd4",
  "wake_reason": "timer",
  "reset_reason": "deepsleep | poweron | panic | task_wdt | brownout | restart | …",
  "meta_ms": 5200,
  "error": "…nur bei result=error…",
  "log": ["== Inkwall 1.3.0 Boot #123 (Wecken: timer, Start: deepsleep) ==", "…"]
}
```

## Offline-Verhalten

| Zustand | Display |
|---|---|
| 1–2 Zyklen ohne Serverkontakt | unveraendert (kurze Aussetzer bleiben unsichtbar) |
| ab dem 3. Zyklus (15 min) | letztes Bild aus dem Flash + schwarzer Balken "Keine Verbindung zum Server seit HH:MM" |
| WLAN weg | dasselbe mit "Kein WLAN seit HH:MM" |
| kein gespeichertes Bild | weisse Seite mit dem Balken |
| Server wieder da | normales Bild, `offline_s` im ACK erzeugt ein Ereignis auf der System-Seite |

Die Uhrzeit "seit HH:MM" stammt aus der Uhr des Geraets, die bei jedem Serverkontakt gestellt
wird; nach einer Stromtrennung ohne Serverkontakt fehlt sie. Auf der Geraet-Seite laesst sich
der Balken mit "Auf dem Display ausprobieren" einmal zur Probe anzeigen.

## Erwartetes Serververhalten

- `GET /meta.json?sleep=from_meta` liefert mindestens `{"hash": "<32 Hex-Zeichen>", "next_wake_sec": <zahl>}`;
  optional `format`, `epd_url`, `epd_size`, `firmware_version`, `firmware_md5`, `firmware_url`,
  `firmware_force`, `epoch`, `tz_offset_sec`, `clean_due`, `show_offline_test` und
  `sleep_from: "meta"` – dann zieht das Geraet die Zeit seit `/meta.json` (Download,
  Bildaufbau, ACK) selbst von `next_wake_sec` ab, und der Server rechnet nur die Zeit bis
  `/meta.json` ein (`meta_ms` aus dem letzten ACK). Ein langer Zyklus mit Bildaufbau
  verschiebt so das naechste Aufwachen nicht mehr.
- `GET /current.epd` liefert das kompakte Bild (Header `PLX6`, 16 Bytes, danach 600 Bytes je Zeile)
- `GET /current.bmp` liefert ein unkomprimiertes 24-Bit-BMP in `1200x1600`
- `GET /firmware.bin` liefert die Firmware mit Header `x-MD5`
- `POST /ack` akzeptiert das JSON oben

## Build

```powershell
arduino-cli compile --fqbn "esp32:esp32:esp32s3:PSRAM=opi,FlashSize=16M,PartitionScheme=app3M_fat9M_16MB" --export-binaries .
```

Die fertige Datei liegt danach unter `build/esp32.esp32.esp32s3/Inkwall.ino.bin` und kann
so auf der Geraet-Seite hochgeladen werden. Getestet mit `arduino-cli 1.4.1` und `esp32:esp32 3.3.8`.

## Arduino IDE

Empfohlene Einstellungen:

- Board: `ESP32S3 Dev Module`
- PSRAM: `OPI PSRAM`
- Flash Size: `16MB`
- Partition Scheme: `16M Flash (3MB APP/9.9MB FATFS)`

"Sketch → Kompilierte Binaerdatei exportieren" legt die `.bin` im Sketch-Ordner ab.

## Fehlerverhalten

- WLAN nicht erreichbar: Fallback-Sleep, der Fehler wird im RTC-Speicher gemerkt und mit dem
  naechsten erfolgreichen ACK gemeldet
- `/meta.json` und `/hash` nicht erreichbar (oder die Antwort ist kein Hash, etwa ein Captive
  Portal): Fallback-Sleep. Kurze Anfragen haben ein eigenes Timeout (`HTTP_SHORT_TIMEOUT_MS`, 10 s)
- `next_wake_sec` fehlt oder ist ungueltig: Wake-Intervall faellt auf `POLL_FALLBACK_SEC` zurueck;
  jede Schlafzeit wird auf `MIN_SLEEP_SEC`..`MAX_SLEEP_SEC` (10 s .. 6 h) begrenzt
- Server liefert PNG (`OUTPUT_FORMAT=png`) oder rendert fuer ein anderes Panel (`epd_size` passt
  nicht): klare Fehlermeldung im ACK statt eines vergeblichen Downloads
- Bild laesst sich nicht laden oder anzeigen: neuer Versuch nach `IMAGE_RETRY_SEC` (120 s),
  hoechstens `MAX_IMAGE_TRIES` (3) je Bild, danach erst wieder bei einem neuen Bild. Wird das
  kompakte Bild angeboten, gibt es keinen BMP-Ersatz mehr (5,8 MB fuer dasselbe Ergebnis)
- Panel antwortet nicht (BUSY bleibt aktiv): `result=error` mit "Panel antwortet nicht" statt
  "updated"; eine frisch eingespielte Firmware wird dann nicht bestaetigt
- Absturz, Watchdog, Unterspannung: `reset_reason` im ACK, der Server meldet es als Ereignis.
  Hash, Fehlerzaehler und "seit HH:MM" ueberleben solche Neustarts (RTC_NOINIT mit Pruefsumme);
  nach einer Stromtrennung merkt sich das Geraet in `/shown.txt`, welches Bild noch auf dem
  Panel steht, und zeichnet es nicht ohne Grund neu
- Das letzte Bild fuer den Offline-Hinweis wird erst in `/last.tmp` geschrieben und dann
  umbenannt – Strom weg beim Schreiben laesst das alte stehen
- OTA fehlgeschlagen: Meldung im Log, der normale Zyklus laeuft mit der alten Firmware weiter
- `POST /ack` schlaegt fehl: Warnung im Log, aber kein Abbruch des Zyklus
