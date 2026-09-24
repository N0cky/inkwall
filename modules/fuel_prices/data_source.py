"""
Tankpreise-Datenquelle: Tankerkönig (creativecommons.tankerkoenig.de), die
freie Schnittstelle zu den amtlichen Preisen der Markttransparenzstelle für
Kraftstoffe (MTS-K). Lizenz CC BY 4.0 – die Quellenangabe steht auf dem Bild.

Zwei Wege zu den Stationen:
- Umkreis um Koordinaten (list.php, bis 25 km)
- feste Stationen per ID (prices.php); Name und Adresse holt detail.php einmal
  und merkt sie sich auf Platte

Fair Use: nicht öfter als alle fünf Minuten abfragen – der Cache erzwingt das.
Bei jeder erfolgreichen Abfrage wandert der günstigste Preis je Kraftstoff in
die Historie (history.py).
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime

from app.config import DATA_DIR, get_int_setting, get_setting, get_settings_values, now_local
from app.http_client import FETCH_RETRY_BACKOFF_SECONDS, HTTP_SESSION, network_allowed
from app.logger import get_logger

from . import history

log = get_logger(__name__)

API_BASE = "https://creativecommons.tankerkoenig.de/json"
USER_AGENT = "Inkwall/0.2 (+https://github.com/N0cky/inkwall)"
ATTRIBUTION = "Daten: Tankerkönig.de / MTS-K · CC BY 4.0"

FUELS = (("e5", "Super E5"), ("e10", "Super E10"), ("diesel", "Diesel"))
FUEL_LABELS = dict(FUELS)
FUEL_SHORT = {"e5": "E5", "e10": "E10", "diesel": "Diesel"}
FUEL_KEYS = tuple(k for k, _ in FUELS)

SECTIONS = (
    ("day",   "Tagesverlauf"),
    ("week",  "7 Tage"),
    ("month", "30 Tage"),
    ("hours", "Uhrzeit-Profil"),
    ("stats", "Kennzahlen"),
)
SECTION_KEYS = tuple(k for k, _ in SECTIONS)

DEFAULT_RADIUS_KM = 5
DEFAULT_MAX_STATIONS = 6
DEFAULT_CACHE_SECONDS = 300
MIN_CACHE_SECONDS = 300        # Fair-Use-Regel von Tankerkönig
MAX_FIXED_STATIONS = 10
STATION_ID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")

STATIONS_FILE = DATA_DIR / "fuel_stations.json"

_CACHE: dict = {"fetched_at": 0.0, "last_attempt_at": 0.0, "stations": None, "error": "", "key": ""}
_DETAILS: dict[str, dict] = {}     # station id → {name, brand, street, place}
_DETAIL_FAILED: dict[str, float] = {}   # station id → Zeitpunkt des letzten Fehlschlags
DETAIL_RETRY_SECONDS = 3600        # eine unbekannte oder gerade nicht lieferbare Station nicht bei jedem Abruf erneut fragen
_LOCK = threading.Lock()
_DISK_LOADED = False


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

def api_key() -> str:
    return get_setting("FUEL_API_KEY", "").strip()


def parse_location(raw: str) -> tuple[float, float] | None:
    """'50.5556, 8.5045' → (lat, lng). Auch '50,5556 8,5045' mit Dezimalkomma. None, wenn unbrauchbar."""
    text = (raw or "").strip()
    if not text:
        return None
    parts = [p.strip(" ,;") for p in re.split(r"[;\s]+", text) if p.strip(" ,;")]
    if len(parts) == 1:      # ohne Leerzeichen: "50.5556,8.5045" oder "50,5556,8,5045"
        parts = [p.strip() for p in text.split(",") if p.strip()]
    if len(parts) == 4:      # Dezimalkomma ohne Leerzeichen: vier Teile durch das Komma
        parts = [f"{parts[0]}.{parts[1]}", f"{parts[2]}.{parts[3]}"]
    if len(parts) != 2:
        return None
    try:
        lat = float(parts[0].replace(",", "."))
        lng = float(parts[1].replace(",", "."))
    except ValueError:
        return None
    if not (-90 <= lat <= 90 and -180 <= lng <= 180):
        return None
    return (round(lat, 6), round(lng, 6))


def location() -> tuple[float, float] | None:
    return parse_location(get_setting("FUEL_LOCATION", ""))


def radius_km() -> int:
    return get_int_setting("FUEL_RADIUS_KM", DEFAULT_RADIUS_KM, 1, 25)


def parse_fuels(raw: str) -> tuple[str, ...]:
    chosen = tuple(p.strip() for p in (raw or "").split(",") if p.strip() in FUEL_LABELS)
    return chosen or ("e5", "diesel")


def fuel_types() -> tuple[str, ...]:
    return parse_fuels(get_setting("FUEL_TYPES", ""))


def primary_fuel() -> str:
    raw = get_setting("FUEL_PRIMARY", "").strip().lower()
    fuels = fuel_types()
    return raw if raw in fuels else fuels[0]


def parse_stations(raw: str) -> list[tuple[str, str]]:
    """'Aral Hermannsteiner|a1b2…; …' → [(label, id), …], nur gültige IDs, höchstens MAX_FIXED_STATIONS."""
    out: list[tuple[str, str]] = []
    for chunk in re.split(r"[;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        label, sid = (p.strip() for p in chunk.split("|", 1)) if "|" in chunk else ("", chunk)
        if STATION_ID.match(sid):
            out.append((label, sid.lower()))
    return out[:MAX_FIXED_STATIONS]


def fixed_stations() -> list[tuple[str, str]]:
    return parse_stations(get_setting("FUEL_STATIONS", ""))


def max_stations() -> int:
    return get_int_setting("FUEL_MAX_STATIONS", DEFAULT_MAX_STATIONS, 1, 12)


def parse_price(raw: str) -> float | None:
    text = (raw or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        return None
    return value if 0.5 <= value <= 5 else None


def alert_price() -> float | None:
    return parse_price(get_setting("FUEL_ALERT_PRICE", ""))


def parse_sections(raw: str, unset_means_all: bool = True) -> tuple[str, ...]:
    text = (raw or "").strip()
    if not text:
        return SECTION_KEYS if unset_means_all else ()
    return tuple(p.strip() for p in text.split(",") if p.strip() in SECTION_KEYS)


def enabled_sections() -> tuple[str, ...]:
    # Schlüssel nie gespeichert → alle Abschnitte; leer gespeichert → der Nutzer hat alle Haken entfernt
    raw = get_settings_values().get("FUEL_SECTIONS")
    return parse_sections(raw or "", unset_means_all=raw is None)


def cache_seconds() -> int:
    return get_int_setting("FUEL_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, MIN_CACHE_SECONDS, 3600)


def settings_key() -> str:
    """Fingerabdruck der Abfrage-Einstellungen: ändert er sich, ist der Cache hinfällig."""
    return json.dumps({"loc": location(), "rad": radius_km(), "ids": [s for _, s in fixed_stations()]}, sort_keys=True)


# ---------------------------------------------------------------------------
# Stationsdetails (feste Stationen) auf Platte
# ---------------------------------------------------------------------------

def _load_disk() -> None:
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        if STATIONS_FILE.exists():
            raw = json.loads(STATIONS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _DETAILS.update({k: v for k, v in raw.items() if isinstance(v, dict) and v.get("name")})
    except Exception as exc:
        log.warning(f"Tankpreise: gespeicherte Stationen nicht lesbar: {exc}")


def _save_disk() -> None:
    try:
        STATIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATIONS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_DETAILS, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STATIONS_FILE)
    except Exception as exc:
        log.warning(f"Tankpreise: Stationen nicht speicherbar: {exc}")


# ---------------------------------------------------------------------------
# Tankerkönig
# ---------------------------------------------------------------------------

def _get(endpoint: str, params: dict) -> dict:
    """GET mit API-Key; wirft bei HTTP-Fehlern und bei ok=false."""
    response = HTTP_SESSION.get(
        f"{API_BASE}/{endpoint}", params={**params, "apikey": api_key()},
        headers={"User-Agent": USER_AGENT}, timeout=25,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, dict) or not payload.get("ok", False):
        message = str((payload or {}).get("message") or "unbekannte Antwort") if isinstance(payload, dict) else "unbekannte Antwort"
        raise RuntimeError(f"Tankerkönig: {message}")
    return payload


def _price(value) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        price = float(value)
    except (TypeError, ValueError):
        return None
    return price if price > 0 else None


def _nice(value) -> str:
    """Tankerkönig liefert vieles in GROSSBUCHSTABEN – aufs Panel gehört 'Hermannsteiner Str.'."""
    text = str(value or "").strip()
    return text.title() if text.isupper() and len(text) > 3 else text


def parse_station(item: dict, label: str = "") -> dict | None:
    """Ein Stationsobjekt aus list.php oder detail.php → einheitliches dict."""
    if not isinstance(item, dict) or not item.get("id"):
        return None
    street = " ".join(p for p in (_nice(item.get("street")), str(item.get("houseNumber") or "").strip()) if p)
    name = _nice(item.get("brand") or item.get("name") or "Tankstelle")
    dist = item.get("dist")
    try:
        dist_km = round(float(dist), 1) if dist is not None else None
    except (TypeError, ValueError):
        dist_km = None
    return {
        "id": str(item["id"]).lower(),
        "label": label,
        "name": name,
        "full_name": str(item.get("name") or "").strip(),
        "street": street,
        "place": _nice(item.get("place")),
        "dist_km": dist_km,
        "is_open": bool(item.get("isOpen", True)),
        "prices": {f: _price(item.get(f)) for f in FUEL_KEYS},
    }


def search_radius(lat: float, lng: float, radius: int) -> list[dict]:
    """Alle Stationen im Umkreis mit allen Preisen (type=all verlangt sort=dist). Wirft bei Fehlern."""
    payload = _get("list.php", {"lat": lat, "lng": lng, "rad": radius, "sort": "dist", "type": "all"})
    stations = [parse_station(s) for s in payload.get("stations") or []]
    return [s for s in stations if s]


def station_detail(station_id: str, force: bool = False) -> dict | None:
    """Name und Adresse einer Station – einmal geholt, dann von Platte."""
    with _LOCK:
        _load_disk()
        if not force and station_id in _DETAILS:
            return dict(_DETAILS[station_id])
        failed_at = _DETAIL_FAILED.get(station_id)
        if not force and failed_at and time.time() - failed_at < DETAIL_RETRY_SECONDS:
            return None
    if not force and not network_allowed():
        return None
    try:
        payload = _get("detail.php", {"id": station_id})
        parsed = parse_station(payload.get("station") or {})
    except Exception as exc:
        log.warning(f"Tankpreise: Station {station_id} nicht abrufbar: {exc}")
        parsed = None
    if not parsed:
        with _LOCK:
            _DETAIL_FAILED[station_id] = time.time()
        return None
    detail = {k: parsed[k] for k in ("name", "full_name", "street", "place")}
    with _LOCK:
        _DETAIL_FAILED.pop(station_id, None)
        _DETAILS[station_id] = detail
        _save_disk()
    return dict(detail)


def fetch_fixed(stations: list[tuple[str, str]]) -> list[dict]:
    """Preise fester Stationen über prices.php, Details aus dem Merker. Wirft bei Fehlern."""
    ids = [sid for _, sid in stations]
    payload = _get("prices.php", {"ids": ",".join(ids)})
    prices = payload.get("prices") or {}
    out: list[dict] = []
    for label, sid in stations:
        entry = prices.get(sid) or prices.get(sid.upper()) or {}
        detail = station_detail(sid) or {"name": label or "Tankstelle", "full_name": "", "street": "", "place": ""}
        status = str(entry.get("status") or "").lower()
        out.append({
            "id": sid, "label": label, "name": label or detail["name"], "full_name": detail.get("full_name", ""),
            "street": detail.get("street", ""), "place": detail.get("place", ""), "dist_km": None,
            "is_open": status == "open",
            "prices": {f: _price(entry.get(f)) for f in FUEL_KEYS},
        })
    return out


# ---------------------------------------------------------------------------
# Cache und Historie
# ---------------------------------------------------------------------------

def fetch_stations(force_refresh: bool = False) -> dict:
    """
    {"stations": [...] | None, "error": str, "fetched_at": float}.
    stations None nur, wenn nie erfolgreich geladen wurde.
    """
    seconds = cache_seconds()
    key = settings_key()
    now = time.time()
    with _LOCK:
        if _CACHE["key"] != key:
            _CACHE.update({"fetched_at": 0.0, "last_attempt_at": 0.0, "stations": None, "error": "", "key": key})
        if not force_refresh:
            if _CACHE["stations"] is not None and now - _CACHE["fetched_at"] < seconds:
                return {k: _CACHE[k] for k in ("stations", "error", "fetched_at")}
            if now - _CACHE["last_attempt_at"] < min(FETCH_RETRY_BACKOFF_SECONDS, seconds):
                return {k: _CACHE[k] for k in ("stations", "error", "fetched_at")}
        if not network_allowed():
            return {k: _CACHE[k] for k in ("stations", "error", "fetched_at")}
        _CACHE["last_attempt_at"] = now
    try:
        fixed = fixed_stations()
        if fixed:
            stations = fetch_fixed(fixed)
        else:
            loc = location()
            if loc is None:
                raise RuntimeError("Standort fehlt")
            stations = search_radius(loc[0], loc[1], radius_km())
        with _LOCK:
            _CACHE.update({"fetched_at": now, "stations": stations, "error": ""})
        _record_history(stations, now_local())
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        message = f"HTTP {status}" if status else (str(exc).strip()[:120] or exc.__class__.__name__)
        log.warning(f"Tankpreise nicht ladbar: {message}")
        with _LOCK:
            _CACHE["error"] = message
    with _LOCK:
        return {k: _CACHE[k] for k in ("stations", "error", "fetched_at")}


def remember_radius_result(stations: list[dict]) -> bool:
    """
    Ergebnis einer Umkreissuche, die schon gemacht wurde (Verbindung prüfen),
    als aktuellen Stand übernehmen – statt dieselbe list.php-Abfrage gleich
    nochmal zu schicken. Nur ohne feste Stationen, da gilt genau diese Abfrage.
    """
    if fixed_stations() or location() is None:
        return False
    now = time.time()
    with _LOCK:
        _CACHE.update({"fetched_at": now, "last_attempt_at": now, "stations": stations, "error": "", "key": settings_key()})
    _record_history(stations, now_local())
    return True


def cheapest_per_fuel(stations: list[dict], fuels: tuple[str, ...] = FUEL_KEYS) -> dict[str, tuple[float, str]]:
    """Günstigster Preis je Kraftstoff unter den geöffneten Stationen: {fuel: (preis, name)}."""
    out: dict[str, tuple[float, str]] = {}
    for fuel in fuels:
        best = None
        for s in stations:
            price = s["prices"].get(fuel)
            if price is None or not s["is_open"]:
                continue
            if best is None or price < best[0]:
                best = (price, s["name"])
        if best:
            out[fuel] = best
    return out


def _record_history(stations: list[dict], now: datetime) -> None:
    try:
        history.record(now, cheapest_per_fuel(stations, fuel_types()))
    except Exception as exc:
        log.warning(f"Tankpreise: Historie nicht geschrieben: {exc}")


def should_refresh() -> bool:
    now = time.time()
    with _LOCK:
        if now - _CACHE["last_attempt_at"] < min(FETCH_RETRY_BACKOFF_SECONDS, cache_seconds()):
            return False
        return now - _CACHE["fetched_at"] >= cache_seconds()


def clear_cache() -> None:
    global _DISK_LOADED
    with _LOCK:
        _CACHE.update({"fetched_at": 0.0, "last_attempt_at": 0.0, "stations": None, "error": "", "key": ""})
        _DETAILS.clear()
        _DETAIL_FAILED.clear()
        _DISK_LOADED = False


# ---------------------------------------------------------------------------
# Inhalt fürs Rendering
# ---------------------------------------------------------------------------

def build_content(stations: list[dict], now: datetime, fuels: tuple[str, ...], primary: str, max_n: int,
                  alert: float | None, sections: tuple[str, ...], fetched_at: float = 0.0, error: str = "",
                  radius: int | None = None, fixed: bool = False, stats: dict | None = None) -> dict:
    """
    Sortiert nach dem Hauptkraftstoff (offen vor geschlossen, ohne Preis zuletzt),
    kappt auf max_n, markiert günstigste und teuerste Station, prüft den Preisalarm.
    """
    if primary not in fuels:
        primary = fuels[0]

    def sort_key(s: dict):
        price = s["prices"].get(primary)
        return (0 if s["is_open"] and price is not None else 1, price if price is not None else 99.0, s["dist_km"] or 0.0)

    ordered = sorted(stations, key=sort_key)[:max_n]
    priced = [s for s in ordered if s["is_open"] and s["prices"].get(primary) is not None]
    cheapest = priced[0] if priced else None
    priciest = priced[-1] if len(priced) > 1 else None
    rows = []
    for s in ordered:
        rows.append({**s, "is_cheapest": s is cheapest, "is_priciest": s is priciest})
    best = cheapest_per_fuel(ordered, fuels)
    saving_ct = round((priciest["prices"][primary] - cheapest["prices"][primary]) * 100, 1) if cheapest and priciest else None
    stale = ""
    if error and stations and fetched_at:
        stale = datetime.fromtimestamp(fetched_at, tz=now.tzinfo).isoformat()
    return {
        "now": now.isoformat(),
        "fuels": list(fuels),
        "primary": primary,
        "stations": rows,
        "total": len(stations),
        "cheapest": {f: {"price": p, "station": n} for f, (p, n) in best.items()},
        "saving_ct": saving_ct,
        "alert_price": alert,
        "alert": bool(alert is not None and cheapest and cheapest["prices"][primary] <= alert),
        "sections": list(sections),
        "radius_km": radius,
        "fixed": fixed,
        "error": error if not stations else "",
        "stale_since": stale,
        "attribution": ATTRIBUTION,
        "stats": stats or {},
    }


def fetch_fuel_content(force_refresh: bool = False) -> dict | None:
    if not api_key() or (location() is None and not fixed_stations()):
        return None
    result = fetch_stations(force_refresh)
    stations = result["stations"]
    if stations is None:
        return None
    now = now_local()
    fuels = fuel_types()
    primary = primary_fuel()
    sections = enabled_sections()
    stats = history.build_stats(primary, now) if sections else {}
    return build_content(stations, now, fuels, primary, max_stations(), alert_price(), sections,
                         fetched_at=result["fetched_at"], error=result["error"], radius=radius_km(),
                         fixed=bool(fixed_stations()), stats=stats)
