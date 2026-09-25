"""
Feiertage und Schulferien je Bundesland (openholidaysapi.org).

Einmal je Jahr und Art geladen, eine Woche lang gültig und auf Platte
gemerkt – die Daten ändern sich selten, und nach einem Neustart oder bei
einem Ausfall der Schnittstelle bleibt der letzte Stand erhalten. Geladen
wird nur, wo Netz erlaubt ist (app.http_client.network_allowed); sonst gilt,
was schon da ist.

Nutzer: der Kalender (Feiertage und Ferien als eigene Quelle), die
Müllabfuhr (Grund einer Verschiebung) und der Zeitplan (Fenster nur in den
Ferien bzw. nur außerhalb).
"""

from __future__ import annotations

import json
import os
import threading
import time
from datetime import date

from app.config import DATA_DIR, HOLIDAY_REGIONS, get_setting
from app.http_client import FETCH_RETRY_BACKOFF_SECONDS, HTTP_SESSION, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

API_URL = "https://openholidaysapi.org"
REFRESH_SECONDS = 7 * 86400
CACHE_FILE = DATA_DIR / "holidays_cache.json"
USER_AGENT = "Inkwall (+https://github.com/N0cky/inkwall)"
PUBLIC, SCHOOL = "public", "school"
_PATHS = {PUBLIC: "/PublicHolidays", SCHOOL: "/SchoolHolidays"}
_VALID_REGIONS = {code for code, _ in HOLIDAY_REGIONS if code}

# "DE-HE|public|2026" → {"fetched_at", "last_attempt_at", "items": [{"start", "end", "name"}, …]}
_CACHE: dict[str, dict] = {}
_LOCK = threading.Lock()
_DISK_LOADED = False


def configured_region() -> str:
    """Eingestelltes Bundesland (DE-HE …) oder "" (aus)."""
    code = get_setting("HOLIDAY_REGION", "").strip().upper()
    return code if code in _VALID_REGIONS else ""


def region_name(code: str) -> str:
    return next((name for c, name in HOLIDAY_REGIONS if c == code), code)


# ---------------------------------------------------------------------------
# Platte
# ---------------------------------------------------------------------------

def _load_disk() -> None:
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        data = json.loads(CACHE_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    if isinstance(data, dict):
        for key, entry in data.items():
            if isinstance(entry, dict) and isinstance(entry.get("items"), list):
                _CACHE.setdefault(key, {"fetched_at": float(entry.get("fetched_at", 0)), "last_attempt_at": 0.0,
                                        "items": entry["items"]})


def _save_disk() -> None:
    payload = {key: {"fetched_at": e["fetched_at"], "items": e["items"]} for key, e in _CACHE.items() if e.get("fetched_at")}
    try:
        CACHE_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = CACHE_FILE.with_name(CACHE_FILE.name + ".tmp")
        tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, CACHE_FILE)
    except OSError as exc:
        log.warning(f"Feiertage: Cache nicht speicherbar: {exc}")


# ---------------------------------------------------------------------------
# Laden
# ---------------------------------------------------------------------------

def _name(entry: dict) -> str:
    names = entry.get("name") or []
    for item in names:
        if isinstance(item, dict) and str(item.get("language", "")).upper() == "DE" and item.get("text"):
            return str(item["text"])
    return str(names[0].get("text", "")) if names and isinstance(names[0], dict) else ""


def parse_items(data: list, region: str) -> list[dict]:
    """Antwort der Schnittstelle → [{"start", "end", "name"}] (ISO-Daten, Ende einschließlich)."""
    items: list[dict] = []
    for entry in data if isinstance(data, list) else []:
        if not isinstance(entry, dict):
            continue
        # Nur landesweit gültige Tage: „Local“ (z. B. Augsburger Friedensfest) und Tage anderer Länder weglassen
        if entry.get("regionalScope") == "Local":
            continue
        codes = {str(s.get("code", "")) for s in entry.get("subdivisions") or [] if isinstance(s, dict)}
        if not entry.get("nationwide") and region not in codes:
            continue
        try:
            start = date.fromisoformat(str(entry.get("startDate")))
            end = date.fromisoformat(str(entry.get("endDate") or entry.get("startDate")))
        except ValueError:
            continue
        name = _name(entry)
        if name:
            items.append({"start": start.isoformat(), "end": max(start, end).isoformat(), "name": name})
    return items


def _fetch(region: str, kind: str, year: int) -> list[dict]:
    response = HTTP_SESSION.get(
        API_URL + _PATHS[kind],
        params={"countryIsoCode": "DE", "subdivisionCode": region, "languageIsoCode": "DE",
                "validFrom": f"{year}-01-01", "validTo": f"{year}-12-31"},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=10,
    )
    response.raise_for_status()
    return parse_items(response.json(), region)


def _year_items(region: str, kind: str, year: int) -> list[dict]:
    key = f"{region}|{kind}|{year}"
    now = time.time()
    with _LOCK:
        _load_disk()
        entry = _CACHE.get(key)
        fresh = entry is not None and now - entry["fetched_at"] < REFRESH_SECONDS
        waiting = entry is not None and now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS
        if fresh or waiting or not network_allowed():
            return list(entry["items"]) if entry else []
        entry = _CACHE.setdefault(key, {"fetched_at": 0.0, "last_attempt_at": 0.0, "items": []})
        entry["last_attempt_at"] = now
    try:
        items = _fetch(region, kind, year)
    except Exception as exc:
        log.warning(f"Feiertage ({region}, {kind} {year}) nicht ladbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            return list(_CACHE[key]["items"])
    with _LOCK:
        _CACHE[key].update(fetched_at=now, items=items)
        _save_disk()
    return list(items)


def between(kind: str, start: date, end: date, region: str | None = None) -> list[dict]:
    """
    Feiertage (kind="public") oder Ferien (kind="school"), die [start, end]
    berühren: [{"start": date, "end": date (einschließlich), "name"}], nach Beginn
    sortiert. Leer ohne Bundesland.
    """
    region = configured_region() if region is None else region
    if not region:
        return []
    seen: set[tuple] = set()
    result: list[dict] = []
    for year in range(start.year, end.year + 1):
        for item in _year_items(region, kind, year):
            try:
                s, e = date.fromisoformat(item["start"]), date.fromisoformat(item["end"])
            except (KeyError, ValueError):
                continue
            key = (s, e, item.get("name", ""))
            if e < start or s > end or key in seen:
                continue
            seen.add(key)
            result.append({"start": s, "end": e, "name": item.get("name", "")})
    result.sort(key=lambda i: (i["start"], i["end"]))
    return result


def school_holiday_on(day: date, region: str | None = None) -> dict | None:
    """Die Ferien, in die day fällt, sonst None."""
    return next(iter(between(SCHOOL, day, day, region)), None)


def public_holiday_on(day: date, region: str | None = None) -> dict | None:
    return next(iter(between(PUBLIC, day, day, region)), None)


def shift_reason(day: date, holidays: list[dict]) -> str:
    """
    Feiertag, der einen Abfuhrtermin verschoben hat: in derselben Kalenderwoche
    wie der verschobene Termin (danach rücken meist alle Termine der Woche einen
    Tag). "" ohne passenden Feiertag.
    """
    week = day.isocalendar()[:2]
    candidates = [h for h in holidays if h["start"].isocalendar()[:2] == week and abs((h["start"] - day).days) <= 6]
    if not candidates:
        return ""
    return min(candidates, key=lambda h: abs((h["start"] - day).days))["name"]


def clear_cache() -> None:
    """Für Tests."""
    global _DISK_LOADED
    with _LOCK:
        _CACHE.clear()
        _DISK_LOADED = True


def school_holiday_checker(windows=None):
    """
    Prüffunktion date → bool für den Zeitplan, oder None, wenn kein Bundesland
    eingestellt ist oder kein Fenster eine Ferien-Bedingung hat.
    """
    if windows is not None and not any(getattr(w, "school", "") for w in windows):
        return None
    region = configured_region()
    if not region:
        return None
    return lambda day: school_holiday_on(day, region) is not None
