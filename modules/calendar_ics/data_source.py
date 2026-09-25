"""
Kalender-Datenquelle: liest ICS-Kalender (Google, Nextcloud, iCloud, Outlook –
jeder Dienst bietet einen privaten ICS-Link) und liefert die Termine der
nächsten Tage, inklusive Wiederholungen.

Das Lesen der ICS-Dateien (Wiederholungen, verschobene und abgesagte
Einzeltermine, Zeitzonen) übernimmt app/ics.py – derselbe Parser wie bei der
Müllabfuhr.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta

from app import ics
from app.config import DATA_DIR, get_int_setting, get_setting, now_local
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

DEFAULT_CACHE_SECONDS = 900
DEFAULT_DAYS_AHEAD = 7
DEFAULT_MAX_EVENTS = 14
SOURCE_COLORS = ("blue", "green", "red", "yellow")   # Reihenfolge der Quellen → Farbe

CACHE_FILE = DATA_DIR / "calendar_cache.json"   # letzter guter Stand je Quelle (ICS-Text)

_CACHE: dict[str, dict] = {}     # url → {"fetched_at", "last_attempt_at", "calendar", "error", "text"}
_OCC_MEMO: dict[tuple, list] = {}  # (url, fetched_at, von, bis) → Termine – Auflösen kostet bei großen Kalendern ~0,1 s
_LOCK = threading.Lock()
_DISK_LOADED = False

# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def parse_sources(raw: str) -> list[tuple[str, str]]:
    """'Label|URL; URL' → [(label, url), …]. Wie beim Müll-Modul."""
    sources: list[tuple[str, str]] = []
    for chunk in re.split(r"[;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, url = (part.strip() for part in chunk.split("|", 1))
        else:
            label, url = "", chunk
        if url.lower().startswith(("http://", "https://", "webcal://")):
            if url.lower().startswith("webcal://"):
                url = "https://" + url[len("webcal://"):]
            sources.append((label, url))
    return sources


# ---------------------------------------------------------------------------
# Platten-Cache: der letzte gute ICS-Text je Quelle überlebt einen Neustart,
# damit ein Ausfall des Kalender-Servers keine leere Seite bringt.
# ---------------------------------------------------------------------------

def _load_disk_cache() -> None:
    """Einmal pro Prozess: gespeicherte Stände in den Speicher-Cache übernehmen (als abgelaufen)."""
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        if not CACHE_FILE.exists():
            return
        raw = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except Exception as exc:
        log.warning(f"Kalender-Cache nicht lesbar: {exc}")
        return
    for url, entry in (raw or {}).items():
        if url in _CACHE or not isinstance(entry, dict) or not entry.get("text"):
            continue
        try:
            calendar = ics.parse_calendar(str(entry["text"]))
        except Exception as exc:
            log.warning(f"Kalender-Cache für {url[:60]}… unbrauchbar: {exc}")
            continue
        _CACHE[url] = {
            "fetched_at": float(entry.get("fetched_at", 0.0)),
            "last_attempt_at": 0.0,
            "calendar": calendar,
            "error": "",
            "text": str(entry["text"]),
        }


def _save_disk_cache() -> None:
    try:
        payload = {
            url: {"fetched_at": entry["fetched_at"], "text": entry["text"]}
            for url, entry in _CACHE.items() if entry.get("text")
        }
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception as exc:
        log.warning(f"Kalender-Cache nicht speicherbar: {exc}")


def _describe_error(exc: Exception) -> str:
    """Kurze, menschenlesbare Fehlermeldung für Prüfen und Status."""
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if status:
        return f"HTTP {status}"
    text = str(exc).strip() or exc.__class__.__name__
    return text[:120]


# ---------------------------------------------------------------------------
# Fetch + Cache
# ---------------------------------------------------------------------------

def fetch_calendar(url: str, force_refresh: bool = False):
    """Geparster Kalender einer URL (gecacht). None nur, wenn nie erfolgreich geladen wurde."""
    cache_seconds = get_int_setting("CALENDAR_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 60, 86400)
    now = time.time()
    with _LOCK:
        _load_disk_cache()
        entry = _CACHE.get(url)
        if entry is not None and not force_refresh:
            if entry["calendar"] is not None and now - entry["fetched_at"] < cache_seconds:
                return entry["calendar"]
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                return entry["calendar"]
        if not network_allowed():
            return entry["calendar"] if entry is not None else None
        _CACHE[url] = {
            "fetched_at": entry["fetched_at"] if entry else 0.0,
            "last_attempt_at": now,
            "calendar": entry["calendar"] if entry else None,
            "error": entry.get("error", "") if entry else "",
            "text": entry.get("text", "") if entry else "",
        }
    try:
        response = HTTP_SESSION.get(url, timeout=30, headers={"User-Agent": "Inkwall/0.2"})
        response.raise_for_status()
        text = response.text
        calendar = ics.parse_calendar(text)
        with _LOCK:
            unchanged = entry is not None and entry.get("text") == text
            _CACHE[url] = {"fetched_at": now, "last_attempt_at": now, "calendar": calendar, "error": "", "text": text}
            if not unchanged:
                _save_disk_cache()        # große Google-Kalender nicht bei jedem Abruf neu schreiben
        log.info(f"Kalender geladen: {len(calendar.walk('VEVENT'))} Einträge{' (unverändert)' if unchanged else ''}")
        return calendar
    except Exception as exc:
        log.warning(f"Kalender nicht ladbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            _CACHE[url]["error"] = _describe_error(exc)
            return _CACHE[url]["calendar"]


def source_state(url: str) -> dict:
    """{'fetched_at': float, 'error': str, 'loaded': bool} für eine URL (leer, wenn unbekannt)."""
    with _LOCK:
        _load_disk_cache()
        entry = _CACHE.get(url) or {}
        return {
            "fetched_at": float(entry.get("fetched_at", 0.0)),
            "error": str(entry.get("error", "") or ""),
            "loaded": entry.get("calendar") is not None,
        }


def should_refresh_calendar() -> bool:
    """
    Neu rendern, wenn der Stand eines eingetragenen Kalenders abgelaufen ist.
    Nur die Quellen aus den Einstellungen zählen: ein entfernter Kalender wird
    nie mehr geladen und würde sonst jeden Durchlauf einen Neu-Render auslösen.
    """
    cache_seconds = get_int_setting("CALENDAR_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 60, 86400)
    now = time.time()
    urls = [url for _, url in parse_sources(get_setting("CALENDAR_ICS_URLS", ""))]
    with _LOCK:
        for url in urls:
            entry = _CACHE.get(url)
            if entry is None or entry["calendar"] is None:
                continue
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                continue
            if now - entry["fetched_at"] >= cache_seconds:
                return True
    return False


def prune_cache(keep: list[str]) -> None:
    """Stände entfernter Kalender vergessen – auch den privaten ICS-Text auf Platte."""
    wanted = set(keep)
    with _LOCK:
        _load_disk_cache()
        stale = [url for url in _CACHE if url not in wanted]
        if not stale:
            return
        for url in stale:
            del _CACHE[url]
        _save_disk_cache()
    log.info(f"Kalender-Cache: {len(stale)} nicht mehr eingetragene Kalender entfernt")


def clear_cache() -> None:
    global _DISK_LOADED
    with _LOCK:
        _CACHE.clear()
        _OCC_MEMO.clear()
        _DISK_LOADED = False


# ---------------------------------------------------------------------------
# Inhalt fürs Rendering
# ---------------------------------------------------------------------------

def relative_day_label(days: int) -> str:
    if days <= 0:
        return "Heute"
    if days == 1:
        return "Morgen"
    if days == 2:
        return "Übermorgen"
    return f"In {days} Tagen"


def _occurrences_for(url: str, calendar, fetched_at: float, start: date, end: date) -> list[dict]:
    """Termine eines Kalenders im Zeitraum – gemerkt, solange Stand und Zeitraum gleich bleiben."""
    key = (url, fetched_at, start, end)
    with _LOCK:
        hit = _OCC_MEMO.get(key)
    if hit is None:
        hit = ics.occurrences(calendar, start, end)
        with _LOCK:
            if len(_OCC_MEMO) > 32:
                _OCC_MEMO.clear()
            _OCC_MEMO[key] = hit
    return hit


def build_calendar_content(sources: list[tuple], now: datetime,
                           days_ahead: int, max_events: int, hide_past_today: bool = True) -> dict:
    """
    sources: [(label, color, Kalender oder schon aufgelöste Termine), …]. Gibt
    Tage mit Terminen zurück; 'heute' ist immer enthalten (ggf. leer), damit das
    Display eine Aussage macht.
    """
    today = now.date()
    window_end = today + timedelta(days=days_ahead)
    occurrences: list[dict] = []
    source_counts = [0] * len(sources)
    for index, (label, color, source) in enumerate(sources):
        found = source if isinstance(source, list) else ics.occurrences(source, today, window_end)
        for occ in found:
            if ics.as_date(occ["start"]) > window_end:
                continue
            occurrences.append({**occ, "label": label, "color": color})
            source_counts[index] += 1

    # Vergangene Termine von heute ausblenden (zeitgebunden und schon vorbei)
    if hide_past_today:
        occurrences = [
            o for o in occurrences
            if o["all_day"] or not isinstance(o["end"], datetime) or o["end"] > now
        ]

    occurrences.sort(key=ics._sort_key)

    by_day: dict[date, list[dict]] = {today: []}
    for o in occurrences:
        # Mehrtägige Termine auf jeden Tag im Fenster legen
        s_date = ics.as_date(o["start"])
        e_date = ics.as_date(o["end"])
        if o["all_day"] and e_date > s_date:
            e_date = e_date - timedelta(days=1)
        d = max(s_date, today)
        while d <= min(e_date, window_end):
            entry = dict(o)
            entry["continues"] = d != s_date
            entry["day"] = d
            by_day.setdefault(d, []).append(entry)
            d += timedelta(days=1)

    days: list[dict] = []
    remaining = max_events
    for d in sorted(by_day):
        evs = by_day[d]
        if d != today and not evs:
            continue
        if remaining <= 0 and d != today:
            break
        shown = evs[:max(0, remaining)] if d != today else evs[:max(1, remaining) if remaining > 0 else len(evs)]
        remaining -= len(shown)
        days.append({
            "date": d,
            "in_days": (d - today).days,
            "relative": relative_day_label((d - today).days),
            "events": shown,
            "hidden": max(0, len(evs) - len(shown)),
        })

    return {
        "today": today.isoformat(),
        "days_ahead": days_ahead,
        "days": days,
        "total_events": len(occurrences),
        "source_counts": source_counts,
    }


def fetch_calendar_content(force_refresh: bool = False) -> dict | None:
    sources = parse_sources(get_setting("CALENDAR_ICS_URLS", ""))
    if not sources:
        return None
    days_ahead = get_int_setting("CALENDAR_DAYS_AHEAD", DEFAULT_DAYS_AHEAD, 1, 60)
    max_events = get_int_setting("CALENDAR_MAX_EVENTS", DEFAULT_MAX_EVENTS, 1, 60)
    hide_past = get_setting("CALENDAR_HIDE_PAST_TODAY", "true").strip().lower() != "false"

    cache_seconds = get_int_setting("CALENDAR_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 60, 86400)
    now = now_local()
    if network_allowed():
        prune_cache([url for _, url in sources])
    today = now.date()
    window_end = today + timedelta(days=days_ahead)
    sources_events: list[tuple] = []
    loaded_index: list[int] = []
    oldest_stale = 0.0
    for index, (label, url) in enumerate(sources):
        calendar = fetch_calendar(url, force_refresh)
        if calendar is None:
            continue
        state = source_state(url)
        loaded_index.append(index)
        found = _occurrences_for(url, calendar, state["fetched_at"], today, window_end)
        sources_events.append((label, SOURCE_COLORS[index % len(SOURCE_COLORS)], found))
        # Quelle gerade nicht erreichbar und der Stand ist älter als ein Refresh → „Stand vom …“
        if state["error"] and state["fetched_at"] and time.time() - state["fetched_at"] > cache_seconds:
            oldest_stale = state["fetched_at"] if not oldest_stale else min(oldest_stale, state["fetched_at"])
    if not sources_events:
        return None

    content = build_calendar_content(sources_events, now, days_ahead, max_events, hide_past)
    counts = dict(zip(loaded_index, content.pop("source_counts", [])))
    content["sources"] = []
    for i, (label, url) in enumerate(sources):
        state = source_state(url)
        content["sources"].append({
            "label": label or f"Kalender {i + 1}",
            "color": SOURCE_COLORS[i % len(SOURCE_COLORS)],
            "count": counts.get(i, 0),
            "loaded": i in counts,
            "error": state["error"],
        })
    content["source_errors"] = [{"label": s["label"], "error": s["error"]} for s in content["sources"] if s["error"]]
    content["stale_since"] = (
        datetime.fromtimestamp(oldest_stale, tz=now.tzinfo).isoformat() if oldest_stale else ""
    )
    return content
