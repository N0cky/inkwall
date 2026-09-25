#pragma once

// ════════════════════════════════════════════════════════════════════════════
//  Inkwall – Beispielkonfiguration
//
//  Diese Datei ist die sichere Vorlage fuer gemeinsam genutzte Defaults.
//  Lokale Zugangsdaten und geraetespezifische Werte kommen in config.private.h.
// ════════════════════════════════════════════════════════════════════════════


// ── WiFi ─────────────────────────────────────────────────────────────────────
#ifndef WIFI_SSID
#define WIFI_SSID        "WIFI_SSID_HERE"
#endif

#ifndef WIFI_PASSWORD
#define WIFI_PASSWORD    "WIFI_PASSWORD_HERE"
#endif

#ifndef WIFI_TIMEOUT_MS
#define WIFI_TIMEOUT_MS  20000          // max. Wartezeit auf Verbindung (ms)
#endif


// ── Python-Server ─────────────────────────────────────────────────────────────
#ifndef SERVER_BASE_URL
#define SERVER_BASE_URL  "http://192.168.178.47:8787"
#endif

#ifndef DEVICE_ID
#define DEVICE_ID        "esp32-eink-01"
#endif


// ── Display SPI-Pins ──────────────────────────────────────────────────────────
// Originalbeispiel verwendet: SCK=13, MOSI=14, CS_M=15, CS_S=2, RST=26, DC=27, BUSY=25, PWR=33
// Hier auf ESP32-S3 anpassen:
#ifndef PIN_EPD_MOSI
#define PIN_EPD_MOSI    14   // SPI MOSI (DIN)
#endif

#ifndef PIN_EPD_SCLK
#define PIN_EPD_SCLK    13   // SPI Clock
#endif

#ifndef PIN_EPD_CS_M
#define PIN_EPD_CS_M    15   // Chip Select Master (linke Displayhaelfte)
#endif

#ifndef PIN_EPD_CS_S
#define PIN_EPD_CS_S     2   // Chip Select Slave  (rechte Displayhaelfte)
#endif

#ifndef PIN_EPD_RST
#define PIN_EPD_RST     11   // Reset (aktiv LOW)
#endif

#ifndef PIN_EPD_DC
#define PIN_EPD_DC      12   // DC-Pin (beim 13in3e im Treiber nicht genutzt, trotzdem verdrahten)
#endif

#ifndef PIN_EPD_BUSY
#define PIN_EPD_BUSY    10   // Busy: LOW = beschaeftigt, HIGH = bereit
#endif

#ifndef PIN_EPD_PWR
#define PIN_EPD_PWR      9   // Versorgungsspannung Display (HIGH = ein)
#endif


// ── Display ───────────────────────────────────────────────────────────────────
// Das Display ist nativ 1200x1600 (Hochformat/Portrait).
// Server-Einstellung: RENDER_WIDTH=1200, RENDER_HEIGHT=1600, DISPLAY_ROTATION=0
#ifndef EPD_WIDTH
#define EPD_WIDTH       1200
#endif

#ifndef EPD_HEIGHT
#define EPD_HEIGHT      1600
#endif

#ifndef EPD_SPI_FREQ
#define EPD_SPI_FREQ    10000000UL  // 10 MHz; bei Problemen → 4000000
#endif


// ── Verhalten ─────────────────────────────────────────────────────────────────
#ifndef POLL_FALLBACK_SEC
#define POLL_FALLBACK_SEC  120
#endif

#ifndef HTTP_TIMEOUT_MS
#define HTTP_TIMEOUT_MS  60000   // BMP-Download: 1200x1600x3 ≈ 5.76 MB
#endif

// Kurze Anfragen (meta.json, hash, Rückmeldung): ein Server, der die Verbindung
// annimmt, aber nicht antwortet, hielt das Gerät sonst minutenlang wach
#ifndef HTTP_SHORT_TIMEOUT_MS
#define HTTP_SHORT_TIMEOUT_MS  10000
#endif

#ifndef HTTP_CONNECT_TIMEOUT_MS
#define HTTP_CONNECT_TIMEOUT_MS  5000
#endif

// Die Rückmeldung darf etwas länger dauern: an ihr hängt die Bestätigung einer
// neuen Firmware, und ältere Server verschickten dabei noch Benachrichtigungen
#ifndef ACK_TIMEOUT_MS
#define ACK_TIMEOUT_MS  20000
#endif

// Schlafzeit-Grenzen: schützt vor Serverfehlern (Sekundentakt oder tagelanger Schlaf)
#ifndef MIN_SLEEP_SEC
#define MIN_SLEEP_SEC  10
#endif

#ifndef MAX_SLEEP_SEC
#define MAX_SLEEP_SEC  21600     // 6 h
#endif

// Bild ließ sich nicht laden oder anzeigen: nach so vielen Sekunden neu versuchen
// (statt einen ganzen Takt das alte Bild zu zeigen), höchstens MAX_IMAGE_TRIES-mal
// je Bild – danach erst wieder, wenn der Server ein anderes Bild hat
#ifndef IMAGE_RETRY_SEC
#define IMAGE_RETRY_SEC  120
#endif

#ifndef MAX_IMAGE_TRIES
#define MAX_IMAGE_TRIES  3
#endif

#ifndef HTTP_RETRY_COUNT
#define HTTP_RETRY_COUNT  2
#endif

#ifndef HTTP_RETRY_DELAY_MS
#define HTTP_RETRY_DELAY_MS  1500
#endif

#ifndef ACK_ENABLED
#define ACK_ENABLED       true
#endif

// Firmware-Update ueber den Server: /meta.json nennt die bereitgestellte Version,
// ist sie neuer als FIRMWARE_VERSION (oder vom Server erzwungen), laedt das Geraet
// /firmware.bin (MD5-geprueft) in die zweite App-Partition und startet neu. Schafft
// die neue Firmware keinen vollstaendigen Zyklus (Bild + Rueckmeldung), rollt sie
// auf die alte zurueck.
#ifndef FIRMWARE_OTA_ENABLED
#define FIRMWARE_OTA_ENABLED  true
#endif

// Serielle Ausgaben des Zyklus mit dem ACK an den Server schicken (System-Seite → "Geraet")
#ifndef DEVICE_LOG_ENABLED
#define DEVICE_LOG_ENABLED    true
#endif

// Kompaktes 4-bpp-Bild (/current.epd, 960 KB) statt 24-Bit-BMP (5,8 MB), wenn der Server es anbietet
#ifndef PREFER_COMPACT_IMAGE
#define PREFER_COMPACT_IMAGE  true
#endif

#ifndef SERIAL_BAUD
#define SERIAL_BAUD      115200
#endif
