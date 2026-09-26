/*
 * Inkwall – ESP32-S3 Firmware
 * Waveshare 13.3″ Spectra 6 (EPD_13IN3E) via HTTP-Polling
 *
 * Server-Einstellung (Python):
 *   RENDER_WIDTH  = 1200
 *   RENDER_HEIGHT = 1600
 *   DISPLAY_ROTATION = 0
 *   OUTPUT_FORMAT = bmp
 *
 * Ablauf je Wake-Zyklus:
 *   1. WiFi verbinden
 *   2. GET /meta.json → Hash, next_wake_sec, kompaktes Bild, Firmware, Uhrzeit, Reinigung
 *      (Fallback: GET /hash)
 *   3. Laufende Firmware bestätigen (Rollback-Schutz nach OTA)
 *   4. Firmware-Version weicht ab? → /firmware.bin per OTA einspielen, Neustart
 *   5. Reinigung fällig? → Panel weiß/schwarz durchfahren, Bild danach neu
 *   6. Hash unverändert? → ACK "unchanged", schlafen
 *   7. GET /current.epd (4 bpp, direkt ins Display) oder /current.bmp (24 Bit, konvertieren)
 *      Das Bild wird zusätzlich im Flash (FFat) abgelegt.
 *   8. Bild anzeigen, ACK mit Ergebnis, Gesundheitsdaten und Logzeilen
 *   9. Deep Sleep
 *
 * Ohne WLAN oder Server: nach OFFLINE_AFTER_FAILS Zyklen wird das letzte Bild
 * aus dem Flash mit einem schwarzen Balken "Keine Verbindung … seit HH:MM"
 * neu gezeichnet. Sobald der Server wieder da ist, kommt das normale Bild.
 *
 * Board-Einstellungen (Arduino IDE):
 *   Board:            ESP32S3 Dev Module
 *   PSRAM:            OPI PSRAM
 *   Flash Size:       16MB
 *   Partition Scheme: 16M Flash (3MB APP/9.9MB FATFS)   ← zwei App-Slots, OTA-fähig, FFat für Bilder
 */

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiClient.h>
#include <HTTPClient.h>
#include <HTTPUpdate.h>
#include <Preferences.h>
#include <FFat.h>
#include <ctype.h>
#include <stddef.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>
#include <sys/time.h>
#include <esp_sleep.h>
#include <esp_heap_caps.h>
#include <esp_ota_ops.h>
#include <esp_task_wdt.h>
#include "config.h"
#include "epd.h"
#include "font_data.h"
#include "text_draw.h"

// ─── Version ─────────────────────────────────────────────────────────────────
// Der Server liest die Version aus dem Marker in der .bin (Gerät-Seite → Firmware).
// Der Marker wird im Boot-Log referenziert, sonst wirft der Linker ihn weg.
#ifdef OTA_SELFTEST_FAIL
#define FIRMWARE_VERSION "1.3.2-selftest"
#else
#define FIRMWARE_VERSION "1.3.2"
#endif
#define FW_MARKER_PREFIX "INKWALL_FW_VERSION="
const char FW_VERSION_MARKER[] __attribute__((used)) = FW_MARKER_PREFIX FIRMWARE_VERSION;
static const char* firmwareVersionFromMarker() { return FW_VERSION_MARKER + sizeof(FW_MARKER_PREFIX) - 1; }

// ─── Zustand über Deep Sleep UND Neustarts ───────────────────────────────────
// RTC_DATA_ATTR wird bei jedem Reset außer dem Aufwachen neu initialisiert: nach
// einem Watchdog-Reset oder Absturz waren Hash, Fehlerzähler und "seit HH:MM"
// weg (unnötiger Bildaufbau, Offline-Balken begann von vorn, Fehler verloren).
// RTC_NOINIT überlebt jeden Reset außer Stromausfall; Kennung und Prüfsumme
// erkennen den Datenmüll nach dem Einschalten oder einer neuen Firmware.
// Diagnose-Spur (siehe unten): letzter begonnener Arbeitsschritt, eigene Kennung und Prüfsumme
struct PhaseTrace {
    uint32_t magic;
    uint32_t boot;      // Boot-Zähler des Starts, der den Schritt begann
    uint32_t atMs;      // millis() beim Beginn des Schritts
    uint32_t phase;
    uint32_t crc;
};

struct PersistState {
    uint32_t magic;
    char     storedHash[33];     // Hash des Bilds auf dem Panel ("" = unbekannt/Statusbild)
    char     failedHash[33];     // Bild, dessen Anzeige zuletzt scheiterte …
    uint8_t  imageFails;         // … so oft in Folge
    uint8_t  failWasWifi;
    uint8_t  bannerShown;        // Offline-Balken steht auf dem Display
    uint8_t  setupShown;
    uint32_t bootCount;
    uint32_t failCount;          // Zyklen in Folge ohne Serverkontakt
    int64_t  firstFailEpoch;     // UTC-Sekunden des ersten Fehlzyklus (0 = Uhr unbekannt)
    int32_t  tzOffsetSec;        // vom Server, für "seit HH:MM"
    char     lastError[96];      // Fehler eines Zyklus ohne ACK (z. B. WLAN weg)
    uint32_t crc;
    // Hinter crc: gehört nicht zur Prüfsumme des Zustands (ab 1.3.2, ältere Stände bleiben gültig)
    PhaseTrace trace;
};
static const uint32_t PERSIST_MAGIC = 0x494E4B33;   // "INK3"
RTC_NOINIT_ATTR static PersistState g_persist;
static char     (&storedHash)[33]  = g_persist.storedHash;
static char     (&failedHash)[33]  = g_persist.failedHash;
static char     (&lastError)[96]   = g_persist.lastError;
static uint8_t&  imageFails        = g_persist.imageFails;
static uint32_t& bootCount         = g_persist.bootCount;
static uint32_t& failCount         = g_persist.failCount;
static int64_t&  firstFailEpoch    = g_persist.firstFailEpoch;
static uint8_t&  failWasWifi       = g_persist.failWasWifi;
static uint8_t&  bannerShown       = g_persist.bannerShown;
static uint8_t&  setupShown        = g_persist.setupShown;
static int32_t&  tzOffsetSec       = g_persist.tzOffsetSec;

static uint32_t persistChecksum() {
    const uint8_t* p = (const uint8_t*)&g_persist;
    uint32_t h = 2166136261u;                              // FNV-1a
    for (size_t i = 0; i < offsetof(PersistState, crc); i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

// Nach jeder Änderung aufrufen: stimmt die Prüfsumme nach einem Absturz nicht,
// gilt der ganze Zustand als verloren (wie früher nach jedem Reset).
static void persistSave() {
    g_persist.magic = PERSIST_MAGIC;
    g_persist.crc = persistChecksum();
}

// true = Zustand übernommen, false = frisch angelegt (Stromausfall, neue Firmware)
static bool persistLoad() {
    if (g_persist.magic == PERSIST_MAGIC && g_persist.crc == persistChecksum()) {
        g_persist.storedHash[32] = 0;
        g_persist.failedHash[32] = 0;
        g_persist.lastError[95] = 0;
        return true;
    }
    PhaseTrace trace = g_persist.trace;   // Diagnose-Spur hat eine eigene Prüfsumme, nicht mit wegwerfen
    memset(&g_persist, 0, sizeof(g_persist));
    g_persist.trace = trace;
    persistSave();
    return false;
}

// ── Diagnose: in welchem Schritt endete der letzte Start? ────────────────────
// Bricht die Versorgung ein (Brownout) oder stürzt der Chip ab, steht der
// zuletzt begonnene Schritt noch im RTC-Speicher (überlebt wie g_persist jeden
// Reset außer Stromausfall). Der nächste Start meldet ihn mit dem ACK – so wird
// sichtbar, ob WLAN, Bildaufbau oder das Speichern im Flash die Spannung einbrechen lässt.
enum Phase : uint8_t { PH_NONE = 0, PH_START, PH_WIFI, PH_META, PH_OTA, PH_CLEAN, PH_BANNER,
                       PH_DOWNLOAD, PH_DISPLAY, PH_FLASH, PH_ACK, PH_SLEEP, PH_COUNT };
static const char* const PHASE_NAMES[PH_COUNT] = { "", "start", "wifi", "meta", "ota", "clean", "banner",
                                                   "download", "display", "flash", "ack", "sleep" };
static const uint32_t TRACE_MAGIC = 0x50485332;   // "PHS2"
static PhaseTrace& g_trace = g_persist.trace;     // liegt in g_persist, dort hinter der Prüfsumme
static const char* g_lastPhase = nullptr;         // nur nach Unterspannung, Absturz oder Watchdog gesetzt
static uint32_t g_lastPhaseMs = 0;
static uint32_t g_lastPhaseBoot = 0;

static uint32_t traceChecksum() {
    const uint8_t* p = (const uint8_t*)&g_trace;
    uint32_t h = 2166136261u;                              // FNV-1a wie g_persist
    for (size_t i = 0; i < offsetof(PhaseTrace, crc); i++) { h ^= p[i]; h *= 16777619u; }
    return h;
}

static void setPhase(uint8_t phase) {      // uint8_t statt Phase: Arduino setzt Prototypen vor die enum-Definition
    g_trace.magic = TRACE_MAGIC;
    g_trace.boot = bootCount;
    g_trace.atMs = millis();
    g_trace.phase = phase;
    g_trace.crc = traceChecksum();
}

// Beim Start: endete der vorige Lauf ungeplant, dessen letzten Schritt übernehmen
static void traceLoad() {
    esp_reset_reason_t rr = esp_reset_reason();
    bool abnormal = rr == ESP_RST_BROWNOUT || rr == ESP_RST_PANIC || rr == ESP_RST_INT_WDT
                 || rr == ESP_RST_TASK_WDT || rr == ESP_RST_WDT;
    if (abnormal && g_trace.magic == TRACE_MAGIC && g_trace.crc == traceChecksum()
        && g_trace.phase > PH_NONE && g_trace.phase < PH_COUNT) {
        g_lastPhase = PHASE_NAMES[g_trace.phase];
        g_lastPhaseMs = g_trace.atMs;
        g_lastPhaseBoot = g_trace.boot;
    }
}

static const uint32_t OFFLINE_AFTER_FAILS = 3;         // 3 × 5 min = 15 min
static const int64_t  EPOCH_KNOWN_MIN = 1600000000LL;  // Uhrzeiten davor gelten als "nicht gestellt"
static const char*    LAST_IMAGE_PATH = "/last.epd";   // wie /current.epd (Header + Nutzdaten)
static const char*    LAST_IMAGE_TMP  = "/last.tmp";   // erst hierhin, dann umbenennen
static const char*    LAST_META_PATH  = "/last.txt";   // "hash\nepoch\n"
static const char*    SHOWN_PATH      = "/shown.txt";  // Hash des Bilds auf dem Panel ("" = Statusbild)

// ─── OTA-Gedächtnis im Flash (NVS) ───────────────────────────────────────────
// Rollback-Schutz in der Firmware selbst (der Arduino-Bootloader lässt eine
// neue Firmware nicht im Prüfzustand, siehe Gerätelog "valid" direkt nach OTA):
//   - vor dem Einspielen: pendingVerify=1, Zielversion, MD5 und Zielpartition merken
//   - die neue Firmware zählt ihre Starts; bestätigt ist sie erst nach einem
//     vollständigen Zyklus (Bild angezeigt oder unverändert, Rückmeldung angekommen)
//     – eine Firmware, die beim Bildaufbau hängt oder abstürzt, wird so erkannt
//   - drei Starts ohne erfolgreichen Zyklus → Update.rollBack() auf die alte Partition
//   - die alte Firmware sieht rolledBack, meldet es und lädt genau diese Datei nicht erneut
//   - läuft nach einem Neustart gar nicht die Zielpartition (Strom weg mitten im
//     Update), ist nichts zu prüfen
//   - scheitert das Einspielen selbst, höchstens OTA_MAX_TRIES Versuche je Datei
// Bewusst NVS statt RTC-Speicher: RTC-Variablen liegen je Firmware-Build an anderen
// Adressen, die neue Firmware würde die Notiz der alten nicht finden. NVS ist
// adressunabhängig und überlebt auch Stromausfall.
struct OtaMemory {
    char     targetVersion[32];
    char     targetMd5[33];
    char     targetPart[17];
    char     tryId[48];          // MD5 (oder Version) der zuletzt versuchten Datei
    uint8_t  tries;              // … so oft ohne Erfolg versucht
    int64_t  triedAt;            // UTC-Sekunden des letzten Versuchs (0 = unbekannt)
    uint8_t  pendingVerify;
    uint8_t  failedBoots;
    uint8_t  rolledBack;
    uint8_t  rollbackReported;
};
static OtaMemory otaMemory;
static const char* OTA_NVS_NAMESPACE = "plexeink";   // bewusst der alte Name: sonst vergisst das Gerät sein OTA-Gedächtnis
static const uint8_t OTA_MAX_TRIES = 3;
static const int64_t OTA_RETRY_AFTER_SEC = 24 * 3600;  // danach wieder erlaubt (z. B. WLAN war nur schwach)

static void otaMemoryLoad() {
    memset(&otaMemory, 0, sizeof(otaMemory));
    Preferences prefs;
    if (!prefs.begin(OTA_NVS_NAMESPACE, true)) return;      // noch nie geschrieben
    prefs.getString("target", otaMemory.targetVersion, sizeof(otaMemory.targetVersion));
    prefs.getString("t_md5", otaMemory.targetMd5, sizeof(otaMemory.targetMd5));
    prefs.getString("t_part", otaMemory.targetPart, sizeof(otaMemory.targetPart));
    prefs.getString("try_id", otaMemory.tryId, sizeof(otaMemory.tryId));
    otaMemory.tries            = prefs.getUChar("tries", 0);
    otaMemory.triedAt          = prefs.getLong64("tried_at", 0);
    otaMemory.pendingVerify    = prefs.getUChar("pending", 0);
    otaMemory.failedBoots      = prefs.getUChar("fails", 0);
    otaMemory.rolledBack       = prefs.getUChar("rolled", 0);
    otaMemory.rollbackReported = prefs.getUChar("reported", 0);
    prefs.end();
}

static void otaMemorySave() {
    Preferences prefs;
    if (!prefs.begin(OTA_NVS_NAMESPACE, false)) { Serial.println("[NVS] nicht beschreibbar"); return; }
    prefs.putString("target", otaMemory.targetVersion);
    prefs.putString("t_md5", otaMemory.targetMd5);
    prefs.putString("t_part", otaMemory.targetPart);
    prefs.putString("try_id", otaMemory.tryId);
    prefs.putUChar("tries", otaMemory.tries);
    prefs.putLong64("tried_at", otaMemory.triedAt);
    prefs.putUChar("pending", otaMemory.pendingVerify);
    prefs.putUChar("fails", otaMemory.failedBoots);
    prefs.putUChar("rolled", otaMemory.rolledBack);
    prefs.putUChar("reported", otaMemory.rollbackReported);
    prefs.end();
}

// "1.10.2" > "1.9.9"; Anhänge wie "-selftest" zählen nicht (gleich = nicht neuer)
static int compareVersions(const char* a, const char* b) {
    int va[3] = {0, 0, 0}, vb[3] = {0, 0, 0};
    sscanf(a ? a : "", "%d.%d.%d", &va[0], &va[1], &va[2]);
    sscanf(b ? b : "", "%d.%d.%d", &vb[0], &vb[1], &vb[2]);
    for (int i = 0; i < 3; i++) {
        if (va[i] != vb[i]) return va[i] < vb[i] ? -1 : 1;
    }
    return 0;
}

// Falls der Bootloader doch einmal den Prüfzustand nutzt: nicht automatisch bestätigen,
// das macht der Sketch nach erfolgreichem Serverkontakt.
bool verifyRollbackLater() { return true; }

static const uint32_t HTTP_STREAM_IDLE_TIMEOUT_MS = 10000;
static const size_t   EPD_HEADER_SIZE = 16;
static const size_t   EPD_BUF_SIZE = (size_t)EPD_HEIGHT * EPD_ROW_BYTES;     // 960 000
static const size_t   DEVICE_LOG_MAX_CHARS = 3000;

// ─── Zyklus-Zustand ──────────────────────────────────────────────────────────
struct Meta {
    String hash;
    uint32_t nextWakeSec = POLL_FALLBACK_SEC;
    String format;                 // Ausgabeformat des Servers (bmp | png)
    String epdUrl;
    uint32_t epdSize = 0;          // Dateigröße des kompakten Bilds laut Server
    String firmwareVersion;
    String firmwareMd5;
    String firmwareUrl;
    bool firmwareForce = false;    // auch einspielen, wenn nicht neuer (Downgrade, Test-Build)
    bool sleepFromMeta = false;    // Server rechnet damit, dass das Gerät die Zeit ab meta.json abzieht
    int64_t epoch = 0;
    int32_t tzOffsetSec = 0;
    bool cleanDue = false;
    bool showOfflineTest = false;
};

static String   g_log;                 // Logzeilen dieses Zyklus (gehen mit dem ACK zum Server)
static uint32_t g_cycleStart = 0;
static uint32_t g_metaAtMs = 0;        // millis() beim Eintreffen von /meta.json (0 = noch nicht)
static uint32_t g_downloadMs = 0;
static uint32_t g_refreshMs  = 0;
static String   g_imageFormat;
static String   g_error;
static uint8_t  g_cleaned = 0;
static uint32_t g_offlineSeconds = 0;
static bool     g_storageOk = false;
static bool     g_panelFailed = false; // Panel hat nicht geantwortet (BUSY) – zählt gegen eine neue Firmware

// ─── Vorwärtsdeklarationen ───────────────────────────────────────────────────
void     logf(const char* fmt, ...);
bool     connectWiFi();
bool     httpBegin(HTTPClient& http, const String& url, uint32_t timeoutMs);
String   httpGetString(const String& url, uint32_t timeoutMs = HTTP_SHORT_TIMEOUT_MS);
bool     httpGetBinary(const String& url, uint8_t* buf, size_t bufSize, size_t& outLen);
bool     httpPostJson(const String& url, const String& body, int& outCode, uint32_t timeoutMs = HTTP_SHORT_TIMEOUT_MS);
bool     fetchMeta(Meta& meta);
bool     performOta(const Meta& meta, const String& tryId);
bool     fetchAndDisplayEpd(const String& url, const char* hash);
bool     fetchAndDisplayBmp(const char* hash);
bool     sendAck(const char* result, const char* hash, const char* fwTarget = nullptr);
void     goSleep(uint32_t seconds);
uint8_t  rgbToSpectra6(uint8_t r, uint8_t g, uint8_t b);
bool     hasUnsetConfig();
bool     parsePositiveUIntField(const String& json, const char* key, uint32_t& outValue);
bool     parseStringField(const String& json, const char* key, String& outValue);
bool     parseBoolField(const String& json, const char* key, bool& outValue);
bool     parseBmpHeader(const uint8_t* buf, size_t bufLen,
                        uint32_t& pixelOffset, int32_t& width,
                        int32_t& height, uint16_t& bpp, uint32_t& rowStride);
static void setError(const char* text);
static const char* wakeReasonText();
static const char* resetReasonText();
static String jsonEscape(const String& in);
static void onCycleFailed(bool wifiFailed);
static void showOfflineBanner(bool wifiFailed, bool test);
static void showSetupPage();
static bool runCleanCycle();
static void saveLastImage(const uint8_t* payload, const char* hash);
static void rememberShown(const char* hash);
static String readShown();
static String localTimeText(int64_t epochUtc);
static int64_t nowEpoch();
static bool isHexHash(const String& s);
static void confirmFirmware();
static void feedWatchdog();
static void finishCycle(const char* result, uint32_t sleepSec);

// ════════════════════════════════════════════════════════════════════════════
void setup() {
    Serial.begin(SERIAL_BAUD);
    delay(500);
    g_cycleStart = millis();
    bool persisted = persistLoad();
    bootCount++;
    persistSave();
    traceLoad();              // Schritt, in dem der vorige Start ungeplant endete
    setPhase(PH_START);

    // Watchdog: haengt eine Phase (WLAN, Download, Panel, Flash) laenger als
    // 5 Minuten, startet der Chip neu statt fuer immer wach zu bleiben. Zwischen
    // den Phasen wird er gefuettert, das Panel meldet sich selbst per BUSY-Timeout.
    {
        esp_task_wdt_config_t wdt = { .timeout_ms = 300000, .idle_core_mask = 0, .trigger_panic = true };
        esp_task_wdt_reconfigure(&wdt);
        esp_task_wdt_add(NULL);
    }
    logf("== Inkwall %s Boot #%lu (Wecken: %s, Start: %s) ==", firmwareVersionFromMarker(), (unsigned long)bootCount,
         wakeReasonText(), resetReasonText());
    if (!persisted) logf("[RTC] Kein Zustand aus dem letzten Lauf (Stromausfall oder neue Firmware)");
    if (g_lastPhase) {
        logf("[Diag] Start #%lu endete ungeplant (%s) im Schritt '%s', der %lu ms nach dem Start begann",
             (unsigned long)g_lastPhaseBoot, resetReasonText(), g_lastPhase, (unsigned long)g_lastPhaseMs);
    }
    logf("PSRAM frei: %u KB", heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024);
    {
        // Welcher App-Slot laeuft, und in welchem OTA-Zustand sind beide Slots?
        const esp_partition_t* part = esp_ota_get_running_partition();
        const esp_partition_t* next = esp_ota_get_next_update_partition(NULL);
        auto stateName = [](const esp_partition_t* p) -> const char* {
            esp_ota_img_states_t st;
            if (!p || esp_ota_get_state_partition(p, &st) != ESP_OK) return "?";
            switch (st) {
                case ESP_OTA_IMG_NEW:            return "new";
                case ESP_OTA_IMG_PENDING_VERIFY: return "pending";
                case ESP_OTA_IMG_VALID:          return "valid";
                case ESP_OTA_IMG_INVALID:        return "invalid";
                case ESP_OTA_IMG_ABORTED:        return "aborted";
                default:                         return "undefined";
            }
        };
        logf("Partition: %s (%s), andere: %s (%s)",
             part ? part->label : "?", stateName(part), next ? next->label : "?", stateName(next));
    }
    if (lastError[0]) {
        logf("[WARN] Letzter Zyklus ohne Rueckmeldung: %s", lastError);
    }
    otaMemoryLoad();

    // ── Frisch per OTA geflasht? Starts zählen, notfalls zurückrollen ────────
    if (otaMemory.pendingVerify) {
        const esp_partition_t* running = esp_ota_get_running_partition();
        if (otaMemory.targetPart[0] && running && strcmp(running->label, otaMemory.targetPart) != 0) {
            // Strom weg oder Fehler mitten im Einspielen: die alte Firmware laeuft weiter
            logf("[OTA] Update auf %s ist nicht aktiv geworden (laeuft %s mit %s) - nichts zu pruefen",
                 otaMemory.targetPart, running->label, FIRMWARE_VERSION);
            otaMemory.pendingVerify = 0;
            otaMemory.failedBoots = 0;
            otaMemory.targetPart[0] = 0;
            otaMemorySave();
        } else {
            otaMemory.failedBoots++;
            if (otaMemory.failedBoots > 3) {
                logf("[OTA] %s hat dreimal keinen vollstaendigen Zyklus geschafft -> Rollback", FIRMWARE_VERSION);
                otaMemory.pendingVerify = 0;
                otaMemory.failedBoots = 0;
                otaMemory.rolledBack = 1;
                otaMemory.rollbackReported = 0;
                otaMemorySave();
                persistSave();
                if (Update.canRollBack() && Update.rollBack()) {
                    Serial.flush();
                    delay(200);
                    ESP.restart();
                }
                logf("[OTA] Rollback nicht moeglich - keine bootfaehige alte Firmware");
            } else {
                otaMemorySave();
                logf("[OTA] Lauf %u von %s, bestaetigt nach dem ersten vollstaendigen Zyklus (Bild + Rueckmeldung)",
                     (unsigned)otaMemory.failedBoots, FIRMWARE_VERSION);
            }
        }
    }

#ifdef OTA_SELFTEST_FAIL
    // Selbsttest des Rollbacks: diese Firmware erreicht den Server absichtlich nie.
    // Notbremse unabhängig vom NVS-Gedächtnis: nach sechs Starts in jedem Fall zurück.
    {
        static RTC_DATA_ATTR uint32_t selftestBoots = 0;
        selftestBoots++;
        logf("[SELFTEST] Absichtlich kaputte Firmware (Start %lu) - kein WLAN, Sleep 20 s, Rollback erwartet",
             (unsigned long)selftestBoots);
        if (selftestBoots > 6 && Update.canRollBack() && Update.rollBack()) {
            logf("[SELFTEST] Notbremse: Rollback ohne NVS");
            Serial.flush(); delay(200); ESP.restart();
        }
        Serial.flush();
        esp_sleep_enable_timer_wakeup(20ULL * 1000000ULL);
        esp_deep_sleep_start();
    }
#endif

    // ── Bildspeicher im Flash (FFat-Partition, 9,9 MB) ───────────────────────
    g_storageOk = FFat.begin(true);   // formatiert beim ersten Mal
    if (!g_storageOk) logf("[WARN] Flash-Dateisystem nicht verfuegbar - kein Offline-Bild");

    // Nach Stromausfall steht das letzte Bild meist noch auf dem Panel: nicht
    // ohne Grund neu zeichnen (30 s Refresh, Verschleiß)
    if (!persisted && g_storageOk) {
        String shown = readShown();
        if (isHexHash(shown)) {
            shown.toCharArray(storedHash, sizeof(storedHash));
            logf("[RTC] Panel zeigt noch Bild %.8s", storedHash);
        }
    }
    // Eine frisch eingespielte Firmware muss das Bild einmal selbst zeichnen, bevor sie als gut gilt
    if (otaMemory.pendingVerify && storedHash[0]) {
        storedHash[0] = 0;
        logf("[OTA] Bild wird zur Probe neu gezeichnet");
    }
    persistSave();

    if (hasUnsetConfig()) {
        logf("[ERR] Konfiguration unvollstaendig. Bitte config.private.h oder config.example.h anpassen.");
        setError("Konfiguration unvollstaendig");
        if (!setupShown) { showSetupPage(); setupShown = 1; persistSave(); }
        goSleep(POLL_FALLBACK_SEC);
    }

    setPhase(PH_WIFI);
    if (!connectWiFi()) {
        logf("[WARN] WiFi failed -> Fallback-Sleep");
        setError("WLAN nicht erreichbar");
        onCycleFailed(true);
        goSleep(POLL_FALLBACK_SEC);
    }
    feedWatchdog();

    // ── Meta (Hash, Wake, Bildformat, Firmware, Uhrzeit, Reinigung) ──────────
    setPhase(PH_META);
    Meta meta;
    if (!fetchMeta(meta)) {
        // Alter Server ohne meta.json? Notfalls nur den Hash holen.
        meta.hash = httpGetString(String(SERVER_BASE_URL) + "/hash");
        meta.hash.trim();
        if (!isHexHash(meta.hash)) {
            logf("[WARN] Server nicht erreichbar (/meta.json und /hash)");
            setError("Server nicht erreichbar");
            onCycleFailed(false);
            WiFi.disconnect(true);
            WiFi.mode(WIFI_OFF);
            goSleep(POLL_FALLBACK_SEC);
        }
        g_metaAtMs = millis();
    }
    feedWatchdog();
    logf("[Hash] server=%s lokal=%s", meta.hash.c_str(), storedHash[0] ? storedHash : "(leer)");

    // ── Uhr vom Server stellen ───────────────────────────────────────────────
    if (meta.epoch > EPOCH_KNOWN_MIN) {
        struct timeval tv = { (time_t)meta.epoch, 0 };
        settimeofday(&tv, nullptr);
        tzOffsetSec = meta.tzOffsetSec;
    }

    // ── Wieder online? Offline-Dauer merken, Balken vom Display holen ────────
    bool wasOffline = failCount > 0;
    if (wasOffline) {
        if (firstFailEpoch > EPOCH_KNOWN_MIN && nowEpoch() > firstFailEpoch)
            g_offlineSeconds = (uint32_t)(nowEpoch() - firstFailEpoch);
        logf("[Net] Wieder erreichbar nach %lu Fehlzyklen (%lu s)", (unsigned long)failCount, (unsigned long)g_offlineSeconds);
        failCount = 0;
        firstFailEpoch = 0;
    }
    if (bannerShown) {
        bannerShown = 0;
        storedHash[0] = 0;      // Balken steht auf dem Panel -> Bild in jedem Fall neu zeichnen
    }
    persistSave();

    // ── Wurde die letzte Firmware zurückgerollt? ─────────────────────────────
    // (Eine neue Firmware wird erst am Ende eines vollständigen Zyklus bestätigt, siehe confirmFirmware)
    const esp_partition_t* running = esp_ota_get_running_partition();
    String rejectedVersion, rejectedMd5;
    const esp_partition_t* other = esp_ota_get_next_update_partition(NULL);
    esp_ota_img_states_t otherState;
    bool otherAborted = other && esp_ota_get_state_partition(other, &otherState) == ESP_OK
        && (otherState == ESP_OTA_IMG_ABORTED || otherState == ESP_OTA_IMG_INVALID);
    if (otaMemory.targetVersion[0] && !otaMemory.pendingVerify && (otaMemory.rolledBack || otherAborted)) {
        rejectedVersion = otaMemory.targetVersion;
        rejectedMd5 = otaMemory.targetMd5;
        if (!otaMemory.rollbackReported) {
            g_error = String("Firmware ") + rejectedVersion + " zurueckgerollt: kein vollstaendiger Zyklus nach dem Update";
            logf("[OTA] %s (laeuft wieder %s auf %s)", g_error.c_str(), FIRMWARE_VERSION, running ? running->label : "?");
            otaMemory.rollbackReported = 1;
            otaMemorySave();
        }
    }

    // ── Firmware-Update? ─────────────────────────────────────────────────────
    // Nur neuere Versionen (ein per USB aufgespieltes 1.4.0 fällt nicht auf ein
    // bereitgestelltes 1.3.0 zurück) – ausser der Server erzwingt es.
    if (FIRMWARE_OTA_ENABLED && !meta.firmwareVersion.isEmpty() && !meta.firmwareUrl.isEmpty()
        && meta.firmwareVersion != FIRMWARE_VERSION) {
        int cmp = compareVersions(meta.firmwareVersion.c_str(), FIRMWARE_VERSION);
        String tryId = meta.firmwareMd5.isEmpty() ? meta.firmwareVersion : meta.firmwareMd5;
        bool rejected = !rejectedVersion.isEmpty() && meta.firmwareVersion == rejectedVersion
            && (rejectedMd5.isEmpty() || meta.firmwareMd5.isEmpty() || meta.firmwareMd5 == rejectedMd5);
        if (cmp <= 0 && !meta.firmwareForce) {
            logf("[OTA] Server bietet %s an, laufend ist %s - nicht neuer, kein Update", meta.firmwareVersion.c_str(), FIRMWARE_VERSION);
        } else if (rejected) {
            logf("[OTA] %s wurde schon einmal zurueckgerollt - kein neuer Versuch, bis eine andere Datei bereitsteht",
                 rejectedVersion.c_str());
        } else if (strcmp(otaMemory.tryId, tryId.c_str()) == 0 && otaMemory.tries >= OTA_MAX_TRIES
                   && !(nowEpoch() > EPOCH_KNOWN_MIN && otaMemory.triedAt > 0
                        && nowEpoch() - otaMemory.triedAt > OTA_RETRY_AFTER_SEC)) {
            logf("[OTA] Einspielen von %s ist %u-mal gescheitert - neuer Versuch mit einer anderen Datei oder nach 24 h",
                 meta.firmwareVersion.c_str(), (unsigned)otaMemory.tries);
        } else {
            logf("[OTA] Server bietet %s an, laufend ist %s -> Update%s", meta.firmwareVersion.c_str(), FIRMWARE_VERSION,
                 cmp <= 0 ? " (erzwungen)" : "");
            setPhase(PH_OTA);
            performOta(meta, tryId);   // startet bei Erfolg neu; sonst laeuft der Zyklus normal weiter
        }
    }
    feedWatchdog();

    // ── Panelreinigung ───────────────────────────────────────────────────────
    if (meta.cleanDue) {
        setPhase(PH_CLEAN);
        if (runCleanCycle()) {
            g_cleaned = 1;
        } else {
            g_panelFailed = true;
            g_error = "Panel antwortet nicht (Reinigung)";
        }
        storedHash[0] = 0;      // danach das Bild in jedem Fall neu
        rememberShown("");
        persistSave();
        feedWatchdog();
    }

    // ── Probe des Offline-Hinweises (von der Gerät-Seite angefordert) ────────
    // Balken auf das gespeicherte Bild, ein einziger Bildaufbau; der naechste
    // Zyklus holt das normale Bild, weil der Hash geloescht wird.
    if (meta.showOfflineTest) {
        showOfflineBanner(false, true);
        bannerShown = 1;
        storedHash[0] = 0;
        rememberShown("");
        persistSave();
        finishCycle("test", meta.nextWakeSec);
    }

    // ── Hash unverändert → nur melden und schlafen ───────────────────────────
    if (meta.hash == String(storedHash)) {
        logf("[Hash] Unveraendert -> Sleep %lu s", (unsigned long)meta.nextWakeSec);
        finishCycle(g_error.isEmpty() ? "unchanged" : "error", meta.nextWakeSec);
    }

    // ── Neues Bild laden und anzeigen ────────────────────────────────────────
    logf("[Hash] Geaendert -> Bild laden...");
    bool shown = false;
    uint32_t sleepSec = meta.nextWakeSec;
    bool givenUp = strcmp(failedHash, meta.hash.c_str()) == 0 && imageFails >= MAX_IMAGE_TRIES;
    if (givenUp) {
        logf("[ERR] Bild %.8s liess sich %u-mal nicht anzeigen - kein neuer Versuch, bis der Server ein anderes hat",
             meta.hash.c_str(), (unsigned)imageFails);
        g_error = "Bild wiederholt nicht anzeigbar";
    } else if (meta.format == "png" && meta.epdUrl.isEmpty()) {
        g_error = "Server liefert PNG - fuer das Panel OUTPUT_FORMAT=bmp einstellen";
        logf("[ERR] %s", g_error.c_str());
    } else if (PREFER_COMPACT_IMAGE && !meta.epdUrl.isEmpty()) {
        // Kompaktes Bild angeboten: nur das. Ein BMP als Ersatz waere gleich gross
        // gerendert (5,8 MB umsonst) und scheitert an denselben Netzproblemen.
        const uint32_t expected = EPD_HEADER_SIZE + EPD_BUF_SIZE;
        if (meta.epdSize && meta.epdSize != expected) {
            g_error = String("Server rendert fuer ein anderes Panel (") + meta.epdSize + " statt " + expected
                    + " Bytes) - RENDER_WIDTH, RENDER_HEIGHT und Drehung pruefen";
            logf("[ERR] %s", g_error.c_str());
        } else {
            shown = fetchAndDisplayEpd(meta.epdUrl, meta.hash.c_str());
        }
    } else {
        shown = fetchAndDisplayBmp(meta.hash.c_str());
    }

    if (shown) {
        meta.hash.toCharArray(storedHash, sizeof(storedHash));
        imageFails = 0;
        failedHash[0] = 0;
        rememberShown(storedHash);
        logf("[OK] Bild aktualisiert (%s, Download %lu ms, Anzeige %lu ms)",
             g_imageFormat.c_str(), (unsigned long)g_downloadMs, (unsigned long)g_refreshMs);
    } else if (!givenUp) {
        if (strcmp(failedHash, meta.hash.c_str()) != 0) {
            meta.hash.toCharArray(failedHash, sizeof(failedHash));
            imageFails = 0;
        }
        imageFails++;
        if (g_error.isEmpty()) g_error = "Bild konnte nicht geladen werden";
        logf("[ERR] Bild konnte nicht angezeigt werden (Versuch %u von %u)", (unsigned)imageFails, (unsigned)MAX_IMAGE_TRIES);
        // Bald noch einmal versuchen statt einen ganzen Takt das alte Bild zu zeigen
        if (imageFails < MAX_IMAGE_TRIES && sleepSec > IMAGE_RETRY_SEC) sleepSec = IMAGE_RETRY_SEC;
    }
    persistSave();
    finishCycle(shown && g_error.isEmpty() ? "updated" : "error", sleepSec);
}

void loop() {}


// ════════════════════════════════════════════════════════════════════════════
//  Log, Zeit, Fehler
// ════════════════════════════════════════════════════════════════════════════
void logf(const char* fmt, ...) {
    char buf[256];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    Serial.println(buf);
    if (DEVICE_LOG_ENABLED && g_log.length() + strlen(buf) + 1 < DEVICE_LOG_MAX_CHARS) {
        g_log += buf;
        g_log += '\n';
    }
}

static void setError(const char* text) {
    strncpy(lastError, text, sizeof(lastError) - 1);
    lastError[sizeof(lastError) - 1] = 0;
    g_error = text;
    persistSave();
}

static void feedWatchdog() {
    esp_task_wdt_reset();
}

// 32 Hex-Zeichen (MD5). Alles andere – etwa die Antwort eines Captive Portals
// auf /hash – ist kein Hash und darf weder einen Download noch eine OTA-Bestätigung auslösen.
static bool isHexHash(const String& s) {
    if (s.length() != 32) return false;
    for (size_t i = 0; i < s.length(); i++) {
        if (!isxdigit((unsigned char)s[i])) return false;
    }
    return true;
}

// Eine frisch eingespielte Firmware gilt als gut, wenn ein Zyklus vollständig
// durchlief: Server erreicht, Bild angezeigt oder unverändert, Rückmeldung
// angekommen, das Panel hat geantwortet. Erst dann nicht mehr zurückrollen.
static void confirmFirmware() {
    const esp_partition_t* running = esp_ota_get_running_partition();
    esp_ota_img_states_t otaState;
    if (running && esp_ota_get_state_partition(running, &otaState) == ESP_OK
        && otaState == ESP_OTA_IMG_PENDING_VERIFY) {
        esp_ota_mark_app_valid_cancel_rollback();
    }
    if (!otaMemory.pendingVerify) return;
    otaMemory.pendingVerify = 0;
    otaMemory.failedBoots = 0;
    otaMemory.targetVersion[0] = 0;
    otaMemory.targetMd5[0] = 0;
    otaMemory.targetPart[0] = 0;
    otaMemory.tryId[0] = 0;
    otaMemory.tries = 0;
    otaMemory.triedAt = 0;
    otaMemorySave();
    logf("[OTA] Neue Firmware %s bestaetigt: Zyklus vollstaendig (Partition %s)", FIRMWARE_VERSION,
         running ? running->label : "?");
}

// Schlafzeit ab jetzt: rechnet der Server mit dem Zeitpunkt von /meta.json,
// zieht das Gerät die Zeit seither ab (Download, Bildaufbau, ACK) – ein langer
// Zyklus mit Bildaufbau verschiebt so das nächste Aufwachen nicht.
static bool g_sleepFromMeta = false;
static uint32_t sleepAfterMeta(uint32_t seconds) {
    if (!g_sleepFromMeta || !g_metaAtMs) return seconds;
    uint32_t elapsed = (millis() - g_metaAtMs) / 1000;
    return seconds > elapsed + MIN_SLEEP_SEC ? seconds - elapsed : MIN_SLEEP_SEC;
}

// Ende eines Zyklus mit Serverkontakt: melden, ggf. Firmware bestätigen, schlafen
static void finishCycle(const char* result, uint32_t sleepSec) {
    // Die Zeile muss vor der Rückmeldung stehen, sonst erreicht sie den Server nie
    if (otaMemory.pendingVerify && !g_panelFailed) {
        logf("[OTA] Zyklus vollstaendig - %s wird mit dieser Rueckmeldung bestaetigt", FIRMWARE_VERSION);
    }
    bool reported = !ACK_ENABLED || sendAck(result, storedHash);
    if (!reported) logf("[WARN] ACK nicht bestaetigt");
    if (reported && !g_panelFailed) confirmFirmware();
    WiFi.disconnect(true);
    WiFi.mode(WIFI_OFF);
    goSleep(sleepAfterMeta(sleepSec));
}

static int64_t nowEpoch() {
    return (int64_t)time(nullptr);
}

// "HH:MM" in Serverzeitzone, leer wenn die Uhr nie gestellt wurde
static String localTimeText(int64_t epochUtc) {
    if (epochUtc < EPOCH_KNOWN_MIN) return String();
    time_t local = (time_t)(epochUtc + tzOffsetSec);
    struct tm tmv;
    gmtime_r(&local, &tmv);
    char buf[8];
    snprintf(buf, sizeof(buf), "%02d:%02d", tmv.tm_hour, tmv.tm_min);
    return String(buf);
}

static const char* wakeReasonText() {
    switch (esp_sleep_get_wakeup_cause()) {
        case ESP_SLEEP_WAKEUP_TIMER: return "timer";
        case ESP_SLEEP_WAKEUP_EXT0:
        case ESP_SLEEP_WAKEUP_EXT1:  return "button";
        default:
            // Kein Aufwachen aus dem Schlaf: der Grund steht im Reset (Einschalten, Absturz, Neustart)
            return esp_reset_reason() == ESP_RST_POWERON ? "poweron" : "reset";
    }
}

// Warum ist der Chip gestartet? Absturz und Watchdog waren früher nicht von
// „eingeschaltet“ zu unterscheiden – jetzt gehen sie mit der Rückmeldung zum Server.
static const char* resetReasonText() {
    switch (esp_reset_reason()) {
        case ESP_RST_POWERON:   return "poweron";
        case ESP_RST_EXT:       return "external";
        case ESP_RST_SW:        return "restart";
        case ESP_RST_PANIC:     return "panic";
        case ESP_RST_INT_WDT:   return "int_wdt";
        case ESP_RST_TASK_WDT:  return "task_wdt";
        case ESP_RST_WDT:       return "wdt";
        case ESP_RST_DEEPSLEEP: return "deepsleep";
        case ESP_RST_BROWNOUT:  return "brownout";
        case ESP_RST_SDIO:      return "sdio";
        default:                return "unknown";
    }
}

static String jsonEscape(const String& in) {
    String out;
    out.reserve(in.length() + 8);
    for (size_t i = 0; i < in.length(); i++) {
        char c = in[i];
        switch (c) {
            case '"':  out += "\\\""; break;
            case '\\': out += "\\\\"; break;
            case '\n': out += "\\n";  break;
            case '\r': break;
            case '\t': out += "  ";   break;
            default:
                if ((unsigned char)c < 0x20) break;
                out += c;
        }
    }
    return out;
}

// Zyklus ohne Serverkontakt: zählen, ab der Schwelle den Offline-Balken zeigen
static void onCycleFailed(bool wifiFailed) {
    failCount++;
    failWasWifi = wifiFailed ? 1 : 0;
    if (firstFailEpoch == 0 && nowEpoch() > EPOCH_KNOWN_MIN) firstFailEpoch = nowEpoch();
    persistSave();
    logf("[Net] Fehlzyklus %lu von %lu (%s)", (unsigned long)failCount, (unsigned long)OFFLINE_AFTER_FAILS,
         wifiFailed ? "WLAN" : "Server");
    if (failCount >= OFFLINE_AFTER_FAILS && !bannerShown) {
        showOfflineBanner(wifiFailed, false);
        bannerShown = 1;
        rememberShown("");
        persistSave();
    }
}


// ════════════════════════════════════════════════════════════════════════════
//  Statusbilder: Offline-Balken, Einrichtung, Reinigung
// ════════════════════════════════════════════════════════════════════════════

// Letztes Bild aus dem Flash in den Framebuffer laden (true bei Erfolg)
static bool loadLastImage(uint8_t* fb, int64_t& outEpoch) {
    outEpoch = 0;
    if (!g_storageOk || !FFat.exists(LAST_IMAGE_PATH)) return false;
    File f = FFat.open(LAST_IMAGE_PATH, FILE_READ);
    if (!f) return false;
    uint8_t header[EPD_HEADER_SIZE];
    bool ok = f.read(header, EPD_HEADER_SIZE) == EPD_HEADER_SIZE && memcmp(header, "PLX6", 4) == 0
           && f.read(fb, EPD_BUF_SIZE) == EPD_BUF_SIZE;
    f.close();
    if (!ok) return false;
    File m = FFat.open(LAST_META_PATH, FILE_READ);
    if (m) {
        String hash = m.readStringUntil('\n');
        String epochStr = m.readStringUntil('\n');
        m.close();
        outEpoch = atoll(epochStr.c_str());
    }
    return true;
}

// Erst in eine Temp-Datei, dann umbenennen: Strom weg mitten im Schreiben lässt
// das alte Bild stehen (vorher: abgeschnittene Datei, Offline-Balken auf Weiß).
static void saveLastImage(const uint8_t* payload, const char* hash) {
    if (!g_storageOk) return;
    uint32_t t = millis();
    File f = FFat.open(LAST_IMAGE_TMP, FILE_WRITE);
    if (!f) { logf("[WARN] Bild konnte nicht im Flash gespeichert werden"); return; }
    uint8_t header[EPD_HEADER_SIZE] = {'P', 'L', 'X', '6', 1, 4,
        (uint8_t)(EPD_WIDTH & 0xFF), (uint8_t)(EPD_WIDTH >> 8), (uint8_t)(EPD_HEIGHT & 0xFF), (uint8_t)(EPD_HEIGHT >> 8),
        (uint8_t)(EPD_BUF_SIZE & 0xFF), (uint8_t)((EPD_BUF_SIZE >> 8) & 0xFF), (uint8_t)((EPD_BUF_SIZE >> 16) & 0xFF), (uint8_t)((EPD_BUF_SIZE >> 24) & 0xFF),
        0, 0};
    bool ok = f.write(header, EPD_HEADER_SIZE) == EPD_HEADER_SIZE;
    // in Stücken schreiben, das ist auf FAT deutlich schneller als ein Riesenblock
    for (size_t off = 0; ok && off < EPD_BUF_SIZE; off += 32768) {
        size_t n = min((size_t)32768, EPD_BUF_SIZE - off);
        ok = f.write(payload + off, n) == n;
        feedWatchdog();
    }
    f.close();
    if (ok) {
        FFat.remove(LAST_IMAGE_PATH);
        ok = FFat.rename(LAST_IMAGE_TMP, LAST_IMAGE_PATH);
    } else {
        FFat.remove(LAST_IMAGE_TMP);
    }
    if (ok) {
        File m = FFat.open(LAST_META_PATH, FILE_WRITE);
        if (m) { m.printf("%s\n%lld\n", hash, (long long)nowEpoch()); m.close(); }
    }
    logf("[Flash] Bild %s (%lu ms)", ok ? "gesichert" : "NICHT gesichert", (unsigned long)(millis() - t));
}

// Was steht gerade auf dem Panel? Überlebt Stromausfall, damit nach dem
// Einschalten dasselbe Bild nicht noch einmal gezeichnet wird. "" = Statusbild
// (Offline-Balken, Reinigung, Einrichtung) – dann in jedem Fall neu zeichnen.
static void rememberShown(const char* hash) {
    if (!g_storageOk) return;
    File f = FFat.open(SHOWN_PATH, FILE_WRITE);
    if (!f) return;
    f.print(hash ? hash : "");
    f.close();
}

static String readShown() {
    if (!g_storageOk || !FFat.exists(SHOWN_PATH)) return String();
    File f = FFat.open(SHOWN_PATH, FILE_READ);
    if (!f) return String();
    String s = f.readStringUntil('\n');
    f.close();
    s.trim();
    return s;
}

// Schwarzer Balken oben auf dem letzten Bild (oder auf Weiß, wenn keins da ist)
static void showOfflineBanner(bool wifiFailed, bool test) {
    setPhase(PH_BANNER);
    uint8_t* fb = (uint8_t*)heap_caps_malloc(EPD_BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) { logf("[ERR] PSRAM fuer Offline-Bild fehlt"); return; }
    int64_t imageEpoch = 0;
    bool haveImage = loadLastImage(fb, imageEpoch);
    if (!haveImage) fbFill(fb, EPD_COLOR_WHITE);

    const int barH = 120;
    // Bild um die Balkenhoehe nach unten schieben: der Balken verdeckt so nicht die
    // Kopfzeile der Seite, sondern nur die (meist leeren) untersten Zeilen.
    memmove(fb + (size_t)barH * EPD_ROW_BYTES, fb, (size_t)(EPD_HEIGHT - barH) * EPD_ROW_BYTES);
    fbFillRect(fb, 0, 0, EPD_WIDTH, barH, EPD_COLOR_BLACK);
    String since = localTimeText(test ? nowEpoch() : firstFailEpoch);
    String line1 = String(test ? "Probe: " : "") + (wifiFailed ? "Kein WLAN" : "Keine Verbindung zum Server");
    if (since.length()) line1 += " seit " + since;
    fbDrawText(fb, 40, 14, line1.c_str(), FONT_L, EPD_COLOR_WHITE);

    String line2;
    String shownAt = localTimeText(imageEpoch);
    if (haveImage && shownAt.length()) line2 += "Anzeige vom " + shownAt + "  ·  ";
    else if (!haveImage) line2 += "Kein gespeichertes Bild  ·  ";
    line2 += String("Firmware ") + FIRMWARE_VERSION;
    if (!wifiFailed && WiFi.status() == WL_CONNECTED) line2 += "  ·  " + WiFi.localIP().toString();
    fbDrawText(fb, 40, 74, line2.c_str(), FONT_S, EPD_COLOR_WHITE);

    logf("[EPD] Offline-Hinweis: %s", line1.c_str());
    uint32_t t = millis();
    persistSave();
    EPD_Init();
    if (!EPD_Display(fb)) {
        g_panelFailed = true;
        logf("[ERR] Panel hat beim Offline-Hinweis nicht geantwortet (BUSY)");
    }
    g_refreshMs = millis() - t;
    heap_caps_free(fb);
}

// Weiße Seite mit Hinweis, wenn WLAN oder Server nicht konfiguriert sind
static void showSetupPage() {
    setPhase(PH_BANNER);
    uint8_t* fb = (uint8_t*)heap_caps_malloc(EPD_BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!fb) return;
    fbFill(fb, EPD_COLOR_WHITE);
    fbFillRect(fb, 0, 0, EPD_WIDTH, 120, EPD_COLOR_BLACK);
    fbDrawText(fb, 40, 14, "Inkwall – nicht eingerichtet", FONT_L, EPD_COLOR_WHITE);
    fbDrawText(fb, 40, 74, (String("Firmware ") + FIRMWARE_VERSION).c_str(), FONT_S, EPD_COLOR_WHITE);
    int y = 200;
    const char* lines[] = {
        "WLAN und Server-Adresse fehlen.",
        "In config.private.h eintragen:",
        "WIFI_SSID, WIFI_PASSWORD, SERVER_BASE_URL",
        "und die Firmware neu aufspielen.",
    };
    for (const char* l : lines) { fbDrawText(fb, 60, y, l, FONT_L, EPD_COLOR_BLACK); y += 70; }
    logf("[EPD] Einrichtungshinweis angezeigt");
    EPD_Init();
    EPD_Display(fb);
    heap_caps_free(fb);
    storedHash[0] = 0;
    rememberShown("");
    persistSave();
}

// Geisterbilder loswerden: einmal Schwarz, einmal Weiß, das Bild kommt danach neu
static bool runCleanCycle() {
    logf("[EPD] Reinigung: Schwarz, Weiss");
    uint32_t t = millis();
    EPD_Init();
    bool ok = EPD_Clear(EPD_COLOR_BLACK);
    feedWatchdog();
    ok = EPD_Clear(EPD_COLOR_WHITE) && ok;
    feedWatchdog();
    logf("[EPD] Reinigung %s (%lu ms)", ok ? "fertig" : "FEHLGESCHLAGEN - Panel antwortet nicht (BUSY)",
         (unsigned long)(millis() - t));
    return ok;
}


// ════════════════════════════════════════════════════════════════════════════
//  WiFi
// ════════════════════════════════════════════════════════════════════════════
bool hasUnsetConfig() {
    return strcmp(WIFI_SSID, "WIFI_SSID_HERE") == 0
        || strcmp(WIFI_PASSWORD, "WIFI_PASSWORD_HERE") == 0
        || strlen(WIFI_SSID) == 0
        || strlen(SERVER_BASE_URL) == 0
        || strlen(DEVICE_ID) == 0;
}

bool connectWiFi() {
    WiFi.mode(WIFI_STA);
    WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
    Serial.printf("[WiFi] Verbinde mit %s", WIFI_SSID);
    uint32_t t = millis();
    while (WiFi.status() != WL_CONNECTED) {
        if (millis() - t > WIFI_TIMEOUT_MS) { Serial.println(" TIMEOUT"); return false; }
        delay(300);
        Serial.print(".");
    }
    Serial.println();
    logf("[WiFi] OK IP=%s RSSI=%d dBm (%lu ms)",
         WiFi.localIP().toString().c_str(), WiFi.RSSI(), (unsigned long)(millis() - t));
    return true;
}


// ════════════════════════════════════════════════════════════════════════════
//  HTTP
// ════════════════════════════════════════════════════════════════════════════
// Geräte-Token (DEVICE_TOKEN in config.private.h, INKWALL_DEVICE_TOKEN am Server):
// ohne ihn liefert der Server die Firmware nicht aus und nimmt keine Rückmeldung an.
static void addDeviceToken(HTTPClient* http) {
    if (strlen(DEVICE_TOKEN)) http->addHeader("X-Inkwall-Token", DEVICE_TOKEN);
}

bool httpBegin(HTTPClient& http, const String& url, uint32_t timeoutMs) {
    http.setConnectTimeout(HTTP_CONNECT_TIMEOUT_MS);
    http.setTimeout(timeoutMs);
    if (!http.begin(url)) {
        logf("[HTTP] begin fehlgeschlagen fuer %s", url.c_str());
        return false;
    }
    addDeviceToken(&http);
    return true;
}

// Kurze Antworten (meta.json, hash): eigenes, kurzes Timeout. Ein Server, der die
// Verbindung annimmt, aber nicht antwortet, kostete sonst 2 × 60 s pro Anfrage.
String httpGetString(const String& url, uint32_t timeoutMs) {
    for (uint32_t attempt = 1; attempt <= HTTP_RETRY_COUNT; attempt++) {
        HTTPClient http;
        if (!httpBegin(http, url, timeoutMs)) {
            if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
            continue;
        }

        int    code = http.GET();
        String body;
        if (code == 200) {
            body = http.getString();
            http.end();
            return body;
        }

        logf("[HTTP] GET %s Versuch %lu/%d -> %d", url.c_str(), (unsigned long)attempt, HTTP_RETRY_COUNT, code);
        http.end();
        if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
    }

    return String();
}

bool httpGetBinary(const String& url, uint8_t* buf, size_t bufSize, size_t& outLen) {
    for (uint32_t attempt = 1; attempt <= HTTP_RETRY_COUNT; attempt++) {
        HTTPClient http;
        if (!httpBegin(http, url, HTTP_TIMEOUT_MS)) {
            if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
            continue;
        }

        int code = http.GET();
        if (code != 200) {
            logf("[HTTP] GET %s Versuch %lu/%d -> %d", url.c_str(), (unsigned long)attempt, HTTP_RETRY_COUNT, code);
            http.end();
            if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
            continue;
        }

        int contentLen = http.getSize();
        Serial.printf("[HTTP] Download %s  Content-Length=%d\n", url.c_str(), contentLen);
        if (contentLen > 0 && (size_t)contentLen > bufSize) {
            logf("[HTTP] Datei zu gross fuer Puffer (%d > %u)", contentLen, (unsigned)bufSize);
            http.end();
            return false;
        }

        WiFiClient* stream = http.getStreamPtr();
        outLen = 0;
        uint8_t chunk[4096];
        uint32_t lastProgressAt = millis();
        bool failed = false;

        while (http.connected() || stream->available()) {
            size_t avail  = stream->available();
            if (!avail) {
                if (millis() - lastProgressAt > HTTP_STREAM_IDLE_TIMEOUT_MS) {
                    logf("[HTTP] Stream-Timeout ohne weitere Daten");
                    failed = true;
                    break;
                }
                delay(1);
                continue;
            }
            size_t toRead = min(avail, sizeof(chunk));
            toRead        = min(toRead, bufSize - outLen);
            if (!toRead)  {
                logf("[HTTP] Buffer voll!");
                failed = true;
                break;
            }
            size_t rd = stream->readBytes(chunk, toRead);
            if (!rd) {
                logf("[HTTP] Stream lieferte 0 Bytes");
                failed = true;
                break;
            }
            memcpy(buf + outLen, chunk, rd);
            outLen += rd;
            lastProgressAt = millis();
            feedWatchdog();
            if (contentLen > 0 && (int)outLen >= contentLen) break;
        }

        http.end();
        logf("[HTTP] %u Bytes geladen", (unsigned)outLen);

        if (!failed && contentLen > 0 && (int)outLen != contentLen) {
            logf("[HTTP] Unvollstaendiger Download (%u/%d Bytes)", (unsigned)outLen, contentLen);
            failed = true;
        }

        if (!failed && outLen > 0) return true;

        logf("[HTTP] Download fehlgeschlagen, Versuch %lu/%d", (unsigned long)attempt, HTTP_RETRY_COUNT);
        if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
    }

    outLen = 0;
    return false;
}

bool httpPostJson(const String& url, const String& body, int& outCode, uint32_t timeoutMs) {
    for (uint32_t attempt = 1; attempt <= HTTP_RETRY_COUNT; attempt++) {
        HTTPClient http;
        if (!httpBegin(http, url, timeoutMs)) {
            if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
            continue;
        }

        http.addHeader("Content-Type", "application/json");
        outCode = http.POST(body);
        http.end();

        if (outCode >= 200 && outCode < 300) return true;

        Serial.printf("[HTTP] POST %s Versuch %lu/%d -> %d\n",
                      url.c_str(), (unsigned long)attempt, HTTP_RETRY_COUNT, outCode);
        if (attempt < HTTP_RETRY_COUNT) delay(HTTP_RETRY_DELAY_MS);
    }

    return false;
}


// ════════════════════════════════════════════════════════════════════════════
//  Meta
// ════════════════════════════════════════════════════════════════════════════
bool parsePositiveUIntField(const String& json, const char* key, uint32_t& outValue) {
    String quotedKey = String("\"") + key + "\"";
    int keyPos = json.indexOf(quotedKey);
    if (keyPos < 0) return false;

    int colonPos = json.indexOf(':', keyPos + quotedKey.length());
    if (colonPos < 0) return false;

    int valuePos = colonPos + 1;
    while (valuePos < json.length() && isspace((unsigned char)json[valuePos])) valuePos++;

    if (valuePos >= json.length() || !isDigit(json[valuePos])) return false;

    uint32_t value = 0;
    while (valuePos < json.length() && isDigit(json[valuePos])) {
        uint8_t digit = (uint8_t)(json[valuePos] - '0');
        if (value > (UINT32_MAX - digit) / 10U) return false;
        value = value * 10U + digit;
        valuePos++;
    }

    if (value == 0) return false;
    outValue = value;
    return true;
}

// Ganzzahl mit Vorzeichen, z. B. "tz_offset_sec": -3600
static bool parseIntField(const String& json, const char* key, int64_t& outValue) {
    String quotedKey = String("\"") + key + "\"";
    int keyPos = json.indexOf(quotedKey);
    if (keyPos < 0) return false;
    int colonPos = json.indexOf(':', keyPos + quotedKey.length());
    if (colonPos < 0) return false;
    int p = colonPos + 1;
    while (p < json.length() && isspace((unsigned char)json[p])) p++;
    bool neg = false;
    if (p < json.length() && json[p] == '-') { neg = true; p++; }
    if (p >= json.length() || !isDigit(json[p])) return false;
    int64_t v = 0;
    while (p < json.length() && isDigit(json[p])) { v = v * 10 + (json[p] - '0'); p++; }
    outValue = neg ? -v : v;
    return true;
}

bool parseBoolField(const String& json, const char* key, bool& outValue) {
    String quotedKey = String("\"") + key + "\"";
    int keyPos = json.indexOf(quotedKey);
    if (keyPos < 0) return false;
    int colonPos = json.indexOf(':', keyPos + quotedKey.length());
    if (colonPos < 0) return false;
    int p = colonPos + 1;
    while (p < json.length() && isspace((unsigned char)json[p])) p++;
    if (json.startsWith("true", p))  { outValue = true;  return true; }
    if (json.startsWith("false", p)) { outValue = false; return true; }
    return false;
}

// "key": "wert" – ohne Escapes, reicht fuer Hash, Pfade und Versionsnummern
bool parseStringField(const String& json, const char* key, String& outValue) {
    String quotedKey = String("\"") + key + "\"";
    int keyPos = json.indexOf(quotedKey);
    if (keyPos < 0) return false;
    int colonPos = json.indexOf(':', keyPos + quotedKey.length());
    if (colonPos < 0) return false;
    int openQuote = json.indexOf('"', colonPos + 1);
    if (openQuote < 0) return false;
    for (int i = colonPos + 1; i < openQuote; i++) {
        if (!isspace((unsigned char)json[i])) return false;
    }
    int closeQuote = json.indexOf('"', openQuote + 1);
    if (closeQuote < 0) return false;
    outValue = json.substring(openQuote + 1, closeQuote);
    return true;
}

bool fetchMeta(Meta& meta) {
    // sleep=from_meta: „ich ziehe die Zeit ab meta.json selbst ab“ – der Server
    // bestätigt das mit sleep_from=meta und rechnet dann nur die Zeit bis meta.json ein.
    String body = httpGetString(String(SERVER_BASE_URL) + "/meta.json?sleep=from_meta");
    if (body.isEmpty()) return false;
    g_metaAtMs = millis();

    if (!parseStringField(body, "hash", meta.hash) || !isHexHash(meta.hash)) {
        logf("[Meta] hash fehlt oder ist kein Hash");
        return false;
    }
    if (!parsePositiveUIntField(body, "next_wake_sec", meta.nextWakeSec)) {
        logf("[Meta] next_wake_sec fehlt oder ist ungueltig");
        meta.nextWakeSec = POLL_FALLBACK_SEC;
    }
    parseStringField(body, "format", meta.format);
    parseStringField(body, "epd_url", meta.epdUrl);
    parsePositiveUIntField(body, "epd_size", meta.epdSize);
    parseStringField(body, "firmware_version", meta.firmwareVersion);
    parseStringField(body, "firmware_md5", meta.firmwareMd5);
    parseStringField(body, "firmware_url", meta.firmwareUrl);
    parseBoolField(body, "firmware_force", meta.firmwareForce);
    String sleepFrom;
    meta.sleepFromMeta = parseStringField(body, "sleep_from", sleepFrom) && sleepFrom == "meta";
    g_sleepFromMeta = meta.sleepFromMeta;
    int64_t v;
    if (parseIntField(body, "epoch", v)) meta.epoch = v;
    if (parseIntField(body, "tz_offset_sec", v)) meta.tzOffsetSec = (int32_t)v;
    parseBoolField(body, "clean_due", meta.cleanDue);
    parseBoolField(body, "show_offline_test", meta.showOfflineTest);
    logf("[Meta] next_wake_sec=%lu%s epd=%s firmware=%s%s uhr=%s%s%s",
         (unsigned long)meta.nextWakeSec,
         meta.sleepFromMeta ? " (ab meta)" : "",
         meta.epdUrl.isEmpty() ? "nein" : "ja",
         meta.firmwareVersion.isEmpty() ? "-" : meta.firmwareVersion.c_str(),
         meta.firmwareForce ? " (erzwungen)" : "",
         meta.epoch > EPOCH_KNOWN_MIN ? "ja" : "nein",
         meta.cleanDue ? " reinigung=faellig" : "",
         meta.showOfflineTest ? " offline-probe" : "");
    return true;
}


// ════════════════════════════════════════════════════════════════════════════
//  OTA
// ════════════════════════════════════════════════════════════════════════════
bool performOta(const Meta& meta, const String& tryId) {
    String url = meta.firmwareUrl;
    if (url.startsWith("/")) url = String(SERVER_BASE_URL) + url;
    const esp_partition_t* target = esp_ota_get_next_update_partition(NULL);

    // Versuche je Datei zählen (NVS), damit eine kaputte Datei oder schwaches WLAN
    // nicht bei jedem Aufwachen 3 MB lädt und den App-Slot neu schreibt
    if (strcmp(otaMemory.tryId, tryId.c_str()) != 0 || otaMemory.tries >= OTA_MAX_TRIES) {
        strlcpy(otaMemory.tryId, tryId.c_str(), sizeof(otaMemory.tryId));
        otaMemory.tries = 0;     // andere Datei, oder die Sperrfrist ist um
    }
    otaMemory.tries++;
    otaMemory.triedAt = nowEpoch() > EPOCH_KNOWN_MIN ? nowEpoch() : 0;
    // Schon vor dem Einspielen merken: fällt der Strom zwischen Umschalten der
    // Bootpartition und dem Merken, bliebe die neue Firmware sonst ungeprüft.
    // Läuft nach dem Neustart nicht die Zielpartition, war das Update nicht aktiv.
    meta.firmwareVersion.toCharArray(otaMemory.targetVersion, sizeof(otaMemory.targetVersion));
    meta.firmwareMd5.toCharArray(otaMemory.targetMd5, sizeof(otaMemory.targetMd5));
    strlcpy(otaMemory.targetPart, target ? target->label : "", sizeof(otaMemory.targetPart));
    otaMemory.pendingVerify = 1;
    otaMemory.failedBoots = 0;
    otaMemory.rollbackReported = 0;
    otaMemory.rolledBack = 0;
    otaMemorySave();
    persistSave();

    // Erst melden, dann flashen: so weiss der Server, warum das Geraet gleich neu startet
    if (ACK_ENABLED) sendAck("ota", storedHash, meta.firmwareVersion.c_str());

    WiFiClient client;
    httpUpdate.rebootOnUpdate(false);
    httpUpdate.setLedPin(-1);
    uint32_t t = millis();
    feedWatchdog();
    t_httpUpdate_return ret = httpUpdate.update(client, url, FIRMWARE_VERSION, addDeviceToken);
    feedWatchdog();

    if (ret == HTTP_UPDATE_OK) {
        logf("[OTA] Firmware %s geschrieben (%lu ms, Versuch %u), Neustart", meta.firmwareVersion.c_str(),
             (unsigned long)(millis() - t), (unsigned)otaMemory.tries);
        Serial.flush();
        delay(200);
        ESP.restart();
        return true;   // nicht erreicht
    }
    // Nicht eingespielt: nichts zu prüfen, die laufende Firmware bleibt
    otaMemory.pendingVerify = 0;
    otaMemory.targetPart[0] = 0;
    otaMemorySave();
    if (ret == HTTP_UPDATE_NO_UPDATES) {
        logf("[OTA] Server meldet: kein Update");
        return false;
    }
    logf("[OTA] Fehlgeschlagen (Versuch %u von %u): %d %s", (unsigned)otaMemory.tries, (unsigned)OTA_MAX_TRIES,
         httpUpdate.getLastError(), httpUpdate.getLastErrorString().c_str());
    g_error = String("OTA fehlgeschlagen: ") + httpUpdate.getLastErrorString();
    return false;
}


// ════════════════════════════════════════════════════════════════════════════
//  Kompaktes Bild (PLX6, 4 bpp) → direkt ins Display
// ════════════════════════════════════════════════════════════════════════════

// Das Panel hat nicht geantwortet (BUSY blieb aktiv): Fehler melden statt „updated“.
// Zählt gegen eine frisch eingespielte Firmware (keine Bestätigung in diesem Zyklus).
static bool panelFailed() {
    g_panelFailed = true;
    g_error = "Panel antwortet nicht (BUSY-Timeout) - Kabel und Stromversorgung pruefen";
    logf("[ERR] %s", g_error.c_str());
    return false;
}
bool fetchAndDisplayEpd(const String& path, const char* hash) {
    String url = path.startsWith("/") ? String(SERVER_BASE_URL) + path : path;
    const size_t BUF_SIZE = EPD_HEADER_SIZE + EPD_BUF_SIZE + 64;
    uint8_t* buf = (uint8_t*)heap_caps_malloc(BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!buf) {
        logf("[ERR] PSRAM-Alloc fehlgeschlagen (%u KB)", (unsigned)(BUF_SIZE / 1024));
        g_error = "PSRAM zu klein";
        return false;
    }

    setPhase(PH_DOWNLOAD);
    uint32_t t = millis();
    size_t len = 0;
    if (!httpGetBinary(url, buf, BUF_SIZE, len)) {
        heap_caps_free(buf);
        g_error = "Download des kompakten Bildes fehlgeschlagen";
        return false;
    }
    g_downloadMs = millis() - t;

    auto le16 = [&](int o) -> uint16_t { return (uint16_t)buf[o] | ((uint16_t)buf[o + 1] << 8); };
    auto le32 = [&](int o) -> uint32_t {
        return (uint32_t)buf[o] | ((uint32_t)buf[o+1] << 8) | ((uint32_t)buf[o+2] << 16) | ((uint32_t)buf[o+3] << 24);
    };
    bool ok = len >= EPD_HEADER_SIZE && memcmp(buf, "PLX6", 4) == 0 && buf[4] == 1 && buf[5] == 4;
    uint16_t w = ok ? le16(6) : 0, h = ok ? le16(8) : 0;
    uint32_t payload = ok ? le32(10) : 0;
    if (!ok || w != EPD_WIDTH || h != EPD_HEIGHT || payload != EPD_BUF_SIZE || len < EPD_HEADER_SIZE + payload) {
        logf("[ERR] Kompaktes Bild ungueltig (magic=%s %ux%u payload=%lu len=%u)",
             ok ? "ok" : "falsch", w, h, (unsigned long)payload, (unsigned)len);
        heap_caps_free(buf);
        g_error = "Kompaktes Bild ungueltig";
        return false;
    }

    t = millis();
    feedWatchdog();
    persistSave();
    setPhase(PH_DISPLAY);
    EPD_Init();
    logf("[EPD] Sende kompaktes Bild an Display...");
    bool panelOk = EPD_Display(buf + EPD_HEADER_SIZE);
    g_refreshMs = millis() - t;
    feedWatchdog();
    g_imageFormat = "epd4";
    if (!panelOk) {
        heap_caps_free(buf);
        return panelFailed();
    }
    setPhase(PH_FLASH);
    saveLastImage(buf + EPD_HEADER_SIZE, hash);
    heap_caps_free(buf);
    logf("[EPD] Fertig!");
    return true;
}


// ════════════════════════════════════════════════════════════════════════════
//  BMP → Spectra 6 → Display (Fallback / alte Server)
// ════════════════════════════════════════════════════════════════════════════

// Device-RGB → 4-Bit-Farbcode des Spectra-6-Displays
uint8_t rgbToSpectra6(uint8_t r, uint8_t g, uint8_t b) {
    if (r ==   0 && g ==   0 && b ==   0) return EPD_COLOR_BLACK;
    if (r == 255 && g == 255 && b == 255) return EPD_COLOR_WHITE;
    if (r == 255 && g == 255 && b ==   0) return EPD_COLOR_YELLOW;
    if (r == 255 && g ==   0 && b ==   0) return EPD_COLOR_RED;
    if (r ==   0 && g ==   0 && b == 255) return EPD_COLOR_BLUE;
    if (r ==   0 && g == 255 && b ==   0) return EPD_COLOR_GREEN;
    return EPD_COLOR_WHITE;
}

bool parseBmpHeader(const uint8_t* buf, size_t bufLen,
                    uint32_t& pixelOffset, int32_t& width,
                    int32_t& height, uint16_t& bpp, uint32_t& rowStride) {
    if (bufLen < 54 || buf[0] != 'B' || buf[1] != 'M') return false;
    auto le32 = [&](int o) -> uint32_t {
        return (uint32_t)buf[o] | ((uint32_t)buf[o+1]<<8)
             | ((uint32_t)buf[o+2]<<16) | ((uint32_t)buf[o+3]<<24);
    };
    auto le16 = [&](int o) -> uint16_t {
        return (uint16_t)buf[o] | ((uint16_t)buf[o+1] << 8);
    };

    uint32_t dibHeaderSize = le32(14);
    uint16_t planes        = le16(26);
    pixelOffset = le32(10);
    width       = (int32_t)le32(18);
    height      = (int32_t)le32(22);   // positiv = bottom-up (Standard)
    bpp         = le16(28);
    uint32_t compression = le32(30);
    Serial.printf("[BMP] %dx%d  bpp=%d  offset=%u\n", width, abs(height), bpp, pixelOffset);

    if (dibHeaderSize < 40 || planes != 1 || bpp != 24 || compression != 0) return false;
    if (width <= 0 || height == 0) return false;
    if (width != EPD_WIDTH || abs(height) != EPD_HEIGHT) {
        logf("[BMP] Unerwartete Bildgroesse, erwartet %dx%d", EPD_WIDTH, EPD_HEIGHT);
        return false;
    }
    if ((width & 1) != 0) {
        logf("[BMP] Bildbreite muss gerade sein");
        return false;
    }

    uint64_t rowStride64 = ((uint64_t)width * 3ULL + 3ULL) & ~3ULL;
    uint64_t pixelDataEnd = (uint64_t)pixelOffset + rowStride64 * (uint64_t)abs(height);
    if (rowStride64 > UINT32_MAX || pixelDataEnd > bufLen) {
        logf("[BMP] Pixeldaten ausserhalb des Puffers");
        return false;
    }

    rowStride = (uint32_t)rowStride64;
    return true;
}

bool fetchAndDisplayBmp(const char* hash) {
    const size_t BMP_BUF_SIZE = (size_t)EPD_WIDTH * EPD_HEIGHT * 3 + 256;
    uint8_t* bmpBuf = (uint8_t*)heap_caps_malloc(BMP_BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!bmpBuf) {
        logf("[ERR] PSRAM-Alloc fehlgeschlagen (%u KB)", (unsigned)(BMP_BUF_SIZE / 1024));
        g_error = "PSRAM zu klein";
        return false;
    }

    setPhase(PH_DOWNLOAD);
    uint32_t t = millis();
    size_t bmpLen = 0;
    if (!httpGetBinary(String(SERVER_BASE_URL) + "/current.bmp",
                       bmpBuf, BMP_BUF_SIZE, bmpLen)) {
        heap_caps_free(bmpBuf);
        g_error = "BMP-Download fehlgeschlagen";
        return false;
    }
    g_downloadMs = millis() - t;

    uint32_t pixelOffset;
    int32_t  imgW, imgH;
    uint16_t bpp;
    uint32_t rowStride;
    if (!parseBmpHeader(bmpBuf, bmpLen, pixelOffset, imgW, imgH, bpp, rowStride)) {
        logf("[ERR] Ungueltiges BMP");
        heap_caps_free(bmpBuf);
        g_error = "BMP ungueltig";
        return false;
    }

    int32_t  absH      = abs(imgH);
    bool     bottomUp  = (imgH > 0);

    t = millis();
    setPhase(PH_DISPLAY);
    EPD_Init();

    uint8_t* epdBuf = (uint8_t*)heap_caps_malloc(EPD_BUF_SIZE, MALLOC_CAP_SPIRAM);
    if (!epdBuf) {
        logf("[ERR] PSRAM fuer EPD-Puffer nicht ausreichend");
        heap_caps_free(bmpBuf);
        g_error = "PSRAM zu klein";
        return false;
    }

    Serial.println("[EPD] Konvertiere BMP -> 4bpp Spectra-6...");
    for (int32_t row = 0; row < absH; row++) {
        int32_t bmpRow         = bottomUp ? (absH - 1 - row) : row;
        const uint8_t* src     = bmpBuf + pixelOffset + (size_t)bmpRow * rowStride;
        uint8_t*       dst     = epdBuf + (size_t)row * EPD_ROW_BYTES;

        for (int32_t col = 0; col < imgW; col += 2) {
            size_t px0 = (size_t)col * 3U;
            size_t px1 = (size_t)(col + 1) * 3U;
            uint8_t b0 = src[px0 + 0], g0 = src[px0 + 1], r0 = src[px0 + 2];
            uint8_t b1 = src[px1 + 0], g1 = src[px1 + 1], r1 = src[px1 + 2];
            dst[col/2] = (rgbToSpectra6(r0,g0,b0) << 4) | rgbToSpectra6(r1,g1,b1);
        }
    }
    heap_caps_free(bmpBuf);

    logf("[EPD] Sende BMP-Bild an Display...");
    feedWatchdog();
    persistSave();
    bool panelOk = EPD_Display(epdBuf);
    g_refreshMs = millis() - t;
    feedWatchdog();
    g_imageFormat = "bmp";
    if (!panelOk) {
        heap_caps_free(epdBuf);
        return panelFailed();
    }
    setPhase(PH_FLASH);
    saveLastImage(epdBuf, hash);

    heap_caps_free(epdBuf);
    logf("[EPD] Fertig!");
    return true;
}


// ════════════════════════════════════════════════════════════════════════════
//  ACK – Ergebnis, Gesundheitsdaten und Log
// ════════════════════════════════════════════════════════════════════════════
bool sendAck(const char* result, const char* hash, const char* fwTarget) {
    setPhase(PH_ACK);
    String body;
    body.reserve(g_log.length() + 512);
    body += "{\"device_id\":\""; body += jsonEscape(DEVICE_ID);
    body += "\",\"hash\":\"";    body += jsonEscape(hash);
    body += "\",\"result\":\"";  body += result;
    body += "\",\"fw_version\":\"" FIRMWARE_VERSION "\"";
    if (fwTarget && fwTarget[0]) { body += ",\"fw_target\":\""; body += jsonEscape(fwTarget); body += "\""; }
    body += ",\"rssi\":";          body += String(WiFi.RSSI());
    body += ",\"ip\":\"";          body += WiFi.localIP().toString(); body += "\"";
    body += ",\"boot_count\":";    body += String((unsigned long)bootCount);
    body += ",\"free_psram_kb\":"; body += String((unsigned long)(heap_caps_get_free_size(MALLOC_CAP_SPIRAM) / 1024));
    body += ",\"cycle_ms\":";      body += String((unsigned long)(millis() - g_cycleStart));
    if (g_downloadMs) { body += ",\"download_ms\":"; body += String((unsigned long)g_downloadMs); }
    if (g_refreshMs)  { body += ",\"refresh_ms\":";  body += String((unsigned long)g_refreshMs); }
    if (!g_imageFormat.isEmpty()) { body += ",\"image_format\":\""; body += g_imageFormat; body += "\""; }
    body += ",\"wake_reason\":\""; body += wakeReasonText(); body += "\"";
    body += ",\"reset_reason\":\""; body += resetReasonText(); body += "\"";
    if (g_lastPhase) {
        body += ",\"last_phase\":\""; body += g_lastPhase; body += "\"";
        body += ",\"last_phase_ms\":";   body += String((unsigned long)g_lastPhaseMs);
        body += ",\"last_phase_boot\":"; body += String((unsigned long)g_lastPhaseBoot);
    }
    if (g_metaAtMs) { body += ",\"meta_ms\":"; body += String((unsigned long)g_metaAtMs); }
    if (g_cleaned)        { body += ",\"cleaned\":1"; }
    if (g_offlineSeconds) { body += ",\"offline_s\":"; body += String((unsigned long)g_offlineSeconds); }
    if (!g_error.isEmpty()) { body += ",\"error\":\""; body += jsonEscape(g_error); body += "\""; }

    if (DEVICE_LOG_ENABLED && g_log.length()) {
        body += ",\"log\":[";
        int start = 0;
        bool first = true;
        while (start < (int)g_log.length()) {
            int nl = g_log.indexOf('\n', start);
            if (nl < 0) nl = g_log.length();
            if (nl > start) {
                if (!first) body += ',';
                body += '"'; body += jsonEscape(g_log.substring(start, nl)); body += '"';
                first = false;
            }
            start = nl + 1;
        }
        body += ']';
    }
    body += '}';

    int code = -1;
    bool ok = httpPostJson(String(SERVER_BASE_URL) + "/ack", body, code, ACK_TIMEOUT_MS);
    Serial.printf("[ACK] POST /ack (%s, %u Bytes) -> HTTP %d\n", result, (unsigned)body.length(), code);
    if (ok) {
        lastError[0] = 0;      // Server hat den Fehler des letzten Zyklus jetzt gesehen
        persistSave();
        g_log = "";
        g_cleaned = 0;
        g_offlineSeconds = 0;
    }
    return ok;
}


// ════════════════════════════════════════════════════════════════════════════
//  Deep Sleep
// ════════════════════════════════════════════════════════════════════════════
void goSleep(uint32_t seconds) {
    // Grenzen: ein Serverfehler darf das Gerät weder im Sekundentakt wecken
    // noch für Tage schlafen legen (dann hilft nur noch Stromtrennen)
    if (seconds < MIN_SLEEP_SEC) seconds = MIN_SLEEP_SEC;
    if (seconds > MAX_SLEEP_SEC) seconds = MAX_SLEEP_SEC;
    persistSave();
    Serial.printf("[Sleep] Deep Sleep fuer %lu s\n", (unsigned long)seconds);
    Serial.flush();
    EPD_Sleep();
    setPhase(PH_SLEEP);
    esp_sleep_enable_timer_wakeup((uint64_t)seconds * 1000000ULL);
    esp_deep_sleep_start();
}
