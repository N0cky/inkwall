"""
Beobachtung: Zähler, ACK-Historie des Geräts, Benachrichtigung bei Ausfall
und die Prometheus-Ausgabe für /metrics.

- Zähler leben im Prozess (Renders, Render-Fehler, ACKs je Ergebnis).
- Die ACK-Historie ist eine JSONL-Datei unter data/output – ein kompakter
  Eintrag je Rückmeldung des Geräts (Zeit, Ergebnis, RSSI, Zyklusdauer …),
  auf ACK_HISTORY_KEEP Zeilen begrenzt. Daraus kommen die Kennzahlen der
  Gerät-Seite (Meldungen, längste Pause, Ø Zyklus, WLAN) und von /metrics.
- Benachrichtigung: meldet sich das Gerät länger als NOTIFY_OFFLINE_MINUTES
  nicht, geht genau eine Nachricht an NOTIFY_URL (ntfy-Topic, Discord- oder
  Slack-Webhook, sonst Text-POST); meldet es sich wieder, eine zweite.
  Der Merker liegt in device_state.json, damit ein Neustart nichts doppelt.
"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime, timezone

from app.config import APP_VERSION, DATA_DIR
from app.device import load_device_state, save_device_state
from app.http_client import HTTP_SESSION
from app.logger import get_logger

log = get_logger(__name__)

ACK_HISTORY_PATH = DATA_DIR / "ack_history.jsonl"
ACK_HISTORY_KEEP = 3000            # ≈ 10 Tage bei 5-Minuten-Zyklen
DEFAULT_EXPECTED_SECONDS = 300

_lock = threading.Lock()
_counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}


# ---------------------------------------------------------------------------
# Zähler
# ---------------------------------------------------------------------------

def increment(name: str, amount: int = 1, **labels: str) -> None:
    key = (name, tuple(sorted((k, str(v)) for k, v in labels.items())))
    with _lock:
        _counters[key] = _counters.get(key, 0) + amount


def counter_values() -> list[tuple[str, dict[str, str], int]]:
    with _lock:
        return [(name, dict(labels), value) for (name, labels), value in sorted(_counters.items())]


def reset_counters() -> None:
    with _lock:
        _counters.clear()


# ---------------------------------------------------------------------------
# ACK-Historie
# ---------------------------------------------------------------------------

_HISTORY_INT = ("rssi", "cycle_ms", "download_ms", "refresh_ms", "boot_count", "free_psram_kb", "offline_s")


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


def record_ack(ack: dict, hash_matches: bool | None = None) -> dict:
    """Kompakter Eintrag aus einem normalisierten ACK, angehängt an die Historie."""
    entry: dict = {
        "t": ack.get("ack_at") or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "result": str(ack.get("result", "updated"))[:20],
    }
    for key in _HISTORY_INT:
        value = ack.get(key)
        if isinstance(value, int):
            entry[key] = value
    if ack.get("fw_version"):
        entry["fw"] = str(ack["fw_version"])[:32]
    if ack.get("error"):
        entry["error"] = str(ack["error"])[:80]
    # Ungeplanter Neustart vor diesem Zyklus (Unterspannung, Absturz) und – ab Firmware 1.3.2 – der Schritt
    from app.device import CRASH_RESETS
    if ack.get("reset_reason") in CRASH_RESETS:
        entry["reset"] = str(ack["reset_reason"])[:20]
        if ack.get("last_phase"):
            entry["phase"] = str(ack["last_phase"])[:20]
    if hash_matches is not None:
        entry["match"] = bool(hash_matches)
    with _lock:
        try:
            ACK_HISTORY_PATH.parent.mkdir(parents=True, exist_ok=True)
            with open(ACK_HISTORY_PATH, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
            _trim_history()
        except OSError as exc:
            log.warning(f"ACK-Historie nicht speicherbar: {exc}")
    return entry


def _trim_history() -> None:
    try:
        lines = ACK_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    except FileNotFoundError:
        return
    if len(lines) > ACK_HISTORY_KEEP * 2:
        tmp = ACK_HISTORY_PATH.with_suffix(".jsonl.tmp")
        tmp.write_text("\n".join(lines[-ACK_HISTORY_KEEP:]) + "\n", encoding="utf-8")
        os.replace(tmp, ACK_HISTORY_PATH)


def read_ack_history(limit: int = 500, hours: float | None = None, now: datetime | None = None) -> list[dict]:
    """Chronologisch (älteste zuerst), höchstens limit Einträge, optional nur die letzten hours Stunden."""
    with _lock:
        try:
            lines = ACK_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
        except FileNotFoundError:
            return []
    now = now or datetime.now(timezone.utc)
    cutoff = now.timestamp() - hours * 3600 if hours else None
    entries: list[dict] = []
    for line in reversed(lines):
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        ts = _parse_ts(entry.get("t", ""))
        if ts is None:
            continue
        if cutoff is not None and ts.timestamp() < cutoff:
            break
        entries.append(entry)
        if len(entries) >= limit:
            break
    entries.reverse()
    return entries


def clear_ack_history() -> int:
    with _lock:
        try:
            count = len(ACK_HISTORY_PATH.read_text(encoding="utf-8", errors="replace").splitlines())
            ACK_HISTORY_PATH.unlink()
        except FileNotFoundError:
            return 0
    return count


def ack_stats(entries: list[dict], expected_seconds: int = DEFAULT_EXPECTED_SECONDS, now: datetime | None = None) -> dict:
    """
    Kennzahlen über eine Liste von Historien-Einträgen: Anzahl, Ergebnisse,
    längste Pause (auch bis jetzt), Pausen über dem Dreifachen des erwarteten
    Takts, Zyklusdauer und WLAN-Stärke.
    """
    now = now or datetime.now(timezone.utc)
    stamps = [ts for ts in (_parse_ts(e.get("t", "")) for e in entries) if ts is not None]
    results: dict[str, int] = {}
    for e in entries:
        results[e.get("result", "?")] = results.get(e.get("result", "?"), 0) + 1
    cycles = [e["cycle_ms"] for e in entries if isinstance(e.get("cycle_ms"), int)]
    rssis = [e["rssi"] for e in entries if isinstance(e.get("rssi"), int)]
    gaps = [int((b - a).total_seconds()) for a, b in zip(stamps, stamps[1:])]
    since_last = int((now - stamps[-1]).total_seconds()) if stamps else None
    longest = max(gaps + ([since_last] if since_last is not None else []), default=0)
    threshold = max(60, int(expected_seconds) * 3)
    return {
        "count": len(entries),
        "results": results,
        "errors": results.get("error", 0),
        "first": stamps[0].strftime("%Y-%m-%dT%H:%M:%SZ") if stamps else "",
        "last": stamps[-1].strftime("%Y-%m-%dT%H:%M:%SZ") if stamps else "",
        "since_last_s": since_last,
        "longest_gap_s": longest,
        "gaps_over_threshold": sum(1 for g in gaps if g > threshold) + (1 if since_last is not None and since_last > threshold else 0),
        "gap_threshold_s": threshold,
        "avg_cycle_ms": int(sum(cycles) / len(cycles)) if cycles else None,
        "max_cycle_ms": max(cycles) if cycles else None,
        "rssi_min": min(rssis) if rssis else None,
        "rssi_avg": int(sum(rssis) / len(rssis)) if rssis else None,
        "rssi_last": rssis[-1] if rssis else None,
        # Ungeplante Neustarts je Art und je Arbeitsschritt (Diagnose der Stromversorgung)
        "crashes": sum(1 for e in entries if e.get("reset")),
        "crash_reasons": _count(e.get("reset") for e in entries if e.get("reset")),
        "crash_phases": _count(e.get("phase") for e in entries if e.get("reset") and e.get("phase")),
    }


def _count(values) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in values:
        counts[v] = counts.get(v, 0) + 1
    return counts


# ---------------------------------------------------------------------------
# Benachrichtigung
# ---------------------------------------------------------------------------

def send_notification(url: str, title: str, message: str, tags: str = "", priority: str = "") -> bool:
    """
    Schickt eine Nachricht. ntfy: Text-POST mit Title/Tags/Priority-Headern.
    Discord- und Slack-Webhooks bekommen ihr JSON, alles andere den Text.
    """
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False
    try:
        lower = url.lower()
        if "discord.com/api/webhooks" in lower or "discordapp.com/api/webhooks" in lower:
            response = HTTP_SESSION.post(url, json={"content": f"**{title}**\n{message}"}, timeout=10)
        elif "hooks.slack.com" in lower:
            response = HTTP_SESSION.post(url, json={"text": f"*{title}*\n{message}"}, timeout=10)
        else:
            headers = {"Title": title, "Content-Type": "text/plain; charset=utf-8"}
            if tags:
                headers["Tags"] = tags
            if priority:
                headers["Priority"] = priority
            response = HTTP_SESSION.post(url, data=message.encode("utf-8"), headers=headers, timeout=10)
        status = getattr(response, "status_code", 0)
        if status >= 400:
            log.warning(f"Benachrichtigung abgelehnt (HTTP {status}): {url[:60]}")
            return False
        return True
    except Exception as exc:
        log.warning(f"Benachrichtigung nicht zustellbar: {exc}")
        return False


def _format_minutes(seconds: int) -> str:
    minutes = max(1, int(seconds) // 60)
    if minutes >= 120:
        return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"
    return f"{minutes} min"


def check_device_offline(last_ack: dict, notify_url: str, offline_minutes: int, now: datetime | None = None) -> str | None:
    """
    Vom Worker nach jedem Durchlauf aufgerufen. Meldet das Gerät einmal als
    ausgefallen, wenn die letzte Rückmeldung älter als offline_minutes ist.
    Rückgabe "offline", wenn gerade gemeldet wurde, sonst None.
    """
    if not notify_url or offline_minutes <= 0:
        return None
    ack_at = _parse_ts(str(last_ack.get("ack_at", "")))
    if ack_at is None:
        return None
    now = now or datetime.now(timezone.utc)
    age = int((now - ack_at).total_seconds())
    if age < offline_minutes * 60:
        return None
    state = load_device_state()
    if state.get("offline_notified_at"):
        return None
    from app.notifications import Notification, deliver
    device = last_ack.get("device_id") or "Das Display"
    ok = deliver(notify_url, Notification(
        "offline", f"{device} ausgefallen",
        f"{device} hat sich seit {_format_minutes(age)} nicht gemeldet (zuletzt {ack_at.astimezone():%H:%M}).",
        fields=[("Zuletzt gemeldet", f"{ack_at.astimezone():%d.%m. %H:%M}")], priority="high",
    ))
    state["offline_notified_at"] = now.strftime("%Y-%m-%dT%H:%M:%SZ")
    state["offline_notified_sent"] = ok
    save_device_state(state)
    return "offline"


def note_device_ack(ack: dict, notify_url: str, now: datetime | None = None) -> str | None:
    """
    Bei jeder Rückmeldung: war ein Ausfall gemeldet, geht die Entwarnung raus
    und der Merker wird gelöscht. Rückgabe "online", wenn gemeldet wurde.
    """
    state = load_device_state()
    notified_at = _parse_ts(str(state.get("offline_notified_at", "")))
    if not state.get("offline_notified_at"):
        return None
    state.pop("offline_notified_at", None)
    state.pop("offline_notified_sent", None)
    save_device_state(state)
    if not notify_url:
        return None
    from app.notifications import Notification, deliver
    now = now or datetime.now(timezone.utc)
    device = ack.get("device_id") or "Das Display"
    gone = f" – war rund {_format_minutes(int((now - notified_at).total_seconds()))} länger weg" if notified_at else ""
    deliver(notify_url, Notification("online", f"{device} ist wieder da", f"{device} meldet sich wieder{gone}."))
    return "online"


# ---------------------------------------------------------------------------
# Prometheus
# ---------------------------------------------------------------------------

def _label_value(value) -> str:
    return str(value).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _labels(labels: dict) -> str:
    if not labels:
        return ""
    return "{" + ",".join(f'{k}="{_label_value(v)}"' for k, v in labels.items()) + "}"


def prometheus_text(
    esp32_state: dict,
    last_ack: dict,
    next_wake: tuple[int, str],
    background_poll_seconds: int,
    modules: list[dict],
    schedule_state: dict | None = None,
    offline_minutes: int = 0,
    history_hours: float = 24,
    now: datetime | None = None,
) -> str:
    """Textformat für Prometheus. modules: [{id, enabled, state}] aus der Anzeige-API."""
    now = now or datetime.now(timezone.utc)
    lines: list[str] = []

    def metric(name: str, help_text: str, kind: str, samples: list[tuple[dict, object]]) -> None:
        if not samples:
            return
        lines.append(f"# HELP {name} {help_text}")
        lines.append(f"# TYPE {name} {kind}")
        for labels, value in samples:
            if value is None:
                continue
            if isinstance(value, bool):
                value = 1 if value else 0
            lines.append(f"{name}{_labels(labels)} {value}")

    metric("inkwall_info", "Serverversion", "gauge", [({"version": APP_VERSION}, 1)])

    counters = counter_values()
    for cname, help_text in (("inkwall_renders_total", "Erzeugte Bilder je Inhalt"),
                             ("inkwall_render_errors_total", "Fehlgeschlagene Renders je Inhalt"),
                             ("inkwall_acks_total", "Rückmeldungen des Geräts je Ergebnis"),
                             ("inkwall_notifications_total", "Verschickte Benachrichtigungen je Art")):
        metric(cname, help_text, "counter", [(labels, value) for name, labels, value in counters if name == cname])

    rendered = _parse_ts(str(esp32_state.get("rendered_at", "")))
    metric("inkwall_image_age_seconds", "Alter des aktuellen Bildes", "gauge",
           [({}, int((now - rendered).total_seconds()) if rendered else None)])
    metric("inkwall_image_current", "Aktueller Inhalt auf dem Display", "gauge",
           [({"module": esp32_state.get("media_type", "") or "none"}, 1)])
    metric("inkwall_next_wake_seconds", "Empfohlene Schlafdauer des Geräts", "gauge", [({}, int(next_wake[0]))])
    metric("inkwall_background_poll_seconds", "Prüfintervall des Servers", "gauge", [({}, int(background_poll_seconds))])

    ack_at = _parse_ts(str(last_ack.get("ack_at", "")))
    age = int((now - ack_at).total_seconds()) if ack_at else None
    device = {"device": last_ack.get("device_id") or "unknown"}
    if ack_at:
        metric("inkwall_device_last_ack_age_seconds", "Sekunden seit der letzten Rückmeldung", "gauge", [(device, age)])
        metric("inkwall_device_online", "1 = Rückmeldung jünger als die Ausfallschwelle", "gauge",
               [(device, 1 if (offline_minutes <= 0 or age < offline_minutes * 60) else 0)])
        metric("inkwall_device_hash_matches", "1 = Gerät zeigt das aktuelle Bild", "gauge",
               [(device, last_ack.get("hash", "") == esp32_state.get("hash", ""))])
        for key, name, help_text in (("rssi", "inkwall_device_rssi_dbm", "WLAN-Signal in dBm"),
                                     ("cycle_ms", "inkwall_device_cycle_ms", "Dauer des letzten Zyklus"),
                                     ("download_ms", "inkwall_device_download_ms", "Download-Dauer im letzten Zyklus"),
                                     ("refresh_ms", "inkwall_device_refresh_ms", "Anzeigedauer im letzten Zyklus"),
                                     ("boot_count", "inkwall_device_boot_count", "Aufwachzähler des Geräts"),
                                     ("free_psram_kb", "inkwall_device_free_psram_kb", "Freier PSRAM in KB")):
            value = last_ack.get(key)
            if isinstance(value, int):
                metric(name, help_text, "gauge", [(device, value)])
        if last_ack.get("fw_version"):
            metric("inkwall_device_firmware_info", "Firmware auf dem Gerät", "gauge",
                   [({**device, "version": last_ack["fw_version"]}, 1)])
        stats = ack_stats(read_ack_history(limit=5000, hours=history_hours, now=now), int(next_wake[0]) or DEFAULT_EXPECTED_SECONDS, now)
        window = {**device, "hours": str(int(history_hours))}
        metric("inkwall_device_acks", "Rückmeldungen im Zeitfenster", "gauge", [(window, stats["count"])])
        metric("inkwall_device_ack_errors", "Fehlermeldungen im Zeitfenster", "gauge", [(window, stats["errors"])])
        metric("inkwall_device_longest_gap_seconds", "Längste Pause ohne Rückmeldung im Zeitfenster", "gauge", [(window, stats["longest_gap_s"])])
        metric("inkwall_device_gaps_over_threshold", "Pausen über dem Dreifachen des Takts", "gauge", [(window, stats["gaps_over_threshold"])])
        if stats["avg_cycle_ms"] is not None:
            metric("inkwall_device_cycle_avg_ms", "Mittlere Zyklusdauer im Zeitfenster", "gauge", [(window, stats["avg_cycle_ms"])])
        if stats["rssi_min"] is not None:
            metric("inkwall_device_rssi_min_dbm", "Schwächstes WLAN-Signal im Zeitfenster", "gauge", [(window, stats["rssi_min"])])

    metric("inkwall_module_enabled", "1 = Inhalt im Programm eingeschaltet", "gauge",
           [({"module": m["id"]}, 1 if m.get("enabled") else 0) for m in modules])
    metric("inkwall_module_ready", "1 = Inhalt eingerichtet und ohne Fehler", "gauge",
           [({"module": m["id"], "state": m.get("state", "ready")}, 1 if m.get("state") == "ready" else 0) for m in modules])

    if schedule_state:
        window_obj = schedule_state.get("window")
        metric("inkwall_schedule_window_active", "1 = dieses Zeitfenster gilt gerade", "gauge",
               [({"window": window_obj.name}, 1)] if window_obj is not None else [({"window": "none"}, 0)])
        metric("inkwall_schedule_seconds_until_change", "Sekunden bis zur nächsten Fenstergrenze", "gauge",
               [({}, int(schedule_state.get("seconds_until_change", 0) or 0))])

    return "\n".join(lines) + "\n"
