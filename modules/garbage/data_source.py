"""
Müllabfuhr-Datenquelle: liest einen oder mehrere ICS-Kalender (wie sie fast
jede Kommune anbietet), filtert die nächsten Abfuhrtermine und ordnet jeder
Tonne eine Farbe und ein Symbol zu.

Das Lesen der ICS-Dateien übernimmt app/ics.py (derselbe Parser wie beim
Kalender): so klappen auch Kommunen, die „alle zwei Wochen“ als Regel statt
als Einzeltermine schreiben, abgesagte Termine fallen weg, und ein Termin um
Mitternacht in UTC landet am richtigen Tag statt am Vortag.

Robustheit:
- Der letzte erfolgreiche Stand jeder URL liegt zusätzlich auf Platte
  (data/output/garbage_cache.json). Fällt die Kommune aus, zeigt das Display
  die alten Termine mit "Stand vom …" statt gar nichts.
- Antwortet ein Jahres-Kalender mit 404, merkt sich die Quelle das Jahr:
  im Januar erscheint dann ein Hinweis, statt still "keine Termine".
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from collections import Counter
from datetime import date, datetime, timedelta

from app import ics
from app.config import DATA_DIR, WEEKDAYS_DE_LONG, get_int_setting, get_setting, now_local
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

DEFAULT_CACHE_SECONDS = 6 * 3600
DEFAULT_DAYS_AHEAD = 14
DEFAULT_REMINDER_HOUR = 18      # ab dieser Uhrzeit am Vortag: Erinnerung + Vorrang in der Rotation
DEFAULT_DONE_HOUR = 12          # ab dieser Uhrzeit am Abfuhrtag gilt der Termin als erledigt
LOOKAHEAD_NEXT_YEAR_DAYS = 45   # so früh vor Jahresende auch den Folgejahres-Kalender laden
YEAR_PLACEHOLDER = "{year}"
HISTORY_DAYS = 730              # so weit zurück: Vorjahr fürs Erkennen verschobener Termine
LOOKAHEAD_DAYS = 400            # so weit voraus (Wiederholungsregeln haben oft kein Ende)
CACHE_FILE = DATA_DIR / "garbage_cache.json"

# Stichwort (kleingeschrieben, Teilstring) → Farbschlüssel. Reihenfolge zählt:
# spezifische Begriffe vor allgemeinen ("restmüll" vor "müll").
DEFAULT_TYPE_COLORS: tuple[tuple[str, str], ...] = (
    ("restmüll",   "black"),
    ("restmuell",  "black"),
    ("restabfall", "black"),
    ("hausmüll",   "black"),
    ("bio",        "green"),
    ("grün",       "green"),
    ("gruen",      "green"),
    ("garten",     "green"),
    ("gelb",       "yellow"),
    ("wertstoff",  "yellow"),
    ("verpack",    "yellow"),
    ("leichtverp", "yellow"),
    ("papier",     "blue"),
    ("pappe",      "blue"),
    ("karton",     "blue"),
    ("glas",       "green"),
    ("sperr",      "red"),
    ("schadstoff", "red"),
    ("problem",    "red"),
    ("weihnacht",  "green"),
    ("tannenbaum", "green"),
)
VALID_COLORS = ("black", "green", "yellow", "blue", "red")
COLOR_LABELS = {"black": "Schwarz", "green": "Grün", "yellow": "Gelb", "blue": "Blau", "red": "Rot"}

# Symbol je Tonnenart: Sack, Papierstapel, Sperrmüll-Sessel, Tannenbaum, sonst Tonne.
# Erkennbar auch ohne Farbe – Spectra-Gelb ist auf dem Panel blass.
ICON_KEYWORDS: tuple[tuple[str, str], ...] = (
    ("sack",       "sack"),
    ("sperr",      "bulky"),
    ("weihnacht",  "tree"),
    ("tannenbaum", "tree"),
    ("papier",     "paper"),
    ("pappe",      "paper"),
    ("karton",     "paper"),
)
ICON_LABELS = {"bin": "Tonne", "sack": "Sack", "paper": "Papierstapel", "bulky": "Sperrmüll", "tree": "Tannenbaum"}

_CACHE: dict[str, dict] = {}     # url → {"fetched_at", "last_attempt_at", "events", "missing"}
_LOCK = threading.Lock()
_DISK_LOADED = False


# ---------------------------------------------------------------------------
# Settings-Parsing
# ---------------------------------------------------------------------------

def parse_sources(raw: str) -> list[tuple[str, str]]:
    """
    'Label|URL; Label2|URL2' oder nur 'URL'. Trenner: Semikolon oder Zeilenumbruch.
    Gibt [(label, url), …] zurück. Label darf leer sein.
    """
    sources: list[tuple[str, str]] = []
    for chunk in re.split(r"[;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, url = chunk.split("|", 1)
            label, url = label.strip(), url.strip()
        else:
            label, url = "", chunk
        if url.lower().startswith(("http://", "https://")):
            sources.append((label, url))
    return sources


def parse_type_overrides(raw: str) -> list[tuple[str, str]]:
    """'restmüll=black, bio=green' → [(stichwort, farbe), …]. Unbekannte Farben werden ignoriert."""
    overrides: list[tuple[str, str]] = []
    for chunk in (raw or "").split(","):
        if "=" not in chunk:
            continue
        keyword, color = chunk.split("=", 1)
        keyword, color = keyword.strip().lower(), color.strip().lower()
        if keyword and color in VALID_COLORS:
            overrides.append((keyword, color))
    return overrides


def expand_year(url: str, year: int) -> str:
    return url.replace(YEAR_PLACEHOLDER, str(year))


def classify_type(summary: str, overrides: list[tuple[str, str]] | None = None) -> str:
    text = (summary or "").lower()
    for keyword, color in (overrides or []):
        if keyword in text:
            return color
    for keyword, color in DEFAULT_TYPE_COLORS:
        if keyword in text:
            return color
    return "black"


def classify_icon(summary: str) -> str:
    """'Gelber Sack' → Sack, 'Altpapiertonne' → Tonne (steht 'Tonne' drin, ist es eine), 'Altpapier' → Stapel."""
    text = (summary or "").lower()
    if "sack" in text:
        return "sack"
    if "tonne" in text:
        return "bin"
    for keyword, icon in ICON_KEYWORDS:
        if keyword in text:
            return icon
    return "bin"


# ---------------------------------------------------------------------------
# ICS lesen (app/ics.py)
# ---------------------------------------------------------------------------

def parse_ics_events(text: str, around: date | None = None) -> list[dict]:
    """
    Abfuhrtermine eines ICS-Kalenders, nach Datum sortiert:
    [{'date': date, 'summary': str, 'description': str, 'uid': str}, …].
    Wiederholungen werden im Zeitraum rund um `around` (Standard: heute)
    aufgelöst. Wirft ics.IcsError, wenn der Text kein Kalender ist.
    """
    around = around or now_local().date()
    calendar = ics.parse_calendar(text)
    found = ics.occurrences(calendar, around - timedelta(days=HISTORY_DAYS), around + timedelta(days=LOOKAHEAD_DAYS))
    return [
        {"date": ics.as_date(o["start"]), "summary": o["summary"], "description": o["description"], "uid": o["uid"]}
        for o in found
    ]


# ---------------------------------------------------------------------------
# Platten-Cache (letzter erfolgreicher Stand je URL)
# ---------------------------------------------------------------------------

def _events_to_json(events: list[dict]) -> list[dict]:
    return [{**ev, "date": ev["date"].isoformat()} for ev in events if isinstance(ev.get("date"), date)]


def _events_from_json(items: list) -> list[dict]:
    out: list[dict] = []
    for item in items or []:
        try:
            out.append({**item, "date": date.fromisoformat(str(item.get("date", "")))})
        except (TypeError, ValueError):
            continue
    return out


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
        log.warning(f"Müll-Cache nicht lesbar: {exc}")
        return
    for url, entry in (raw or {}).items():
        if url in _CACHE:
            continue
        _CACHE[url] = {
            "fetched_at": float(entry.get("fetched_at", 0.0)),
            "last_attempt_at": 0.0,
            "events": _events_from_json(entry.get("events")),
            "missing": False,
        }


def _save_disk_cache() -> None:
    try:
        payload = {
            url: {"fetched_at": entry["fetched_at"], "events": _events_to_json(entry["events"])}
            for url, entry in _CACHE.items() if entry.get("events")
        }
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except Exception as exc:
        log.warning(f"Müll-Cache nicht speicherbar: {exc}")


# ---------------------------------------------------------------------------
# Fetch + Cache pro URL
# ---------------------------------------------------------------------------

def fetch_ics_events(url: str, force_refresh: bool = False) -> list[dict] | None:
    """Events einer URL, gecacht. None nur, wenn nie erfolgreich geladen wurde."""
    cache_seconds = get_int_setting("GARBAGE_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 300, 7 * 86400)
    now = time.time()
    with _LOCK:
        _load_disk_cache()
        entry = _CACHE.get(url)
        if entry is not None and not force_refresh:
            if entry["events"] is not None and now - entry["fetched_at"] < cache_seconds:
                return list(entry["events"])
            # Fehlt der Jahreskalender (404), erst nach der Cache-Zeit wieder fragen –
            # die Kommune stellt ihn nicht im Minutentakt online
            backoff = cache_seconds if entry.get("missing") else FETCH_RETRY_BACKOFF_SECONDS
            if now - entry["last_attempt_at"] < backoff:
                return list(entry["events"]) if entry["events"] is not None else None
        if not network_allowed():
            return list(entry["events"]) if entry is not None and entry["events"] is not None else None
        _CACHE[url] = {
            "fetched_at": entry["fetched_at"] if entry else 0.0,
            "last_attempt_at": now,
            "events": entry["events"] if entry else None,
            "missing": bool(entry.get("missing")) if entry else False,
        }

    try:
        response = HTTP_SESSION.get(url, timeout=30, headers={"User-Agent": "Inkwall/0.2"})
        status = getattr(response, "status_code", 200)
        if status == 404:
            with _LOCK:
                _CACHE[url]["missing"] = True
            log.warning(f"Müllkalender nicht vorhanden (404): {url}")
            with _LOCK:
                cached = _CACHE[url]["events"]
            return list(cached) if cached is not None else None
        response.raise_for_status()
        events = parse_ics_events(response.text)
        with _LOCK:
            _CACHE[url] = {"fetched_at": now, "last_attempt_at": now, "events": events, "missing": False}
            _save_disk_cache()
        log.info(f"Müllkalender geladen: {len(events)} Termine")
        return list(events)
    except Exception as exc:
        log.warning(f"Müllkalender nicht ladbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            cached = _CACHE.get(url, {}).get("events")
        return list(cached) if cached is not None else None


def cache_info(url: str) -> dict:
    """{'fetched_at': float, 'missing': bool, 'failed': bool} für eine URL (0/False, wenn unbekannt)."""
    with _LOCK:
        entry = _CACHE.get(url) or {}
        fetched = float(entry.get("fetched_at", 0.0))
        return {
            "fetched_at": fetched,
            "missing": bool(entry.get("missing")),
            "failed": bool(entry) and float(entry.get("last_attempt_at", 0.0)) > fetched,
        }


def should_refresh_garbage() -> bool:
    """
    Neu rendern, wenn der Stand einer gerade geladenen URL abgelaufen ist.
    Nur die URLs aus den Einstellungen zählen (bei {year} das laufende und ggf.
    das nächste Jahr): der Vorjahres-Kalender oder eine entfernte Adresse wird
    nie mehr geladen und würde sonst ab dem Jahreswechsel jeden Durchlauf
    einen Neu-Render (und im Dashboard einen Panel-Refresh) auslösen.
    """
    cache_seconds = get_int_setting("GARBAGE_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 300, 7 * 86400)
    now = time.time()
    urls = active_urls()
    with _LOCK:
        for url in urls:
            entry = _CACHE.get(url)
            # Nie geladen (fehlt, 404): dafür holt fetch_content selbst nach, ein Render bringt nichts
            if entry is None or entry["events"] is None:
                continue
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                continue
            if now - entry["fetched_at"] >= cache_seconds:
                return True
    return False


def active_urls(today: date | None = None) -> list[str]:
    """Alle URLs, die gerade geladen werden: je Quelle eine, mit {year} je Jahr (laufendes, ab Mitte November auch das nächste)."""
    today = today or now_local().date()
    urls: list[str] = []
    for _, url in parse_sources(get_setting("GARBAGE_ICS_URLS", "")):
        if YEAR_PLACEHOLDER in url:
            urls.extend(expand_year(url, y) for y in _years_to_load(today))
        else:
            urls.append(url)
    return urls


def prune_cache(keep: list[str]) -> None:
    """Stände von URLs vergessen, die nicht mehr geladen werden (Vorjahr, entfernte Adresse) – auch auf Platte."""
    wanted = set(keep)
    with _LOCK:
        _load_disk_cache()
        stale = [url for url in _CACHE if url not in wanted]
        if not stale:
            return
        for url in stale:
            del _CACHE[url]
        _save_disk_cache()
    log.info(f"Müll-Cache: {len(stale)} nicht mehr genutzte Kalender entfernt")


def clear_cache() -> None:
    global _DISK_LOADED
    with _LOCK:
        _CACHE.clear()
        _DISK_LOADED = True   # Tests: nicht wieder von Platte laden


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


def usual_weekdays(all_events: list[dict]) -> dict[tuple[str, str], int]:
    """
    Üblicher Wochentag je Serie (Terminname + Adresse), aus allen Terminen der
    Datei – auch vergangenen. Nur, wenn mindestens vier Termine vorliegen und
    mindestens 60 % auf denselben Wochentag fallen; sonst kein Eintrag.
    """
    counts: dict[tuple[str, str], Counter] = {}
    for ev in all_events:
        d = ev.get("date")
        if not isinstance(d, date):
            continue
        counts.setdefault((ev.get("summary", ""), ev.get("label", "")), Counter())[d.weekday()] += 1
    result: dict[tuple[str, str], int] = {}
    for key, counter in counts.items():
        total = sum(counter.values())
        if total < 4:
            continue
        weekday, n = counter.most_common(1)[0]
        if n / total >= 0.6:
            result[key] = weekday
    return result


def build_garbage_content(all_events: list[dict], today: date, days_ahead: int,
                          overrides: list[tuple[str, str]] | None = None,
                          today_done: bool = False, reminder_active: bool = False) -> dict | None:
    """
    Gruppiert Termine nach Tag. 'days' enthält das Fenster [heute, heute+days_ahead];
    'next' ist der nächste Abfuhrtag – notfalls auch jenseits des Fensters (bis 1 Jahr),
    damit das Display nie leer bleibt.

    today_done: heutige Termine gelten als erledigt (Tonne ist geleert) und
    werden übersprungen. reminder_active: es ist Abend – ein Termin morgen ist
    dringend.
    """
    usual = usual_weekdays(all_events)
    seen: set[tuple[date, str, str]] = set()
    future: list[dict] = []
    for ev in all_events:
        d = ev.get("date")
        if not isinstance(d, date) or d < today or (d - today).days > 366:
            continue
        if today_done and d == today:
            continue
        key = (d, ev.get("summary", ""), ev.get("label", ""))
        if key in seen:
            continue
        seen.add(key)
        series = (ev.get("summary", ""), ev.get("label", ""))
        shifted = usual.get(series)
        future.append({
            "date": d,
            "in_days": (d - today).days,
            "summary": ev.get("summary", ""),
            "label": ev.get("label", ""),
            "color": classify_type(ev.get("summary", ""), overrides),
            "icon": classify_icon(ev.get("summary", "")),
            "shifted_from": WEEKDAYS_DE_LONG[shifted] if shifted is not None and shifted != d.weekday() else "",
        })
    if not future:
        return None

    future.sort(key=lambda e: (e["date"], e["summary"], e["label"]))

    def _days(events: list[dict]) -> list[dict]:
        by_day: dict[date, list[dict]] = {}
        for ev in events:
            by_day.setdefault(ev["date"], []).append(ev)
        return [
            {"date": d, "in_days": (d - today).days, "relative": relative_day_label((d - today).days), "events": evs}
            for d, evs in sorted(by_day.items())
        ]

    all_days = _days(future)
    window = [day for day in all_days if day["in_days"] <= days_ahead]
    nxt = all_days[0]
    urgent = nxt["in_days"] == 0 or (nxt["in_days"] == 1 and reminder_active)

    # Je Adresse: eigener nächster Termin (für die Spalten-Darstellung)
    labels = sorted({ev["label"] for ev in future})
    by_label: list[dict] = []
    if len(labels) > 1:
        for label in labels:
            days_l = _days([ev for ev in future if ev["label"] == label])
            by_label.append({
                "label": label or "Adresse",
                "next": days_l[0],
                "days": [d for d in days_l if d["in_days"] <= days_ahead],
            })

    # Erkannte Tonnenarten (für den Prüfen-Knopf)
    kinds: dict[str, dict] = {}
    for ev in all_events:
        summary = ev.get("summary", "")
        if summary and summary not in kinds:
            kinds[summary] = {"summary": summary, "color": classify_type(summary, overrides), "icon": classify_icon(summary)}

    return {
        "today": today.isoformat(),
        "days_ahead": days_ahead,
        "days": window,
        "next": nxt,
        "next_outside_window": bool(nxt["in_days"] > days_ahead),
        "urgent": urgent,
        "reminder": ("Heute rausstellen" if nxt["in_days"] == 0 else "Morgen rausstellen") if urgent else "",
        "by_label": by_label,
        "kinds": sorted(kinds.values(), key=lambda k: k["summary"]),
    }


def _years_to_load(today: date) -> list[int]:
    years = [today.year]
    if (date(today.year, 12, 31) - today).days <= LOOKAHEAD_NEXT_YEAR_DAYS:
        years.append(today.year + 1)
    return years


def fetch_garbage_content(force_refresh: bool = False) -> dict | None:
    sources = parse_sources(get_setting("GARBAGE_ICS_URLS", ""))
    if not sources:
        return None
    now = now_local()
    today = now.date()
    days_ahead = get_int_setting("GARBAGE_DAYS_AHEAD", DEFAULT_DAYS_AHEAD, 1, 90)
    overrides = parse_type_overrides(get_setting("GARBAGE_TYPE_COLORS", ""))
    reminder_hour = get_int_setting("GARBAGE_REMINDER_HOUR", DEFAULT_REMINDER_HOUR, 0, 23)
    done_hour = get_int_setting("GARBAGE_DONE_HOUR", DEFAULT_DONE_HOUR, 0, 23)
    cache_seconds = get_int_setting("GARBAGE_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 300, 7 * 86400)

    all_events: list[dict] = []
    any_loaded = False
    missing_years: set[int] = set()
    oldest_stale: float | None = None
    for label, url in sources:
        # Ohne {year}-Platzhalter gibt es nur eine URL; mit Platzhalter je Jahr eine
        per_year = [(y, expand_year(url, y)) for y in _years_to_load(today)] if YEAR_PLACEHOLDER in url else [(None, url)]
        for year, expanded in per_year:
            events = fetch_ics_events(expanded, force_refresh)
            info = cache_info(expanded)
            if info["missing"] and year is not None and events is None:
                missing_years.add(year)
            if events is None:
                continue
            any_loaded = True
            # Nur aus dem Platten-Cache bedient und deutlich abgelaufen → "Stand vom …" zeigen
            if info["failed"] and info["fetched_at"] and time.time() - info["fetched_at"] > 2 * cache_seconds:
                oldest_stale = info["fetched_at"] if oldest_stale is None else min(oldest_stale, info["fetched_at"])
            for ev in events:
                all_events.append({**ev, "label": label})

    if network_allowed():
        prune_cache(active_urls(today))

    if not any_loaded and not missing_years:
        return None

    content = build_garbage_content(
        all_events, today, days_ahead, overrides,
        today_done=now.hour >= done_hour,
        reminder_active=now.hour >= reminder_hour,
    )
    if content is None:
        if not missing_years:
            return None
        content = {
            "today": today.isoformat(), "days_ahead": days_ahead, "days": [], "next": None,
            "next_outside_window": False, "urgent": False, "reminder": "", "by_label": [], "kinds": [],
        }
    content["sources"] = [label or "Abfuhrkalender" for label, _ in sources]
    content["missing_years"] = sorted(missing_years)
    content["stale_since"] = (
        datetime.fromtimestamp(oldest_stale, tz=now.tzinfo).isoformat() if oldest_stale else ""
    )
    return content
