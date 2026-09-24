"""
Kalender-Datenquelle: liest ICS-Kalender (Google, Nextcloud, iCloud, Outlook –
jeder Dienst bietet einen privaten ICS-Link) und liefert die Termine der
nächsten Tage, inklusive Wiederholungen.

Bewusst ohne externes Paket. Unterstützt, was private Kalender brauchen:
ganztägige und zeitgebundene Termine, Zeitzonen (TZID, UTC), mehrtägige
Termine, DURATION, RRULE mit FREQ=DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL,
COUNT, UNTIL, BYDAY (wöchentlich) sowie EXDATE.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.config import DATA_DIR, get_int_setting, get_setting, local_tz, now_local
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

DEFAULT_CACHE_SECONDS = 900
DEFAULT_DAYS_AHEAD = 7
DEFAULT_MAX_EVENTS = 14
MAX_OCCURRENCES_PER_EVENT = 500
SOURCE_COLORS = ("blue", "green", "red", "yellow")   # Reihenfolge der Quellen → Farbe

CACHE_FILE = DATA_DIR / "calendar_cache.json"   # letzter guter Stand je Quelle (ICS-Text)

_CACHE: dict[str, dict] = {}     # url → {"fetched_at", "last_attempt_at", "events", "error", "text"}
_LOCK = threading.Lock()
_DISK_LOADED = False

WEEKDAY_CODES = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


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
# ICS-Parsing
# ---------------------------------------------------------------------------

def unfold_ics_lines(text: str) -> list[str]:
    lines: list[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        else:
            lines.append(raw)
    return lines


def _unescape(value: str) -> str:
    return (value or "").replace("\\n", " ").replace("\\N", " ").replace("\\,", ",") \
        .replace("\\;", ";").replace("\\\\", "\\").strip()


def _split_property(line: str) -> tuple[str, dict[str, str], str] | None:
    """'DTSTART;TZID=Europe/Berlin:20260910T093000' → ('DTSTART', {'TZID': …}, '2026…')."""
    if ":" not in line:
        return None
    head, value = line.split(":", 1)
    parts = head.split(";")
    name = parts[0].upper()
    params: dict[str, str] = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def _tz_from_params(params: dict[str, str]):
    tzid = params.get("TZID")
    if not tzid:
        return None
    try:
        return ZoneInfo(tzid)
    except ZoneInfoNotFoundError:
        # Outlook schreibt gern eigene Namen ("W. Europe Standard Time") – dann lokal annehmen
        return None


def parse_ics_datetime(value: str, params: dict[str, str]):
    """
    Gibt date (ganztägig) oder tz-aware datetime in lokaler Zeit zurück, None bei Fehler.
    """
    value = (value or "").strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        try:
            return date(int(value[0:4]), int(value[4:6]), int(value[6:8]))
        except ValueError:
            return None
    match = re.fullmatch(r"(\d{4})(\d{2})(\d{2})T(\d{2})(\d{2})(\d{2})?(Z?)", value)
    if not match:
        return None
    y, mo, d, hh, mm, ss, zulu = match.groups()
    try:
        naive = datetime(int(y), int(mo), int(d), int(hh), int(mm), int(ss or 0))
    except ValueError:
        return None
    if zulu:
        aware = naive.replace(tzinfo=timezone.utc)
    else:
        aware = naive.replace(tzinfo=_tz_from_params(params) or local_tz())
    return aware.astimezone(local_tz())


_DURATION_RE = re.compile(r"^([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?$")


def parse_duration(value: str) -> timedelta | None:
    match = _DURATION_RE.match((value or "").strip())
    if not match:
        return None
    sign, weeks, days, hours, minutes, seconds = match.groups()
    delta = timedelta(weeks=int(weeks or 0), days=int(days or 0), hours=int(hours or 0),
                      minutes=int(minutes or 0), seconds=int(seconds or 0))
    return -delta if sign == "-" else delta


def parse_rrule(value: str) -> dict:
    rule: dict = {}
    for part in (value or "").split(";"):
        if "=" not in part:
            continue
        k, v = part.split("=", 1)
        rule[k.strip().upper()] = v.strip()
    return rule


def parse_ics_events(text: str) -> list[dict]:
    """
    Rohe VEVENTs: {'start', 'end', 'all_day', 'summary', 'location', 'uid',
    'rrule': dict|None, 'exdates': set}. Zeiten sind bereits lokal.
    """
    events: list[dict] = []
    current: dict | None = None
    for line in unfold_ics_lines(text):
        if line == "BEGIN:VEVENT":
            current = {"exdates": set(), "rrule": None, "summary": "", "location": ""}
            continue
        if line == "END:VEVENT":
            if current is not None and current.get("start") is not None:
                start = current["start"]
                all_day = isinstance(start, date) and not isinstance(start, datetime)
                end = current.get("end")
                if end is None and current.get("duration") is not None:
                    end = start + current["duration"]
                if end is None:
                    end = start + timedelta(days=1) if all_day else start
                current["end"] = end
                current["all_day"] = all_day
                current.pop("duration", None)
                if current.get("summary"):
                    events.append(current)
            current = None
            continue
        if current is None:
            continue
        prop = _split_property(line)
        if prop is None:
            continue
        name, params, value = prop
        if name == "DTSTART":
            current["start"] = parse_ics_datetime(value, params)
        elif name == "DTEND":
            current["end"] = parse_ics_datetime(value, params)
        elif name == "DURATION":
            current["duration"] = parse_duration(value)
        elif name == "SUMMARY":
            current["summary"] = _unescape(value)
        elif name == "LOCATION":
            current["location"] = _unescape(value)
        elif name == "UID":
            current["uid"] = value.strip()
        elif name == "RRULE":
            current["rrule"] = parse_rrule(value)
        elif name == "EXDATE":
            for chunk in value.split(","):
                parsed = parse_ics_datetime(chunk, params)
                if parsed is not None:
                    current["exdates"].add(parsed.date() if isinstance(parsed, datetime) else parsed)
        elif name == "STATUS" and value.strip().upper() == "CANCELLED":
            current["cancelled"] = True
    return [e for e in events if not e.get("cancelled")]


# ---------------------------------------------------------------------------
# Wiederholungen auflösen
# ---------------------------------------------------------------------------

def _as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _shift(value, days: int = 0, months: int = 0, years: int = 0):
    """Verschiebt date/datetime; ungültige Monatstage (31.02.) → None."""
    if months or years:
        total_months = value.month - 1 + months + years * 12
        y = value.year + total_months // 12
        m = total_months % 12 + 1
        try:
            return value.replace(year=y, month=m)
        except ValueError:
            return None
    return value + timedelta(days=days)


def expand_occurrences(event: dict, window_start: date, window_end: date) -> list[dict]:
    """Alle Vorkommen eines Events, die das Fenster [window_start, window_end] berühren."""
    start = event["start"]
    end = event["end"]
    duration = end - start
    rule = event.get("rrule")
    exdates = event.get("exdates") or set()

    def occurrence(s):
        e = s + duration
        return {"start": s, "end": e, "all_day": event["all_day"], "summary": event["summary"],
                "location": event.get("location", ""), "uid": event.get("uid", "")}

    def touches_window(s) -> bool:
        s_date = _as_date(s)
        e_date = _as_date(s + duration)
        if event["all_day"] and duration >= timedelta(days=1):
            e_date = e_date - timedelta(days=1)     # DTEND ganztägig ist exklusiv
        return s_date <= window_end and e_date >= window_start

    if not rule:
        return [occurrence(start)] if touches_window(start) else []

    freq = rule.get("FREQ", "").upper()
    try:
        interval = max(1, int(rule.get("INTERVAL", "1")))
    except ValueError:
        interval = 1
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    until = None
    if rule.get("UNTIL"):
        until_raw = parse_ics_datetime(rule["UNTIL"], {})
        until = _as_date(until_raw) if until_raw is not None else None
    hard_end = min(window_end, until) if until else window_end

    results: list[dict] = []
    produced = 0

    def emit(s) -> bool:
        """True → weiter, False → abbrechen (Grenze erreicht)."""
        nonlocal produced
        if _as_date(s) > hard_end:
            return False
        produced += 1
        if count is not None and produced > count:
            return False
        if _as_date(s) not in exdates and touches_window(s):
            results.append(occurrence(s))
        return True

    if freq == "WEEKLY":
        bydays = [WEEKDAY_CODES[d[-2:]] for d in rule.get("BYDAY", "").split(",") if d[-2:] in WEEKDAY_CODES]
        if not bydays:
            bydays = [start.weekday()]
        week_anchor = start - timedelta(days=start.weekday())
        for week in range(0, MAX_OCCURRENCES_PER_EVENT):
            base = week_anchor + timedelta(days=7 * interval * week)
            stop = False
            for wd in sorted(bydays):
                s = base + timedelta(days=wd)
                if s < start:
                    continue
                if not emit(s):
                    stop = True
                    break
            if stop or _as_date(base) > hard_end:
                break
        return results

    step_kwargs = {"DAILY": {"days": interval}, "MONTHLY": {"months": interval}, "YEARLY": {"years": interval}}.get(freq)
    if step_kwargs is None:
        return [occurrence(start)] if touches_window(start) else []

    current = start
    for n in range(0, MAX_OCCURRENCES_PER_EVENT):
        if current is not None and not emit(current):
            break
        if freq == "DAILY":
            current = _shift(start, days=interval * (n + 1))
        elif freq == "MONTHLY":
            current = _shift(start, months=interval * (n + 1))
        else:
            current = _shift(start, years=interval * (n + 1))
        if current is None:
            # 31.02. o. ä.: Vorkommen fällt aus, aber Zählung läuft weiter
            produced += 1
            if count is not None and produced >= count:
                break
            current = None
            # nächste Iteration versucht den Folgemonat
            continue
        if _as_date(current) > hard_end:
            break
    return results


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
            events = parse_ics_events(str(entry["text"]))
        except Exception as exc:
            log.warning(f"Kalender-Cache für {url[:60]}… unbrauchbar: {exc}")
            continue
        _CACHE[url] = {
            "fetched_at": float(entry.get("fetched_at", 0.0)),
            "last_attempt_at": 0.0,
            "events": events,
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

def fetch_ics_events(url: str, force_refresh: bool = False) -> list[dict] | None:
    """Events einer URL, gecacht. None nur, wenn nie erfolgreich geladen wurde."""
    cache_seconds = get_int_setting("CALENDAR_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 60, 86400)
    now = time.time()
    with _LOCK:
        _load_disk_cache()
        entry = _CACHE.get(url)
        if entry is not None and not force_refresh:
            if entry["events"] is not None and now - entry["fetched_at"] < cache_seconds:
                return list(entry["events"])
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                return list(entry["events"]) if entry["events"] is not None else None
        if not network_allowed():
            return list(entry["events"]) if entry is not None and entry["events"] is not None else None
        _CACHE[url] = {
            "fetched_at": entry["fetched_at"] if entry else 0.0,
            "last_attempt_at": now,
            "events": entry["events"] if entry else None,
            "error": entry.get("error", "") if entry else "",
            "text": entry.get("text", "") if entry else "",
        }
    try:
        response = HTTP_SESSION.get(url, timeout=30, headers={"User-Agent": "Inkwall/0.2"})
        response.raise_for_status()
        text = response.text
        events = parse_ics_events(text)
        with _LOCK:
            _CACHE[url] = {"fetched_at": now, "last_attempt_at": now, "events": events, "error": "", "text": text}
            _save_disk_cache()
        log.info(f"Kalender geladen: {len(events)} Einträge")
        return list(events)
    except Exception as exc:
        log.warning(f"Kalender nicht ladbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            _CACHE[url]["error"] = _describe_error(exc)
            cached = _CACHE[url]["events"]
        return list(cached) if cached is not None else None


def source_state(url: str) -> dict:
    """{'fetched_at': float, 'error': str, 'loaded': bool} für eine URL (leer, wenn unbekannt)."""
    with _LOCK:
        _load_disk_cache()
        entry = _CACHE.get(url) or {}
        return {
            "fetched_at": float(entry.get("fetched_at", 0.0)),
            "error": str(entry.get("error", "") or ""),
            "loaded": entry.get("events") is not None,
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
            if entry is None or entry["events"] is None:
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


def build_calendar_content(sources_events: list[tuple[str, str, list[dict]]], now: datetime,
                           days_ahead: int, max_events: int, hide_past_today: bool = True) -> dict:
    """
    sources_events: [(label, color, raw_events), …]. Gibt Tage mit Terminen zurück;
    'heute' ist immer enthalten (ggf. leer), damit das Display eine Aussage macht.
    """
    today = now.date()
    window_end = today + timedelta(days=days_ahead)
    occurrences: list[dict] = []
    source_counts = [0] * len(sources_events)
    for index, (label, color, events) in enumerate(sources_events):
        for ev in events:
            for occ in expand_occurrences(ev, today, window_end):
                occ["label"] = label
                occ["color"] = color
                occurrences.append(occ)
                source_counts[index] += 1

    # Vergangene Termine von heute ausblenden (zeitgebunden und schon vorbei)
    if hide_past_today:
        occurrences = [
            o for o in occurrences
            if o["all_day"] or not isinstance(o["end"], datetime) or o["end"] > now
        ]

    def sort_key(o):
        s = o["start"]
        return (_as_date(s), 0 if o["all_day"] else 1, s.timetuple()[3:5] if isinstance(s, datetime) else (0, 0), o["summary"])

    occurrences.sort(key=sort_key)

    by_day: dict[date, list[dict]] = {today: []}
    for o in occurrences:
        # Mehrtägige Termine auf jeden Tag im Fenster legen
        s_date = _as_date(o["start"])
        e_date = _as_date(o["end"])
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
    sources_events: list[tuple[str, str, list[dict]]] = []
    loaded_index: list[int] = []
    oldest_stale = 0.0
    for index, (label, url) in enumerate(sources):
        events = fetch_ics_events(url, force_refresh)
        if events is None:
            continue
        loaded_index.append(index)
        sources_events.append((label, SOURCE_COLORS[index % len(SOURCE_COLORS)], events))
        state = source_state(url)
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
