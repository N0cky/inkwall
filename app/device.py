"""
Gerätedienste für den ESP32: gehostete Firmware (Update über den Server),
Gesundheitsdaten aus dem ACK und das Gerätelog.

Firmware: Die Weboberfläche lädt die .bin hoch, der Server prüft, dass es ein
ESP32-S3-Image ist, liest die Versionsnummer aus dem eingebetteten Marker
"PLEXEINK_FW_VERSION=…", und bietet die Datei unter /firmware.bin an. Das
Gerät vergleicht beim Aufwachen die Version aus /meta.json mit seiner eigenen
und spielt bei Abweichung das Update ein (MD5-geprüft, mit Rollback).
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
from datetime import datetime, timezone

from app.config import DATA_DIR
from app.logger import LOGS_DIR, get_logger

log = get_logger(__name__)

FIRMWARE_DIR = DATA_DIR / "firmware"
FIRMWARE_BIN = FIRMWARE_DIR / "firmware.bin"
FIRMWARE_META = FIRMWARE_DIR / "firmware.json"
DEVICE_LOG_PATH = LOGS_DIR / "device.log"

VERSION_MARKER = b"INKWALL_FW_VERSION="
LEGACY_VERSION_MARKER = b"PLEXEINK_FW_VERSION="     # Firmware bis 1.2.x
ESP_IMAGE_MAGIC = 0xE9
ESP32_S3_CHIP_ID = 9
MAX_FIRMWARE_BYTES = 3 * 1024 * 1024        # App-Partition (app3M_fat9M_16MB)
MAX_LOG_LINES_PER_ACK = 60
MAX_LOG_LINE_CHARS = 200
DEVICE_LOG_KEEP_LINES = 2000

_lock = threading.Lock()


# ---------------------------------------------------------------------------
# Firmware
# ---------------------------------------------------------------------------

def inspect_firmware(data: bytes) -> dict:
    """Prüft ein hochgeladenes Image und liest Version und Prüfsummen. Wirft ValueError."""
    if len(data) < 64:
        raise ValueError("Die Datei ist zu klein für eine Firmware.")
    if data[0] != ESP_IMAGE_MAGIC:
        raise ValueError("Das ist kein ESP32-Firmware-Image (.bin aus dem Arduino-Build erwartet).")
    chip_id = int.from_bytes(data[12:14], "little")
    if chip_id != ESP32_S3_CHIP_ID:
        raise ValueError(f"Die Firmware ist für einen anderen Chip gebaut (Chip-ID {chip_id}, erwartet ESP32-S3).")
    if len(data) > MAX_FIRMWARE_BYTES:
        raise ValueError("Die Firmware ist größer als die App-Partition des Geräts (3 MB).")
    marker = VERSION_MARKER
    pos = data.find(marker)
    if pos < 0:
        marker = LEGACY_VERSION_MARKER
        pos = data.find(marker)
    if pos < 0:
        raise ValueError("Keine Versionsnummer gefunden. Die Firmware muss FIRMWARE_VERSION setzen (ab Sketch 1.1.0).")
    start = pos + len(marker)
    end = data.find(b"\0", start, start + 40)
    version = data[start:end if end > 0 else start + 40].decode("ascii", errors="replace").strip()
    if not re.fullmatch(r"[0-9A-Za-z.+-]{1,32}", version):
        raise ValueError(f"Versionsnummer unlesbar: {version!r}")
    return {
        "version": version,
        "size": len(data),
        "md5": hashlib.md5(data).hexdigest(),
        "sha256": hashlib.sha256(data).hexdigest(),
    }


def version_key(version: str) -> tuple[int, int, int]:
    """'1.3.0' → (1, 3, 0); Anhänge wie '-selftest' zählen nicht – wie in der Firmware."""
    match = re.match(r"\s*(\d+)(?:\.(\d+))?(?:\.(\d+))?", version or "")
    if not match:
        return (0, 0, 0)
    return tuple(int(part or 0) for part in match.groups())  # type: ignore[return-value]


def firmware_update_expected(fw: dict | None, device_version: str) -> bool:
    """
    Holt sich das Gerät die bereitgestellte Firmware? Ab 1.3.0 nur, wenn sie
    neuer ist – oder wenn sie beim Hochladen erzwungen wurde (Downgrade, Test-Build).
    Ältere Firmware nimmt jede andere Version.
    """
    if not fw or not device_version or fw.get("version") == device_version:
        return False
    if fw.get("force") or version_key(device_version) < (1, 3, 0):
        return True
    return version_key(fw["version"]) > version_key(device_version)


def store_firmware(data: bytes, force: bool = False) -> dict:
    info = inspect_firmware(data)
    info["force"] = bool(force)
    info["uploaded_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    with _lock:
        FIRMWARE_DIR.mkdir(parents=True, exist_ok=True)
        tmp = FIRMWARE_BIN.with_suffix(".bin.tmp")
        tmp.write_bytes(data)
        os.replace(tmp, FIRMWARE_BIN)
        FIRMWARE_META.write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info(f"Firmware {info['version']} bereitgestellt ({info['size']} Bytes, md5 {info['md5'][:8]}…)")
    return info


def firmware_info() -> dict | None:
    with _lock:
        if not FIRMWARE_BIN.exists() or not FIRMWARE_META.exists():
            return None
        try:
            info = json.loads(FIRMWARE_META.read_text(encoding="utf-8"))
        except Exception as exc:
            log.warning(f"firmware.json nicht lesbar: {exc}")
            return None
    if not isinstance(info, dict) or not info.get("version"):
        return None
    info["url"] = "/firmware.bin"
    return info


def delete_firmware() -> None:
    with _lock:
        for path in (FIRMWARE_BIN, FIRMWARE_META):
            try:
                path.unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------------------
# Gerätezustand (Panelreinigung, Test des Offline-Hinweises)
# ---------------------------------------------------------------------------

DEVICE_STATE_PATH = DATA_DIR / "device_state.json"
_test_banner_pending = False
# Lesen-Ändern-Schreiben von device_state.json am Stück. Mehrere Schreiber
# (ACK-Request, Benachrichtigungen im Worker, Panelreinigung) dürfen sich
# nicht gegenseitig mit einem alten Stand überschreiben.
_state_lock = threading.RLock()


def load_device_state() -> dict:
    try:
        data = json.loads(DEVICE_STATE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (FileNotFoundError, ValueError):
        return {}


def save_device_state(state: dict) -> None:
    with _lock:
        DEVICE_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        tmp = DEVICE_STATE_PATH.with_suffix(f".json.{threading.get_ident()}.tmp")
        tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(tmp, DEVICE_STATE_PATH)


def update_device_state(change) -> dict:
    """
    change(state) ändert den frisch gelesenen Zustand; gespeichert wird unter
    der Sperre. So gehen keine Felder verloren, die ein anderer Schreiber
    gerade gesetzt hat. Gibt den gespeicherten Zustand zurück.
    """
    with _state_lock:
        state = load_device_state()
        change(state)
        save_device_state(state)
        return state


def last_clean_at() -> datetime | None:
    raw = load_device_state().get("last_clean_at", "")
    try:
        return datetime.fromisoformat(raw) if raw else None
    except ValueError:
        return None


def mark_cleaned(now: datetime) -> None:
    update_device_state(lambda state: state.__setitem__("last_clean_at", now.isoformat()))


def clean_due(now: datetime, interval_days: int, hour: int) -> bool:
    """
    Fällig, wenn die Reinigung eingeschaltet ist, die Stunde passt und die letzte
    Reinigung mindestens interval_days (minus einer halben Stunde Toleranz) her ist.
    Ohne bisherige Reinigung: beim ersten Aufwachen in der passenden Stunde.
    """
    if interval_days <= 0 or now.hour != hour:
        return False
    last = last_clean_at()
    if last is None:
        return True
    if last.tzinfo is None and now.tzinfo is not None:
        last = last.replace(tzinfo=now.tzinfo)
    return (now - last).total_seconds() >= interval_days * 86400 - 1800


def request_test_banner() -> None:
    global _test_banner_pending
    _test_banner_pending = True


def test_banner_pending() -> bool:
    return _test_banner_pending


def consume_test_banner() -> bool:
    global _test_banner_pending
    was = _test_banner_pending
    _test_banner_pending = False
    return was


# ---------------------------------------------------------------------------
# ACK
# ---------------------------------------------------------------------------

_INT_FIELDS = ("rssi", "boot_count", "free_psram_kb", "cycle_ms", "download_ms", "refresh_ms", "cleaned", "offline_s",
               "meta_ms")
_STR_FIELDS = ("device_id", "hash", "fw_version", "result", "error", "image_format", "wake_reason", "reset_reason", "ip")

# Warum der Chip gestartet ist (Firmware ab 1.3.0). Nur die Abstürze sind ein Ereignis wert.
RESET_LABELS = {
    "poweron": "eingeschaltet",
    "external": "Reset-Taste",
    "restart": "Neustart (Firmware, z. B. nach einem Update)",
    "panic": "Absturz",
    "task_wdt": "Watchdog (eine Phase hing)",
    "int_wdt": "Watchdog (Interrupt)",
    "wdt": "Watchdog",
    "brownout": "Unterspannung",
    "deepsleep": "Aufwachen",
}
CRASH_RESETS = frozenset({"panic", "task_wdt", "int_wdt", "wdt", "brownout"})


def normalize_ack(body: dict, remote: str | None) -> dict:
    """Bekannte Felder mit sauberen Typen; unbekannte werden ignoriert."""
    ack: dict = {}
    for key in _STR_FIELDS:
        value = body.get(key)
        if value is not None and str(value).strip():
            ack[key] = str(value).strip()[:200]
    for key in _INT_FIELDS:
        value = body.get(key)
        try:
            if value is not None and value != "":
                ack[key] = int(value)
        except (TypeError, ValueError):
            continue
    ack.setdefault("result", "updated")
    ack["ack_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    ack["remote"] = remote or ""
    return ack


def rssi_quality(rssi: int | None) -> tuple[str, str]:
    """(Beschriftung, Zustand ok|warn|bad) für die Oberfläche."""
    if rssi is None:
        return "", ""
    if rssi >= -67:
        return "gut", "ok"
    if rssi >= -75:
        return "brauchbar", "ok"
    if rssi >= -82:
        return "schwach", "warn"
    return "sehr schwach", "bad"


# ---------------------------------------------------------------------------
# Gerätelog (serielle Ausgaben, die das Gerät mit dem ACK mitschickt)
# ---------------------------------------------------------------------------

def append_device_log(device_id: str, lines: list, received_at: str | None = None) -> int:
    clean: list[str] = []
    for line in list(lines)[-MAX_LOG_LINES_PER_ACK:]:
        text = str(line).replace("\r", "").replace("\n", " ").strip()
        if text:
            clean.append(text[:MAX_LOG_LINE_CHARS])
    if not clean:
        return 0
    stamp = received_at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    device = (device_id or "?").replace("\t", " ")
    with _lock:
        DEVICE_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with open(DEVICE_LOG_PATH, "a", encoding="utf-8") as fh:
            for text in clean:
                fh.write(f"{stamp}\t{device}\t{text}\n")
        _trim_device_log()
    return len(clean)


def _trim_device_log() -> None:
    try:
        lines = DEVICE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    if len(lines) > DEVICE_LOG_KEEP_LINES * 2:
        DEVICE_LOG_PATH.write_text("\n".join(lines[-DEVICE_LOG_KEEP_LINES:]) + "\n", encoding="utf-8")


def read_device_log(limit: int = 300) -> list[dict]:
    """Neueste Zeilen zuerst: [{ts, device, msg}, …]."""
    with _lock:
        try:
            lines = DEVICE_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
    entries: list[dict] = []
    for line in reversed(lines):
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        entries.append({"ts": parts[0], "device": parts[1], "msg": parts[2]})
        if len(entries) >= limit:
            break
    return entries


def clear_device_log() -> None:
    with _lock:
        try:
            DEVICE_LOG_PATH.unlink()
        except FileNotFoundError:
            pass
