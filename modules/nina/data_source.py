"""
NINA-Datenquelle: Warnungen des Bevölkerungsschutzes (warnung.bund.de) für
einen Landkreis bzw. eine kreisfreie Stadt.

Der Ort wird als Name („Wetzlar“) oder als Regionalschlüssel angegeben. Namen
löst das Modul über die amtliche Liste der Gemeinden auf (einmal geladen, auf
Platte gemerkt); NINA liefert die Warnungen je Kreis, deshalb zählen nur die
ersten fünf Stellen, der Rest wird mit Nullen aufgefüllt (065320000000).

Warnungen des Wetterdienstes (Quelle DWD) bleiben draußen – die zeigt das
DWD-Wettermodul. Entwarnungen (msgType Cancel) sind keine aktive Warnung.
"""

from __future__ import annotations

import html
import json
import os
import re
import threading
import time
from datetime import datetime

from app.config import DATA_DIR, get_int_setting, get_setting, local_tz
from app.http_client import FETCH_RETRY_BACKOFF_SECONDS, HTTP_SESSION, network_allowed
from app.logger import get_logger

log = get_logger(__name__)

API_URL = "https://warnung.bund.de/api31"
REGIONS_URL = ("https://www.xrepository.de/api/xrepository/urn:de:bund:destatis:bevoelkerungsstatistik:"
               "schluessel:rs_2021-07-31/download/Regionalschl_ssel_2021-07-31.json")
REGIONS_FILE = DATA_DIR / "nina_regions.json"
USER_AGENT = "Inkwall (+https://github.com/N0cky/inkwall)"
DEFAULT_CACHE_SECONDS = 300

SEVERITY_RANK = {"extreme": 4, "severe": 3, "moderate": 2, "minor": 1}
SEVERITY_LABELS = {"extreme": "Extreme Gefahr", "severe": "Hohe Gefahr", "moderate": "Gefahr", "minor": "Information"}
MIN_SEVERITY_OPTIONS = (("minor", "Alle, auch Hinweise"), ("moderate", "Ab mittlerer Gefahr"), ("severe", "Nur hohe und extreme Gefahr"))
PROVIDER_LABELS = {"MOWAS": "Katastrophenschutz", "KATWARN": "Katwarn", "BIWAPP": "BIWAPP", "DWD": "Wetterdienst",
                   "LHP": "Hochwasser", "POLICE": "Polizei"}
EXCLUDED_PROVIDERS = {"DWD"}     # kommt übers DWD-Wettermodul

_LOCK = threading.Lock()
# Letzter Abruf: {"ars", "fetched_at", "last_attempt_at", "warnings", "excluded", "error"}
_CACHE: dict = {"ars": "", "fetched_at": 0.0, "last_attempt_at": 0.0, "warnings": [], "excluded": 0, "error": ""}
_DETAILS: dict[str, dict] = {}                 # "id|version" → Details aus /warnings/{id}.json
_REGIONS: list[tuple[str, str]] | None = None  # [(Regionalschlüssel, Gemeindename), …]
_REGIONS_FAILED_AT = 0.0


# ---------------------------------------------------------------------------
# Ort → Regionalschlüssel
# ---------------------------------------------------------------------------

def region_query() -> str:
    return get_setting("NINA_REGION", "").strip()


def _load_regions() -> list[tuple[str, str]]:
    """Gemeindeliste: Speicher, dann Platte, dann (wo Netz erlaubt) einmal laden."""
    global _REGIONS, _REGIONS_FAILED_AT
    if _REGIONS is not None:
        return _REGIONS
    try:
        data = json.loads(REGIONS_FILE.read_text(encoding="utf-8"))
        _REGIONS = [(str(code), str(name)) for code, name in data]
        return _REGIONS
    except (OSError, ValueError, TypeError):
        pass
    if not network_allowed() or time.time() - _REGIONS_FAILED_AT < FETCH_RETRY_BACKOFF_SECONDS:
        return []
    try:
        response = HTTP_SESSION.get(REGIONS_URL, timeout=20, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        rows = response.json().get("daten") or []
        regions = [(str(r[0]), str(r[1])) for r in rows if isinstance(r, list) and len(r) >= 2 and str(r[0]).isdigit()]
    except Exception as exc:
        _REGIONS_FAILED_AT = time.time()
        log.warning(f"NINA: Gemeindeliste nicht ladbar: {exc}")
        return []
    _REGIONS = regions
    try:
        REGIONS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = REGIONS_FILE.with_name(REGIONS_FILE.name + ".tmp")
        tmp.write_text(json.dumps(regions, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, REGIONS_FILE)
    except OSError as exc:
        log.warning(f"NINA: Gemeindeliste nicht speicherbar: {exc}")
    return regions


def _plain(name: str) -> str:
    """'Wetzlar, Stadt' → 'wetzlar'."""
    return name.split(",")[0].strip().lower()


def county_ars(code: str) -> str:
    """Regionalschlüssel des Kreises: die ersten fünf Stellen, aufgefüllt auf zwölf."""
    return code[:5] + "0" * 7


def resolve_region(query: str | None = None) -> dict:
    """
    {"ars": Kreis-Schlüssel oder "", "name": gefundener Ort, "matches": [(Schlüssel, Name), …]}.
    Eine Zahl mit 5 bis 12 Stellen wird direkt genommen.
    """
    query = (region_query() if query is None else query).strip()
    if not query:
        return {"ars": "", "name": "", "matches": []}
    digits = re.sub(r"\s", "", query)
    if digits.isdigit() and 5 <= len(digits) <= 12:
        code = digits.ljust(12, "0")
        name = next((n for c, n in _load_regions() if c == code), "")
        return {"ars": county_ars(code), "name": name or f"Kreis {code[:5]}", "matches": [(code, name)]}
    wanted = query.lower()
    regions = _load_regions()
    exact = [(c, n) for c, n in regions if _plain(n) == wanted or n.lower() == wanted]
    matches = exact or [(c, n) for c, n in regions if _plain(n).startswith(wanted)]
    if not matches:
        return {"ars": "", "name": "", "matches": []}
    code, name = matches[0]
    return {"ars": county_ars(code), "name": name, "matches": matches[:8]}


# ---------------------------------------------------------------------------
# Warnungen
# ---------------------------------------------------------------------------

def _text(value: str) -> str:
    """
    HTML der Schnittstelle (<br/>, Entities) → schlichter Text. Die Behörden
    brechen ihre Texte oft mitten im Satz um (feste Zeilenbreite); solche
    Umbrüche werden zu Leerzeichen. Ein neuer Absatz bleibt, wo eine Zeile auf
    . ! ? : endet, eine Leerzeile folgt oder eine Aufzählung (-, •, *) beginnt.
    """
    text = re.sub(r"<br\s*/?>", "\n", str(value or ""), flags=re.I)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    lines = [re.sub(r"[ \t]+", " ", line).strip() for line in text.splitlines()]
    out: list[str] = []
    joinable = False
    for line in lines:
        if not line:
            joinable = False
            continue
        if out and joinable and not line.startswith(("-", "•", "*")):
            out[-1] = f"{out[-1]} {line}"
        else:
            out.append(line)
        joinable = not out[-1].endswith((".", "!", "?", ":"))
    return "\n".join(out).strip()


def _parse_time(value) -> datetime | None:
    try:
        return datetime.fromisoformat(str(value)).astimezone(local_tz()) if value else None
    except ValueError:
        return None


def _parse_detail(data: dict) -> dict:
    info = next((i for i in data.get("info") or [] if str(i.get("language", "")).lower().startswith("de")),
                (data.get("info") or [{}])[0])
    params = {p.get("valueName"): p.get("value") for p in info.get("parameter") or [] if isinstance(p, dict)}
    areas = [a.get("areaDesc", "") for a in info.get("area") or [] if isinstance(a, dict) and a.get("areaDesc")]
    return {
        "event": _text(info.get("event", "")),
        "headline": _text(info.get("headline", "")),
        "description": _text(info.get("description", "")),
        "instruction": _text(info.get("instruction", "")),
        "area": ", ".join(areas),
        "sender": _text(params.get("sender_langname") or info.get("senderName") or ""),
        "expires": info.get("expires") or "",
        "web": str(info.get("web") or ""),
    }


def _detail(warning_id: str, version) -> dict | None:
    key = f"{warning_id}|{version}"
    if key in _DETAILS:
        return _DETAILS[key]
    try:
        response = HTTP_SESSION.get(f"{API_URL}/warnings/{warning_id}.json", timeout=10, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        detail = _parse_detail(response.json())
    except Exception as exc:
        log.warning(f"NINA: Details zu {warning_id} nicht ladbar: {exc}")
        return None
    _DETAILS[key] = detail
    return detail


def parse_dashboard(items: list, min_severity: str = "minor", load_detail=None, now: datetime | None = None) -> tuple[list[dict], int]:
    """
    Dashboard-Einträge → (aktive Warnungen, sortiert nach Schwere und Zeit; Anzahl ausgelassener Wetterwarnungen).
    load_detail(id, version) → Details oder None (Tests geben hier eigene Daten).
    """
    now = now or datetime.now(local_tz())
    floor = SEVERITY_RANK.get(min_severity, 1)
    warnings: list[dict] = []
    excluded = 0
    for item in items if isinstance(items, list) else []:
        payload = (item or {}).get("payload") or {}
        data = payload.get("data") or {}
        provider = str(data.get("provider", "")).upper()
        if provider in EXCLUDED_PROVIDERS:
            excluded += 1
            continue
        msg_type = str(data.get("msgType", "")).lower()
        if msg_type == "cancel" or data.get("valid") is False:
            continue
        severity = str(data.get("severity", "")).lower()
        if SEVERITY_RANK.get(severity, 1) < floor:
            continue
        warning_id = str(item.get("id") or payload.get("id") or "")
        version = payload.get("version", 0)
        detail = (load_detail(warning_id, version) if load_detail else None) or {}
        expires = _parse_time(detail.get("expires"))
        if expires is not None and expires <= now:
            continue
        title = (item.get("i18nTitle") or {}).get("de") or detail.get("headline") or data.get("headline") or "Warnung"
        warnings.append({
            "id": warning_id,
            "version": version,
            "provider": provider,
            "provider_label": PROVIDER_LABELS.get(provider, provider.title() or "NINA"),
            "severity": severity if severity in SEVERITY_RANK else "minor",
            "severity_label": SEVERITY_LABELS.get(severity, "Warnung"),
            "update": msg_type == "update",
            "headline": _text(title),
            "event": detail.get("event", ""),
            "description": detail.get("description", ""),
            "instruction": detail.get("instruction", ""),
            "area": detail.get("area", ""),
            "sender": detail.get("sender", ""),
            "sent": str(item.get("sent") or ""),
            "expires": expires.isoformat() if expires else "",
            "web": detail.get("web", ""),
        })
    # Schwerste zuerst, bei gleicher Schwere die neueste
    warnings.sort(key=lambda w: w["sent"], reverse=True)
    warnings.sort(key=lambda w: -SEVERITY_RANK.get(w["severity"], 1))
    return warnings, excluded


def cache_seconds() -> int:
    return get_int_setting("NINA_CACHE_SECONDS", DEFAULT_CACHE_SECONDS, 60, 3600)


def min_severity() -> str:
    value = get_setting("NINA_MIN_SEVERITY", "minor").strip().lower()
    return value if value in SEVERITY_RANK else "minor"


def fetch_warnings(force: bool = False) -> dict | None:
    """
    {"ars", "region", "warnings", "excluded", "fetched_at", "error", "stale"} – None ohne Ort.
    Kurz gecacht; fällt die Quelle aus, bleibt der letzte Stand (mit error).
    """
    region = resolve_region()
    if not region["ars"]:
        return None
    now = time.time()
    with _LOCK:
        same = _CACHE["ars"] == region["ars"]
        fresh = same and now - _CACHE["fetched_at"] < cache_seconds()
        waiting = same and now - _CACHE["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS
        if not force and (fresh or waiting or not network_allowed()):
            return _snapshot(region) if same and _CACHE["fetched_at"] else None
        _CACHE["last_attempt_at"] = now
    try:
        response = HTTP_SESSION.get(f"{API_URL}/dashboard/{region['ars']}.json", timeout=10, headers={"User-Agent": USER_AGENT})
        response.raise_for_status()
        warnings, excluded = parse_dashboard(response.json(), min_severity(), _detail)
    except Exception as exc:
        log.warning(f"NINA: Warnungen für {region['ars']} nicht ladbar: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with _LOCK:
            if _CACHE["ars"] != region["ars"]:
                _CACHE.update(ars=region["ars"], fetched_at=0.0, warnings=[], excluded=0)
            _CACHE["error"] = str(exc)[:160]
            return _snapshot(region) if _CACHE["fetched_at"] else None
    with _LOCK:
        _CACHE.update(ars=region["ars"], fetched_at=now, warnings=warnings, excluded=excluded, error="")
        # Details alter Versionen nicht ewig halten
        keep = {f"{w['id']}|{w['version']}" for w in warnings}
        for key in [k for k in _DETAILS if k not in keep]:
            _DETAILS.pop(key, None)
        return _snapshot(region)


def _snapshot(region: dict) -> dict:
    return {
        "ars": _CACHE["ars"],
        "region": region.get("name", ""),
        "warnings": [dict(w) for w in _CACHE["warnings"]],
        "excluded": _CACHE["excluded"],
        "fetched_at": _CACHE["fetched_at"],
        "error": _CACHE["error"],
    }


def cached_warnings() -> list[dict]:
    """Aktive Warnungen des letzten Abrufs, ohne Netz (für „dringend“ und Benachrichtigungen)."""
    with _LOCK:
        return [dict(w) for w in _CACHE["warnings"]]


def should_refresh_nina() -> bool:
    if not region_query():
        return False
    with _LOCK:
        now = time.time()
        if now - _CACHE["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS and _CACHE["error"]:
            return False
        return now - _CACHE["fetched_at"] >= cache_seconds()


def clear_cache() -> None:
    """Für Tests."""
    global _REGIONS
    with _LOCK:
        _CACHE.update(ars="", fetched_at=0.0, last_attempt_at=0.0, warnings=[], excluded=0, error="")
        _DETAILS.clear()
    _REGIONS = None
