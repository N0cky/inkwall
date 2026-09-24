"""
DWD-Pollenflug-Datenabruf, -Parsing und -Caching.

Endpoint: https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json
Aktualisierung durch DWD: einmal täglich (~11 Uhr).
Cache-TTL: 6 Stunden (weit über dem DWD-Updatezyklus, spart Requests).

Region-Konfiguration "region_id:partregion_id", z. B. "90:92" für Hessen –
Rhein-Main. Länder ohne Teilregionen haben partregion_id -1 ("20:-1").
"90:-1" bei einem Land mit Teilregionen heißt: das ganze Land, je Allergen
und Tag der höchste Wert aller Teilregionen. Bis 0.2 standen die Teilregionen
als "92:-1" in der Liste – solche Werte werden weiter als Teilregion gelesen.
"""
from __future__ import annotations

import threading
import time
from datetime import datetime
from zoneinfo import ZoneInfo

from app.config import get_cfg, get_csv_setting, get_setting
from app.logger import get_logger
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS, network_allowed

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Konstanten
# ---------------------------------------------------------------------------

DWD_POLLEN_URL = "https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json"

DEFAULT_POLLEN_CACHE_SECONDS: int = 6 * 3600  # 6 h
POLLEN_UPDATE_DELAY_SECONDS: int = 15 * 60

# Roh-String → numerischer Mittelwert 0..3
_LOAD_MAP: dict[str, float] = {
    "0":   0.0,
    "0-1": 0.5,
    "1":   1.0,
    "1-2": 1.5,
    "2":   2.0,
    "2-3": 2.5,
    "3":   3.0,
}

# Alle von der DWD-API gelieferten Allergene (API-Schreibweise)
ALL_ALLERGENS: list[str] = [
    "Birke", "Esche", "Hasel", "Erle",
    "Graeser", "Roggen", "Beifuss", "Ambrosia",
]

# Anzeigenamen für die UI / das Bild
ALLERGEN_LABELS: dict[str, str] = {
    "Birke":    "Birke",
    "Esche":    "Esche",
    "Hasel":    "Hasel",
    "Erle":     "Erle",
    "Graeser":  "Gräser",
    "Roggen":   "Roggen",
    "Beifuss":  "Beifuß",
    "Ambrosia": "Ambrosia",
}

# ---------------------------------------------------------------------------
# Cache (Thread-sicher)
# ---------------------------------------------------------------------------

_POLLEN_CACHE: dict = {"fetched_at": 0.0, "last_attempt_at": 0.0, "region_key": "", "data": None, "next_refresh_at": 0.0,
                       "not_found_at": 0.0}
_POLLEN_LOCK  = threading.Lock()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def parse_load(raw: str | None) -> float | None:
    """Wandelt DWD-Laststring ("0", "1-2", …) in Float um. -1 → None."""
    if raw is None:
        return None
    s = str(raw).strip()
    if s == "-1":
        return None
    return _LOAD_MAP.get(s)


def parse_region_key(key: str) -> tuple[int, int] | None:
    """'90:92' → (90, 92), '20:-1' oder '20' → (20, -1). None bei Unsinn."""
    parts = str(key or "").strip().split(":")
    try:
        region_id = int(parts[0])
        partregion_id = int(parts[1]) if len(parts) > 1 and parts[1].strip() else -1
    except (ValueError, IndexError):
        return None
    return region_id, partregion_id


def _select_entries(content: list, region_id: int, partregion_id: int) -> list[dict]:
    """
    API-Einträge zur Auswahl: die Teilregion, sonst alle Einträge des Landes.
    Alte Werte bis 0.2 ("92:-1" für Rhein-Main) meinten die Teilregion 92.
    """
    entries = [e for e in content if isinstance(e, dict)]
    if partregion_id != -1:
        return [e for e in entries if e.get("region_id") == region_id and e.get("partregion_id") == partregion_id]
    whole = [e for e in entries if e.get("region_id") == region_id]
    if whole:
        return whole
    return [e for e in entries if e.get("partregion_id") == region_id]


def _find_region(content: list, region_id: int, partregion_id: int = -1) -> dict | None:
    """Erster passender API-Eintrag (für Aufrufer, die nur einen brauchen)."""
    entries = _select_entries(content, region_id, partregion_id)
    return entries[0] if entries else None


def _get_local_timezone() -> ZoneInfo:
    return ZoneInfo(get_cfg().timezone or "Europe/Berlin")


def _parse_pollen_update_timestamp(raw_value: str | None) -> datetime | None:
    if not raw_value:
        return None
    cleaned = str(raw_value).strip().replace(" Uhr", "")
    try:
        return datetime.strptime(cleaned, "%Y-%m-%d %H:%M").replace(tzinfo=_get_local_timezone())
    except ValueError:
        return None


def _compute_day_shift(last_update_dt: datetime | None) -> int:
    if last_update_dt is None:
        return 0
    now_local = datetime.now(_get_local_timezone())
    return max(0, min(2, (now_local.date() - last_update_dt.date()).days))


def _shift_pollen_days(days: dict[str, float | None], day_shift: int) -> dict[str, float | None]:
    ordered = [days.get("today"), days.get("tomorrow"), days.get("dayafter_to")]
    if day_shift > 0:
        ordered = ordered[day_shift:]
    while len(ordered) < 3:
        ordered.append(None)
    return {
        "today": ordered[0],
        "tomorrow": ordered[1],
        "dayafter_to": ordered[2],
    }


def _compute_next_refresh_at(next_update_dt: datetime | None) -> float:
    if next_update_dt is None:
        return 0.0
    return next_update_dt.timestamp() + POLLEN_UPDATE_DELAY_SECONDS


def _build_summary(entry: dict, selected: tuple[str, ...]) -> dict | None:
    """
    Baut das Pollen-Summary-Dict.
    selected leer → gibt None zurück (keine Anzeige gewünscht).
    """
    if not selected:
        return None                          # leere Auswahl = nichts anzeigen

    pollen_raw = entry.get("Pollen", {})
    allergens: dict[str, dict] = {}
    for allergen in selected:
        raw = pollen_raw.get(allergen)
        if raw is None:
            continue
        allergens[allergen] = {
            "today":       parse_load(raw.get("today")),
            "tomorrow":    parse_load(raw.get("tomorrow")),
            "dayafter_to": parse_load(raw.get("dayafter_to")),
        }
    return {
        "region_name":     entry.get("region_name", ""),
        "partregion_name": entry.get("partregion_name", ""),
        "allergens":       allergens,
    }


# ---------------------------------------------------------------------------
# Öffentliche API
# ---------------------------------------------------------------------------

def fetch_dwd_pollen(force_refresh: bool = False) -> dict | None:
    """
    Liefert Pollen-Zusammenfassung für die konfigurierte Region oder None.

    Gibt None zurück wenn:
    - keine Region konfiguriert
    - keine Allergene ausgewählt
    - Region nicht in API gefunden
    - Netzwerkfehler (sofern kein Cache vorhanden)
    """
    region_key = get_setting("DWD_POLLEN_REGION", "").strip()
    if not region_key:
        return None

    selected_allergens = get_csv_setting("DWD_POLLEN_ALLERGENS")
    if not selected_allergens:
        return None                          # nichts ausgewählt → kein Strip

    parsed = parse_region_key(region_key)
    if parsed is None:
        log.warning(f"Ungültige Region-Konfiguration: {region_key!r}")
        return None
    region_id, partregion_id = parsed

    with _POLLEN_LOCK:
        now = time.time()
        cached = _POLLEN_CACHE

        if (not force_refresh
                and cached["data"] is not None
                and cached["region_key"] == region_key
                and (now - cached["fetched_at"]) < DEFAULT_POLLEN_CACHE_SECONDS
                and not (cached.get("next_refresh_at", 0.0) and now >= cached["next_refresh_at"] and cached["fetched_at"] < cached["next_refresh_at"])):
            # Cache gültig – aber Allergen-Selektion könnte sich geändert haben,
            # daher nochmal filtern bevor wir zurückgeben.
            return _filter_cached(cached["data"], selected_allergens)

        # Backoff nach Fehlschlag oder "Region nicht gefunden" (ein Einstellungsfehler:
        # erst nach der Cache-Zeit wieder fragen, nicht alle 5 min den ganzen Feed laden)
        if (not force_refresh
                and cached["region_key"] == region_key
                and (now - cached["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS
                     or now - cached.get("not_found_at", 0.0) < DEFAULT_POLLEN_CACHE_SECONDS)):
            if cached.get("data"):
                return _filter_cached(cached["data"], selected_allergens)
            return None
        if not force_refresh and not network_allowed():
            if cached.get("data") and cached["region_key"] == region_key:
                return _filter_cached(cached["data"], selected_allergens)
            return None
        cached["last_attempt_at"] = now
        cached["region_key"] = region_key

        try:
            resp = HTTP_SESSION.get(DWD_POLLEN_URL, timeout=15)
            resp.raise_for_status()
            js = resp.json()
        except Exception as exc:
            log.warning(f"Pollen fetch-Fehler: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
            if cached.get("data"):
                return _filter_cached(cached["data"], selected_allergens)
            return None

        content = js.get("content", [])
        entries = _select_entries(content, region_id, partregion_id)
        if not entries:
            cached["not_found_at"] = now
            cached["data"] = None
            available = sorted({f"{e.get('region_id')}:{e.get('partregion_id')}" for e in content if isinstance(e, dict)})
            log.warning(f"Pollen-Region {region_key} nicht in der DWD-API gefunden. Verfügbar: {', '.join(available)} – "
                        f"nächster Versuch in {DEFAULT_POLLEN_CACHE_SECONDS // 3600} h")
            return None
        cached["not_found_at"] = 0.0
        entry = entries[0]

        last_update_dt = _parse_pollen_update_timestamp(js.get("last_update"))
        next_update_dt = _parse_pollen_update_timestamp(js.get("next_update"))
        day_shift = _compute_day_shift(last_update_dt)

        # Alle Allergen-Daten cachen (ohne Filterung → gilt für alle Allergen-Kombis)
        full_summary = {
            "region_name":     entry.get("region_name", ""),
            # Mehrere Teilregionen (ganzes Land): kein Teilregionsname, Werte sind die Höchstwerte
            "partregion_name": entry.get("partregion_name", "") if len(entries) == 1 else "",
            "allergens_all":   _extract_all_allergens(entries, day_shift),
            "last_update":     js.get("last_update"),
            "next_update":     js.get("next_update"),
            "day_shift":       day_shift,
        }
        _POLLEN_CACHE["fetched_at"] = now
        _POLLEN_CACHE["region_key"] = region_key
        _POLLEN_CACHE["data"]       = full_summary
        _POLLEN_CACHE["next_refresh_at"] = _compute_next_refresh_at(next_update_dt)
        return _filter_cached(full_summary, selected_allergens)


def _extract_all_allergens(entries: dict | list[dict], day_shift: int = 0) -> dict[str, dict]:
    """
    Alle Allergen-Daten (ungefiltert). Bei mehreren Einträgen (ganzes Land mit
    Teilregionen) je Allergen und Tag der höchste Wert – wer „Hessen“ wählt,
    soll die stärkere Belastung sehen, nicht zufällig die erste Teilregion.
    """
    if isinstance(entries, dict):
        entries = [entries]
    result: dict[str, dict] = {}
    for allergen in ALL_ALLERGENS:
        days: dict[str, float | None] = {}
        found = False
        for entry in entries:
            raw = (entry.get("Pollen") or {}).get(allergen)
            if raw is None:
                continue
            found = True
            for day in ("today", "tomorrow", "dayafter_to"):
                value = parse_load(raw.get(day))
                if value is not None and (days.get(day) is None or value > days[day]):
                    days[day] = value
                days.setdefault(day, None)
        if found:
            result[allergen] = _shift_pollen_days(days, day_shift)
    return result


def _filter_cached(full_summary: dict, selected: tuple[str, ...]) -> dict | None:
    """Filtert den Cache auf die aktuell ausgewählten Allergene."""
    if not selected:
        return None
    all_data = full_summary.get("allergens_all", {})
    filtered = {a: v for a, v in all_data.items() if a in selected}
    return {
        "region_name":     full_summary["region_name"],
        "partregion_name": full_summary["partregion_name"],
        "allergens":       filtered,
        "last_update":     full_summary.get("last_update"),
        "next_update":     full_summary.get("next_update"),
        "day_shift":       full_summary.get("day_shift", 0),
    }


def should_refresh_dwd_pollen() -> bool:
    with _POLLEN_LOCK:
        if not get_setting("DWD_POLLEN_REGION", "") or not get_csv_setting("DWD_POLLEN_ALLERGENS"):
            return False
        cached = _POLLEN_CACHE
        now = time.time()
        region_key = get_setting("DWD_POLLEN_REGION", "").strip()
        if cached["region_key"] == region_key and now - cached["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
            return False
        if cached["region_key"] == region_key and now - cached.get("not_found_at", 0.0) < DEFAULT_POLLEN_CACHE_SECONDS:
            return False
        if cached["data"] is None:
            return True
        next_refresh_at = cached.get("next_refresh_at", 0.0)
        return (
            (now - cached["fetched_at"]) >= DEFAULT_POLLEN_CACHE_SECONDS
            or (next_refresh_at and now >= next_refresh_at and cached["fetched_at"] < next_refresh_at)
        )
