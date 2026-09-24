"""
Abfahrten-Datenquelle: Bahn und ÖPNV über eine transport.rest-Schnittstelle
(hafas-rest-api). Standard ist v6.db.transport.rest (Deutsche Bahn samt
Nahverkehr); jede andere Instanz mit demselben Format geht auch, etwa
v6.vbb.transport.rest für Berlin oder ein selbst gehosteter db-vendo-client.

Haltestellen werden als Nummer (IBNR / HAFAS-ID) oder als Name angegeben;
Namen löst das Modul einmal über /locations auf und merkt sich das Ergebnis
auf Platte. Abfahrten werden je Haltestelle kurz gecacht; fällt die Quelle
aus, bleibt der letzte Stand mit stale_since erhalten.
"""

from __future__ import annotations

import json
import os
import re
import threading
import time
from datetime import datetime, timedelta

from app.config import DATA_DIR, get_int_setting, get_setting, local_tz, now_local
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

DEFAULT_API_URL = "https://v6.db.transport.rest"
DEFAULT_CACHE_SECONDS = 120
DEFAULT_DURATION_MINUTES = 60
DEFAULT_MAX_PER_STOP = 8
MAX_STOPS = 3
NOT_FOUND_BACKOFF_SECONDS = 3600    # Name ohne Treffer: ein Tippfehler wird nicht alle 5 min neu gesucht
USER_AGENT = "Inkwall/0.2 (+https://github.com/N0cky/inkwall)"

PRODUCTS = (
    ("nationalExpress", "ICE"),
    ("national",        "IC/EC"),
    ("regionalExpress", "RE"),
    ("regional",        "RB"),
    ("suburban",        "S-Bahn"),
    ("subway",          "U-Bahn"),
    ("tram",            "Tram"),
    ("bus",             "Bus"),
    ("ferry",           "Fähre"),
)
PRODUCT_LABELS = {key: label for key, label in PRODUCTS}
ALL_PRODUCT_KEYS = tuple(key for key, _ in PRODUCTS)

STOPS_FILE = DATA_DIR / "departures_stops.json"

_CACHE: dict[str, dict] = {}       # stop_id → {"fetched_at", "last_attempt_at", "departures", "error", "name"}
_RESOLVED: dict[str, dict] = {}    # "api-basis|query (lower)" → {"id", "name"}; ältere Einträge nur "query"
_RESOLVE_FAILED: dict[str, tuple[float, float]] = {}   # Schlüssel wie _RESOLVED → (Zeitpunkt, Wartezeit)
_LOCK = threading.Lock()
_DISK_LOADED = False


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

def api_base_url() -> str:
    raw = get_setting("DEPARTURES_API_URL", "").strip().rstrip("/")
    return raw or DEFAULT_API_URL


def parse_stops(raw: str) -> list[tuple[str, str]]:
    """'Bahnhof|Wetzlar; 8006429' → [(label, query), …], höchstens MAX_STOPS."""
    stops: list[tuple[str, str]] = []
    for chunk in re.split(r"[;\n]+", raw or ""):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "|" in chunk:
            label, query = (p.strip() for p in chunk.split("|", 1))
        else:
            label, query = "", chunk
        if query:
            stops.append((label, query))
    return stops[:MAX_STOPS]


def selected_products() -> tuple[str, ...]:
    raw = get_setting("DEPARTURES_PRODUCTS", "").strip()
    if not raw:
        return ALL_PRODUCT_KEYS
    chosen = tuple(p.strip() for p in raw.split(",") if p.strip() in PRODUCT_LABELS)
    return chosen or ALL_PRODUCT_KEYS


# ---------------------------------------------------------------------------
# Haltestellen auflösen
# ---------------------------------------------------------------------------

def _load_disk() -> None:
    global _DISK_LOADED
    if _DISK_LOADED:
        return
    _DISK_LOADED = True
    try:
        if STOPS_FILE.exists():
            raw = json.loads(STOPS_FILE.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                _RESOLVED.update({k: v for k, v in raw.items() if isinstance(v, dict) and v.get("id")})
    except Exception as exc:
        log.warning(f"Abfahrten: gespeicherte Haltestellen nicht lesbar: {exc}")


def _save_disk() -> None:
    try:
        STOPS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STOPS_FILE.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(_RESOLVED, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, STOPS_FILE)
    except Exception as exc:
        log.warning(f"Abfahrten: Haltestellen nicht speicherbar: {exc}")


def search_locations(query: str, results: int = 5) -> list[dict]:
    """Haltestellen zu einem Namen: [{id, name, products}, …]. Wirft bei Netzfehlern."""
    response = HTTP_SESSION.get(
        f"{api_base_url()}/locations",
        params={"query": query, "results": results, "poi": "false", "addresses": "false"},
        headers={"User-Agent": USER_AGENT}, timeout=20,
    )
    response.raise_for_status()
    found: list[dict] = []
    for item in response.json() or []:
        if not isinstance(item, dict) or item.get("type") not in ("stop", "station"):
            continue
        found.append({
            "id": str(item.get("id", "")),
            "name": str(item.get("name", "")),
            "products": [k for k, v in (item.get("products") or {}).items() if v],
        })
    return [f for f in found if f["id"]]


def _resolve_key(query: str) -> str:
    """Je Schnittstelle gemerkt: die IDs von DB und VBB sind nicht dieselben."""
    return f"{api_base_url()}|{query.strip().lower()}"


def resolve_stop(query: str, force: bool = False) -> dict | None:
    """
    Nummer → direkt; Name → erster Treffer von /locations (gemerkt). None, wenn
    nichts gefunden. Fehlschläge werden gemerkt (Netzfehler 5 min, kein Treffer
    1 h), damit nicht jeder Render, jede Zusammenfassung und jeder Scrape die
    Schnittstelle erneut fragt. Ist sie nicht erreichbar, gilt ein alter Eintrag
    ohne Schnittstelle im Schlüssel (von vor 0.3) als Notlösung.
    """
    query = (query or "").strip()
    if not query:
        return None
    if query.isdigit():
        return {"id": query, "name": ""}
    key = _resolve_key(query)
    now = time.time()
    with _LOCK:
        _load_disk()
        if not force and key in _RESOLVED:
            return dict(_RESOLVED[key])
        legacy = _RESOLVED.get(query.lower())
        fallback = dict(legacy) if legacy else None
        failed = _RESOLVE_FAILED.get(key)
        if not force and failed and now - failed[0] < failed[1]:
            return fallback
    if not force and not network_allowed():
        return fallback
    try:
        found = search_locations(query, results=3)
    except Exception as exc:
        log.warning(f"Abfahrten: Haltestelle „{query}“ nicht auflösbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            _RESOLVE_FAILED[key] = (now, FETCH_RETRY_BACKOFF_SECONDS)
        return fallback
    if not found:
        with _LOCK:
            _RESOLVE_FAILED[key] = (now, NOT_FOUND_BACKOFF_SECONDS)
        return None
    with _LOCK:
        _RESOLVE_FAILED.pop(key, None)
        _RESOLVED[key] = {"id": found[0]["id"], "name": found[0]["name"]}
        _save_disk()
        return dict(_RESOLVED[key])


# ---------------------------------------------------------------------------
# Abfahrten
# ---------------------------------------------------------------------------

def _parse_when(value) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(local_tz()) if parsed.tzinfo else parsed.replace(tzinfo=local_tz())


def _clean_platform(value) -> str:
    """'2 (U5)' → '2': der Zusatz in Klammern (Berliner U-Bahn) passt nicht in die Spalte."""
    text = re.sub(r"\s*\([^)]*\)\s*$", "", str(value or "")).strip()
    return text[:8]


def parse_departures(payload) -> list[dict]:
    """Rohantwort → [{when, planned, delay_min, line, product, direction, platform, planned_platform, cancelled, warnings}, …]."""
    items = payload.get("departures", []) if isinstance(payload, dict) else (payload or [])
    out: list[dict] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        line = item.get("line") or {}
        planned = _parse_when(item.get("plannedWhen"))
        when = _parse_when(item.get("when")) or planned
        if planned is None and when is None:
            continue
        delay = item.get("delay")
        delay_min = int(round(delay / 60)) if isinstance(delay, (int, float)) else None
        warnings = [
            str(r.get("text") or r.get("summary") or "").strip()
            for r in (item.get("remarks") or []) if isinstance(r, dict) and r.get("type") in ("warning", "status")
        ]
        out.append({
            "when": when or planned,
            "planned": planned or when,
            "delay_min": delay_min,
            "line": str(line.get("name") or line.get("productName") or "?").strip(),
            "product": str(line.get("product") or "").strip(),
            "direction": str(item.get("direction") or (item.get("destination") or {}).get("name") or "").strip(),
            "platform": _clean_platform(item.get("platform")),
            "planned_platform": _clean_platform(item.get("plannedPlatform")),
            "cancelled": bool(item.get("cancelled")),
            "warnings": [w for w in warnings if w][:2],
        })
    out.sort(key=lambda d: d["when"])
    return out


def fetch_stop_departures(stop_id: str, duration_minutes: int, force_refresh: bool = False) -> dict:
    """
    {"departures": [...] | None, "name": str, "error": str, "fetched_at": float}.
    departures None nur, wenn nie erfolgreich geladen wurde.
    """
    cache_seconds = get_int_setting("DEPARTURES_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 30, 3600)
    now = time.time()
    with _LOCK:
        entry = _CACHE.get(stop_id)
        if entry is not None and not force_refresh:
            if entry["departures"] is not None and now - entry["fetched_at"] < cache_seconds:
                return dict(entry)
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                return dict(entry)
        if not network_allowed():
            if entry is not None:
                return dict(entry)
            return {"fetched_at": 0.0, "last_attempt_at": 0.0, "departures": None, "error": "", "name": ""}
        _CACHE[stop_id] = {
            "fetched_at": entry["fetched_at"] if entry else 0.0,
            "last_attempt_at": now,
            "departures": entry["departures"] if entry else None,
            "error": entry["error"] if entry else "",
            "name": entry["name"] if entry else "",
        }
    try:
        response = HTTP_SESSION.get(
            f"{api_base_url()}/stops/{stop_id}/departures",
            params={"duration": duration_minutes, "results": 30, "remarks": "true"},
            headers={"User-Agent": USER_AGENT}, timeout=25,
        )
        response.raise_for_status()
        payload = response.json()
        departures = parse_departures(payload)
        items = payload.get("departures", []) if isinstance(payload, dict) else payload
        name = ""
        for item in items or []:
            stop = item.get("stop") if isinstance(item, dict) else None
            if isinstance(stop, dict) and stop.get("name"):
                name = str(stop["name"])
                break
        with _LOCK:
            _CACHE[stop_id] = {"fetched_at": now, "last_attempt_at": now, "departures": departures, "error": "", "name": name}
            return dict(_CACHE[stop_id])
    except Exception as exc:
        status = getattr(getattr(exc, "response", None), "status_code", None)
        message = f"HTTP {status}" if status else (str(exc).strip()[:120] or exc.__class__.__name__)
        log.warning(f"Abfahrten für {stop_id} nicht ladbar: {message} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            _CACHE[stop_id]["error"] = message
            return dict(_CACHE[stop_id])


def should_refresh_departures() -> bool:
    """Neu rendern, wenn die Abfahrten einer eingetragenen Haltestelle abgelaufen sind (entfernte zählen nicht)."""
    cache_seconds = get_int_setting("DEPARTURES_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 30, 3600)
    now = time.time()
    with _LOCK:
        _load_disk()
        ids = set()
        for _, query in parse_stops(get_setting("DEPARTURES_STOPS", "")):
            if query.isdigit():
                ids.add(query)
            else:
                known = _RESOLVED.get(_resolve_key(query)) or _RESOLVED.get(query.strip().lower())
                if known:
                    ids.add(known["id"])
        for stop_id, entry in _CACHE.items():
            if stop_id not in ids:
                continue
            if now - entry["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
                continue
            if now - entry["fetched_at"] >= cache_seconds:
                return True
    return False


def clear_cache() -> None:
    global _DISK_LOADED
    with _LOCK:
        _CACHE.clear()
        _RESOLVED.clear()
        _RESOLVE_FAILED.clear()
        _DISK_LOADED = False


# ---------------------------------------------------------------------------
# Inhalt fürs Rendering
# ---------------------------------------------------------------------------

def build_departures_content(stops: list[dict], now: datetime, products: tuple[str, ...],
                             walk_minutes: int, max_per_stop: int) -> dict:
    """
    stops: [{"label", "id", "name", "departures": [...] | None, "error", "fetched_at"}]
    Filtert Produkte, lässt Abfahrten weg, die man mit dem Fußweg nicht mehr
    erreicht, berechnet Minuten bis zur Abfahrt und markiert alte Stände.
    """
    sections: list[dict] = []
    oldest_stale = None
    for stop in stops:
        departures = stop.get("departures")
        rows: list[dict] = []
        for dep in departures or []:
            if products and dep["product"] and dep["product"] not in products:
                continue
            minutes = int((dep["when"] - now).total_seconds() // 60)
            if minutes < walk_minutes and not dep["cancelled"]:
                continue
            if minutes < -1:
                continue
            rows.append({**dep, "in_minutes": max(0, minutes)})
            if len(rows) >= max_per_stop:
                break
        stale = ""
        if stop.get("error") and departures is not None and stop.get("fetched_at"):
            stale_dt = datetime.fromtimestamp(stop["fetched_at"], tz=now.tzinfo)
            stale = stale_dt.isoformat()
            oldest_stale = stale_dt if oldest_stale is None or stale_dt < oldest_stale else oldest_stale
        sections.append({
            "label": stop.get("label") or stop.get("name") or stop.get("id", ""),
            "name": stop.get("name") or "",
            "id": stop.get("id", ""),
            "rows": rows,
            "error": stop.get("error", "") if departures is None else "",
            "stale_since": stale,
            "total": len(departures or []),
        })
    return {
        "now": now.isoformat(),
        "sections": sections,
        "stale_since": oldest_stale.isoformat() if oldest_stale else "",
        "walk_minutes": walk_minutes,
        "loaded": any(s["rows"] or s["total"] for s in sections) or any(not s["error"] for s in sections),
    }


def fetch_departures_content(force_refresh: bool = False) -> dict | None:
    stops = parse_stops(get_setting("DEPARTURES_STOPS", ""))
    if not stops:
        return None
    duration = get_int_setting("DEPARTURES_DURATION_MINUTES", DEFAULT_DURATION_MINUTES, 10, 240)
    max_per_stop = get_int_setting("DEPARTURES_MAX", DEFAULT_MAX_PER_STOP, 1, 20)
    walk = get_int_setting("DEPARTURES_WALK_MINUTES", 0, 0, 120)
    products = selected_products()

    resolved_stops: list[dict] = []
    any_loaded = False
    for label, query in stops:
        target = resolve_stop(query)
        if target is None:
            resolved_stops.append({"label": label or query, "id": "", "name": "", "departures": None,
                                   "error": "Haltestelle nicht gefunden", "fetched_at": 0.0})
            continue
        result = fetch_stop_departures(target["id"], duration, force_refresh)
        if result["departures"] is not None:
            any_loaded = True
        resolved_stops.append({
            "label": label, "id": target["id"], "name": result.get("name") or target.get("name") or "",
            "departures": result["departures"], "error": result.get("error", ""), "fetched_at": result.get("fetched_at", 0.0),
        })
    if network_allowed():
        # Abfahrten entfernter Haltestellen vergessen
        used = {s["id"] for s in resolved_stops if s["id"]}
        with _LOCK:
            for stop_id in [k for k in _CACHE if k not in used]:
                del _CACHE[stop_id]
    if not any_loaded:
        return None
    return build_departures_content(resolved_stops, now_local(), products, walk, max_per_stop)
