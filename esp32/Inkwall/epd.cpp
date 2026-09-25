#include "epd.h"
#include <esp_task_wdt.h>

// Hardware-SPI (FSPI / SPI2 auf ESP32-S3) – viel schneller als Bit-Bang für 5.76 MB
static SPIClass epd_spi(FSPI);

// ─── Exakte Initialisierungswerte aus dem Waveshare EPD_13IN3E-Treiber ───────

static const uint8_t V_AN_TM[]          = {0xC0, 0x1C, 0x1C, 0xCC, 0xCC, 0xCC, 0x15, 0x15, 0x55};
static const uint8_t V_CMD66[]          = {0x49, 0x55, 0x13, 0x5D, 0x05, 0x10};
static const uint8_t V_PSR[]            = {0xDF, 0x69};
static const uint8_t V_CDI[]            = {0xF7};
static const uint8_t V_TCON[]           = {0x03, 0x03};
static const uint8_t V_AGID[]           = {0x10};
static const uint8_t V_PWS[]            = {0x22};
static const uint8_t V_CCSET[]          = {0x01};
static const uint8_t V_TRES[]           = {0x04, 0xB0, 0x03, 0x20};  // 1200 × 1600
static const uint8_t V_PWR[]            = {0x0F, 0x00, 0x28, 0x2C, 0x28, 0x38};
static const uint8_t V_EN_BUF[]         = {0x07};
static const uint8_t V_BTST_P[]         = {0xE8, 0x28};
static const uint8_t V_BOOST_VDDP_EN[]  = {0x01};
static const uint8_t V_BTST_N[]         = {0xE8, 0x28};
static const uint8_t V_BUCK_BOOST_VDDN[]= {0x01};
static const uint8_t V_TFT_VCOM_POWER[] = {0x02};
static const uint8_t V_POF[]            = {0x00};
static const uint8_t V_DRF[]            = {0x00};

// ─── Kommando-Adressen ────────────────────────────────────────────────────────
#define CMD_AN_TM           0x74
#define CMD_CMD66           0xF0
#define CMD_PSR             0x00
#define CMD_CDI             0x50
#define CMD_TCON            0x60
#define CMD_AGID            0x86
#define CMD_PWS             0xE3
#define CMD_CCSET           0xE0
#define CMD_TRES            0x61
#define CMD_PWR             0x01
#define CMD_EN_BUF          0xB6
#define CMD_BTST_P          0x06
#define CMD_BOOST_VDDP_EN   0xB7
#define CMD_BTST_N          0x05
#define CMD_BUCK_BOOST_VDDN 0xB0
#define CMD_TFT_VCOM_POWER  0xB1
#define CMD_DTM             0x10   // Data Transmission
#define CMD_PON             0x04   // Power On
#define CMD_DRF             0x12   // Display Refresh
#define CMD_POF             0x02   // Power Off
#define CMD_DSLP            0x07   // Deep Sleep

// ─── Low-Level Helpers ────────────────────────────────────────────────────────

static inline void CS_M_L()  { digitalWrite(PIN_EPD_CS_M, LOW);  }
static inline void CS_M_H()  { digitalWrite(PIN_EPD_CS_M, HIGH); }
static inline void CS_S_L()  { digitalWrite(PIN_EPD_CS_S, LOW);  }
static inline void CS_S_H()  { digitalWrite(PIN_EPD_CS_S, HIGH); }
static inline void CS_ALL_L(){ digitalWrite(PIN_EPD_CS_M, LOW);  digitalWrite(PIN_EPD_CS_S, LOW);  }
static inline void CS_ALL_H(){ digitalWrite(PIN_EPD_CS_M, HIGH); digitalWrite(PIN_EPD_CS_S, HIGH); }

// BUSY: LOW = beschäftigt, HIGH = bereit  (Quelle: EPD_13IN3E_ReadBusyH)
// false bei Timeout: ein Panel, das nicht antwortet (loses Flachbandkabel, keine
// Spannung), meldete früher trotzdem "aktualisiert". Der Watchdog wird hier
// gefüttert, weil die Wartezeit selbst begrenzt ist.
static bool WaitBusy(uint32_t timeoutMs = 90000) {
    uint32_t t = millis();
    while (digitalRead(PIN_EPD_BUSY) == LOW) {
        if (millis() - t > timeoutMs) {
            Serial.println("[EPD] WaitBusy Timeout!");
            delay(20);
            return false;
        }
        esp_task_wdt_reset();
        delay(10);
    }
    delay(20);
    return true;
}

// Sendet [Cmd, data[0..len-1]] über SPI. CS muss vorher LOW gesetzt sein.
static void SpiSend(uint8_t cmd, const uint8_t* data, size_t len) {
    epd_spi.transfer(cmd);
    if (data && len) epd_spi.writeBytes(const_cast<uint8_t*>(data), len);
}

// ─── TurnOnDisplay  (PON → DRF → POF) ────────────────────────────────────────
static bool TurnOnDisplay(void) {
    Serial.print("[EPD] Power On... ");
    CS_ALL_L();
    epd_spi.transfer(CMD_PON);
    CS_ALL_H();
    bool ok = WaitBusy();

    delay(50);

    Serial.print("Refresh... ");
    CS_ALL_L();
    SpiSend(CMD_DRF, V_DRF, sizeof(V_DRF));
    CS_ALL_H();
    ok = WaitBusy() && ok;

    CS_ALL_L();
    SpiSend(CMD_POF, V_POF, sizeof(V_POF));
    CS_ALL_H();
    // Kurz auf das Abschalten warten, bevor der nächste Befehl kommt (Reinigung:
    // Schwarz direkt gefolgt von Weiß). Bewusst kurz und nicht als Fehler gewertet.
    WaitBusy(5000);
    Serial.println(ok ? "Done." : "FEHLER (BUSY)");
    return ok;
}

// ════════════════════════════════════════════════════════════════════════════
//  Öffentliche API
// ════════════════════════════════════════════════════════════════════════════

void EPD_Init(void) {
    // Pins konfigurieren
    pinMode(PIN_EPD_CS_M,  OUTPUT); CS_M_H();
    pinMode(PIN_EPD_CS_S,  OUTPUT); CS_S_H();
    pinMode(PIN_EPD_RST,   OUTPUT); digitalWrite(PIN_EPD_RST, HIGH);
    pinMode(PIN_EPD_DC,    OUTPUT); digitalWrite(PIN_EPD_DC,  HIGH);
    pinMode(PIN_EPD_PWR,   OUTPUT); digitalWrite(PIN_EPD_PWR, HIGH);  // Display einschalten
    pinMode(PIN_EPD_BUSY,  INPUT);

    // Hardware-SPI starten (MISO = -1, kein Lesekanal nötig) – nur einmal pro Boot.
    // beginTransaction() haelt den Bus-Mutex; ein zweiter Aufruf ohne endTransaction()
    // blockiert fuer immer. Genau das passierte, wenn in einem Zyklus zweimal
    // initialisiert wurde (Bild + Offline-Balken, Reinigung + Bild).
    static bool spiReady = false;
    if (!spiReady) {
        epd_spi.begin(PIN_EPD_SCLK, -1, PIN_EPD_MOSI, -1);
        epd_spi.beginTransaction(SPISettings(EPD_SPI_FREQ, MSBFIRST, SPI_MODE0));
        spiReady = true;
    }
    delay(20);

    // Reset-Sequenz (5 Flanken × 30 ms, exakt wie im Originalcode)
    digitalWrite(PIN_EPD_RST, HIGH); delay(30);
    digitalWrite(PIN_EPD_RST, LOW);  delay(30);
    digitalWrite(PIN_EPD_RST, HIGH); delay(30);
    digitalWrite(PIN_EPD_RST, LOW);  delay(30);
    digitalWrite(PIN_EPD_RST, HIGH); delay(30);

    // ── Initialisierungssequenz (1:1 aus EPD_13IN3E_Init) ─────────────────

    // AN_TM → nur CS_M
    CS_M_L();
    SpiSend(CMD_AN_TM, V_AN_TM, sizeof(V_AN_TM));
    CS_ALL_H();

    // Alle weiteren Befehle → CS_ALL
    CS_ALL_L(); SpiSend(CMD_CMD66,          V_CMD66,          sizeof(V_CMD66));          CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_PSR,            V_PSR,            sizeof(V_PSR));            CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_CDI,            V_CDI,            sizeof(V_CDI));            CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_TCON,           V_TCON,           sizeof(V_TCON));           CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_AGID,           V_AGID,           sizeof(V_AGID));           CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_PWS,            V_PWS,            sizeof(V_PWS));            CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_CCSET,          V_CCSET,          sizeof(V_CCSET));          CS_ALL_H();
    CS_ALL_L(); SpiSend(CMD_TRES,           V_TRES,           sizeof(V_TRES));           CS_ALL_H();

    // Power/Booster-Befehle → nur CS_M
    CS_M_L(); SpiSend(CMD_PWR,            V_PWR,            sizeof(V_PWR));            CS_ALL_H();
    CS_M_L(); SpiSend(CMD_EN_BUF,         V_EN_BUF,         sizeof(V_EN_BUF));         CS_ALL_H();
    CS_M_L(); SpiSend(CMD_BTST_P,         V_BTST_P,         sizeof(V_BTST_P));         CS_ALL_H();
    CS_M_L(); SpiSend(CMD_BOOST_VDDP_EN,  V_BOOST_VDDP_EN,  sizeof(V_BOOST_VDDP_EN));  CS_ALL_H();
    CS_M_L(); SpiSend(CMD_BTST_N,         V_BTST_N,         sizeof(V_BTST_N));         CS_ALL_H();
    CS_M_L(); SpiSend(CMD_BUCK_BOOST_VDDN,V_BUCK_BOOST_VDDN,sizeof(V_BUCK_BOOST_VDDN));CS_ALL_H();
    CS_M_L(); SpiSend(CMD_TFT_VCOM_POWER, V_TFT_VCOM_POWER, sizeof(V_TFT_VCOM_POWER)); CS_ALL_H();

    Serial.println("[EPD] Init OK");
}

// imageBuffer: EPD_HEIGHT Zeilen × EPD_ROW_BYTES (600) Bytes
//   Bytes 0..EPD_HALF_ROW_BYTES-1   (0..299)  → Master CS (linke 600px)
//   Bytes EPD_HALF_ROW_BYTES..EPD_ROW_BYTES-1 (300..599) → Slave CS (rechte 600px)
bool EPD_Display(const uint8_t* imageBuffer) {
    const uint32_t rowBytes  = EPD_ROW_BYTES;       // 600
    const uint32_t halfBytes = EPD_HALF_ROW_BYTES;  // 300
    const uint32_t height    = EPD_HEIGHT;           // 1600

    // ── Master CS: linke Hälfte jeder Zeile ───────────────────────────────
    Serial.println("[EPD] Sende Master-Haelfte...");
    CS_M_L();
    epd_spi.transfer(CMD_DTM);
    for (uint32_t row = 0; row < height; row++) {
        epd_spi.writeBytes(
            const_cast<uint8_t*>(imageBuffer + row * rowBytes),
            halfBytes
        );
        delay(1);   // Timing aus Originalcode
    }
    CS_ALL_H();

    // ── Slave CS: rechte Hälfte jeder Zeile ───────────────────────────────
    Serial.println("[EPD] Sende Slave-Haelfte...");
    CS_S_L();
    epd_spi.transfer(CMD_DTM);
    for (uint32_t row = 0; row < height; row++) {
        epd_spi.writeBytes(
            const_cast<uint8_t*>(imageBuffer + row * rowBytes + halfBytes),
            halfBytes
        );
        delay(1);
    }
    CS_ALL_H();

    return TurnOnDisplay();
}

bool EPD_Clear(uint8_t color) {
    const uint32_t halfBytes = EPD_HALF_ROW_BYTES;
    const uint32_t height    = EPD_HEIGHT;
    uint8_t fill = (color << 4) | color;

    // Zeilenpuffer auf dem Stack (300 Bytes)
    uint8_t buf[EPD_HALF_ROW_BYTES];
    memset(buf, fill, halfBytes);

    CS_M_L();
    epd_spi.transfer(CMD_DTM);
    for (uint32_t row = 0; row < height; row++) {
        epd_spi.writeBytes(buf, halfBytes);
        delay(1);
    }
    CS_ALL_H();

    CS_S_L();
    epd_spi.transfer(CMD_DTM);
    for (uint32_t row = 0; row < height; row++) {
        epd_spi.writeBytes(buf, halfBytes);
        delay(1);
    }
    CS_ALL_H();

    return TurnOnDisplay();
}

void EPD_Sleep(void) {
    CS_ALL_L();
    epd_spi.transfer(CMD_DSLP);
    epd_spi.transfer(0xA5);
    CS_ALL_H();
    Serial.println("[EPD] Deep Sleep");
}
