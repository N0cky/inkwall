"""
DWD-Wetter-Datenabruf, -Parsing und -Caching.
"""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

from app.config import get_cfg, get_int_setting, get_setting, local_tz
from app.logger import get_logger
from app.http_client import HTTP_SESSION, FETCH_RETRY_BACKOFF_SECONDS

log = get_logger(__name__)

DWD_STATION_OVERVIEW_URL          = "https://app-prod-ws.warnwetter.de/v30/stationOverviewExtended?stationIds={station_id}"
DEFAULT_DWD_WEATHER_CACHE_SECONDS = 900
DWD_STATION_NAMES = {
    "10532": "Gießen",
}


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------

DWD_WEATHER_CACHE: dict = {"fetched_at": 0.0, "last_attempt_at": 0.0, "station_id": "", "timezone": "", "data": None}
DWD_WEATHER_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Hilfsfunktionen
# ---------------------------------------------------------------------------

def scale_dwd_value(value, factor: float = 10.0) -> float | None:
    if value is None:
        return None
    try:
        return float(value) / factor
    except (TypeError, ValueError):
        return None


def format_precipitation_text(value_mm: float | None) -> str:
    return "--" if value_mm is None else f"{value_mm:.1f} mm"


def format_humidity_text(value_percent: float | None) -> str:
    return "--" if value_percent is None else f"{round(value_percent)} %"


def format_wind_text(value_kmh: float | None, gust_kmh: float | None = None) -> str:
    if value_kmh is None and gust_kmh is None:
        return "--"
    if value_kmh is None:
        return f"Böen {gust_kmh:.1f} km/h"
    if gust_kmh is None:
        return f"{value_kmh:.1f} km/h"
    return f"{value_kmh:.1f} km/h\nBöen {gust_kmh:.1f} km/h"


def format_pressure_text(value_hpa: float | None) -> str:
    return "--" if value_hpa is None else f"{round(value_hpa)} hPa"


def format_sunshine_text(value_tenth_min: float | None) -> str:
    """DWD liefert Sonnenscheindauer in 1/10 Minuten → in h:mm umrechnen."""
    if value_tenth_min is None:
        return "--"
    total_min = round(float(value_tenth_min) / 10)
    h, m = divmod(total_min, 60)
    return f"{h}h {m:02d}m" if h else f"{m} min"


def compass_label(degrees: float | None) -> str:
    if degrees is None:
        return "--"
    dirs = ["N", "NO", "O", "SO", "S", "SW", "W", "NW"]
    return dirs[round(degrees / 45) % 8]


def resolve_dwd_station_name(station_id: str) -> str:
    cleaned = (station_id or "").strip()
    return DWD_STATION_NAMES.get(cleaned, cleaned) if cleaned else ""


def format_unix_ms_time(timestamp_ms) -> str:
    """
    Wandelt einen Unix-Timestamp in Millisekunden in eine HH:MM-Zeichenkette um.

    Der Timestamp wird explizit als UTC interpretiert und dann in die konfigurierte
    Lokalzeit konvertiert. Benötigt das Paket 'tzdata' (in requirements.txt).
    """
    if not timestamp_ms:
        return "--:--"
    try:
        utc_dt = datetime.fromtimestamp(int(timestamp_ms) / 1000, tz=timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            from app.config import get_cfg
            tz_name = get_cfg().timezone or "Europe/Berlin"
            local_dt = utc_dt.astimezone(ZoneInfo(tz_name))
        except Exception as tz_exc:
            log.warning(f"Timezone-Konvertierung fehlgeschlagen ({tz_exc}), nutze OS-Zeitzone. Ist 'tzdata' installiert?")
            local_dt = utc_dt.astimezone()
        return local_dt.strftime("%H:%M")
    except (TypeError, ValueError, OSError):
        return "--:--"


def normalize_text(value) -> str:
    if value is None:
        return ""
    return " ".join(str(value).split())


def map_dwd_icon_label(icon_code: int | None) -> str:
    return {
        1:  "Sonnig",
        2:  "Leicht bewölkt",
        3:  "Bewölkt",
        4:  "Bedeckt",
        5:  "Nebel",
        6:  "Nebel mit Glätte",
        7:  "Leichter Regen",
        8:  "Regen",
        9:  "Starker Regen",
        10: "Leichter Regen mit Glätte",
        11: "Starker Regen mit Glätte",
        12: "Regen und Schneeschauer",
        13: "Regen und Schneefall",
        14: "Leichter Schneefall",
        15: "Schneefall",
        16: "Starker Schneefall",
        17: "Wolkig mit Hagel",
        18: "Sonnig, leichter Regen",
        19: "Sonnig, starker Regen",
        20: "Sonnig, Regen und Schneeschauer",
        21: "Sonnig, Regen und Schneefall",
        22: "Sonnig, leichter Schneefall",
        23: "Sonnig mit Schneefall",
        24: "Sonnig mit Hagel",
        25: "Sonnig mit starkem Hagel",
        26: "Gewitter",
        27: "Gewitter mit Regen",
        28: "Starkes Gewitter",
        29: "Gewitter mit Hagel",
        30: "Starkes Gewitter mit Hagel",
        31: "Windig",
    }.get(icon_code, "Wetter")


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _parse_moon_phase(raw) -> float | None:
    """
    Normalisiert den DWD-Mondphasen-Wert auf 0.0–1.0.

    DWD liefert einen Integer 0–7 (8-Phasen-System):
      0 = Neumond          → 0.000
      1 = Zunehmende Sichel → 0.125
      2 = Erstes Viertel   → 0.250
      3 = Zunehmender Mond → 0.375
      4 = Vollmond         → 0.500
      5 = Abnehmender Mond → 0.625
      6 = Letztes Viertel  → 0.750
      7 = Abnehmende Sichel → 0.875
    """
    if raw is None:
        return None
    try:
        v = float(raw)
        if 0.0 <= v <= 7.0:
            return v / 8.0   # DWD: 8-Phasen-Integer → 0–1
        return None
    except (TypeError, ValueError):
        return None


def build_dwd_weather_summary(payload: dict, station_id: str) -> dict | None:
    station_data = payload.get(station_id)
    if not isinstance(station_data, dict):
        return None

    forecast1 = station_data.get("forecast1") or {}
    if not isinstance(forecast1, dict):
        forecast1 = {}
    days = station_data.get("days") or []
    if not isinstance(days, list):
        days = []
    warnings = station_data.get("warnings") or []
    if not isinstance(warnings, list):
        warnings = []

    start        = int(forecast1.get("start") or 0)
    step         = int(forecast1.get("timeStep") or 0)
    temperatures = forecast1.get("temperature") or []
    current_index = 0
    if start and step and temperatures:
        now_ms = int(time.time() * 1000)
        current_index = min(max(int((now_ms - start) // step), 0), len(temperatures) - 1)

    def series_value(series_name: str, index: int, factor: float = 10.0) -> float | None:
        series = forecast1.get(series_name)
        if not isinstance(series, list) or index >= len(series):
            return None
        return scale_dwd_value(series[index], factor)

    def series_plain(series_name: str, index: int):
        series = forecast1.get(series_name)
        if not isinstance(series, list) or index >= len(series):
            return None
        return series[index]

    def _safe_icon(raw) -> int | None:
        """Konvertiert einen rohen API-Icon-Wert sicher zu int (1–31) oder None."""
        if raw is None:
            return None
        try:
            v = int(raw)
            return v if 1 <= v <= 31 else None
        except (TypeError, ValueError):
            return None

    today = days[0] if days else {}
    if not isinstance(today, dict):
        today = {}

    _raw_cur_icon = series_plain("icon", current_index) or series_plain("icon1h", current_index)
    current_icon  = _safe_icon(_raw_cur_icon) if _raw_cur_icon is not None else None
    current_wind = series_value("windSpeed", current_index) or scale_dwd_value(today.get("windSpeed"))
    current_gust = series_value("windGust", current_index) or scale_dwd_value(today.get("windGust"))

    humidity_series = forecast1.get("humidity") or []
    today_date = str(today.get("dayDate") or "")
    if start and step and isinstance(temperatures, list) and temperatures:
        icon_series = forecast1.get("icon")
        if not isinstance(icon_series, list):
            icon_series = forecast1.get("icon1h")
        if not isinstance(icon_series, list):
            icon_series = None

        # Genug Rohdaten für beide Modi sammeln:
        # Rohdatenhorizont für bis zu 14 sichtbare Punkte bei max. 4h Intervall.
        # Dafür sammeln wir bis in den dritten Folgetag hinein.
        day_points: list[dict] = []
        next_day_count = 0
        second_next_day_count = 0
        third_next_day_count = 0
        today_dt = None
        if today_date:
            try:
                today_dt = datetime.strptime(today_date, "%Y-%m-%d").date()
            except ValueError:
                today_dt = None
        for index in range(len(temperatures)):
            point_ts_ms = start + (index * step)
            # tz-aware: im Container ist die Systemzeit UTC, die Stunden-
            # labels müssen aber in der konfigurierten Zeitzone stehen
            point_dt    = datetime.fromtimestamp(point_ts_ms / 1000, tz=local_tz())
            point_date_obj = point_dt.date()
            point_date  = point_dt.strftime("%Y-%m-%d")

            if today_dt is not None:
                day_offset = (point_date_obj - today_dt).days
                if day_offset < 0:
                    continue
                if day_offset == 1:
                    next_day_count += 1
                elif day_offset == 2:
                    second_next_day_count += 1
                elif day_offset == 3:
                    if third_next_day_count >= 4:
                        break          # max. 4h vom dritten Folgetag
                    third_next_day_count += 1
                elif day_offset > 3:
                    break
            elif today_date and point_date != today_date:
                if not day_points:
                    continue
                day_offset = 1
                if next_day_count >= 48:
                    break              # Fallback: ausreichend langer Horizont
                next_day_count += 1
            else:
                day_offset = 0

            hum = scale_dwd_value(humidity_series[index]) if index < len(humidity_series) else None

            # Icon-Code: Rohdaten sauber als int übernehmen (API liefert teils Floats)
            raw_icon = icon_series[index] if isinstance(icon_series, list) and index < len(icon_series) else None
            if raw_icon is not None:
                try:
                    raw_icon = int(raw_icon)
                except (TypeError, ValueError):
                    raw_icon = None

            day_points.append({
                "time":         point_dt.strftime("%H:%M"),
                "point_ts":     point_ts_ms,   # Millisekunden-Timestamp für Modus-Filterung
                "temp_c":       scale_dwd_value(temperatures[index]),
                "icon_code":    raw_icon,
                "humidity_pct": round(hum) if hum is not None else None,
                "day_offset":   day_offset,
                "is_next_day":  point_date != today_date,
            })
        hourly_all = day_points   # ungefiltert – Modus-Filterung in fetch_dwd_weather_content

    weather_days: list[dict] = []
    for entry in days[:5]:
        if not isinstance(entry, dict):
            continue
        wind_kmh  = scale_dwd_value(entry.get("windSpeed"))
        wind_dir  = scale_dwd_value(entry.get("windDirection"))
        icon_code = _safe_icon(entry.get("icon"))
        weather_days.append({
            "day_date":           entry.get("dayDate") or "",
            "min_temp_c":         scale_dwd_value(entry.get("temperatureMin")),
            "max_temp_c":         scale_dwd_value(entry.get("temperatureMax")),
            "icon_code":          icon_code,
            "label":              map_dwd_icon_label(icon_code),
            "precipitation_text": format_precipitation_text(scale_dwd_value(entry.get("precipitation"))),
            "wind_kmh":           round(wind_kmh) if wind_kmh is not None else None,
            "wind_dir_label":     compass_label(wind_dir),
            "sunshine_text":      format_sunshine_text(entry.get("sunshine")),
        })

    current_pressure = series_value("surfacePressure", current_index)
    wind_dir_today   = scale_dwd_value(today.get("windDirection"))
    parsed_warnings: list[dict] = []
    for entry in warnings:
        if not isinstance(entry, dict):
            continue
        start_ms = entry.get("start")
        end_ms = entry.get("end")
        headline = normalize_text(entry.get("headline"))
        event = normalize_text(entry.get("event"))
        description = normalize_text(entry.get("descriptionText") or entry.get("description"))
        instruction = normalize_text(entry.get("instruction"))
        try:
            level = int(entry.get("level")) if entry.get("level") is not None else None
        except (TypeError, ValueError):
            level = None
        parsed_warnings.append({
            "warn_id": entry.get("warnId") or "",
            "type": entry.get("type"),
            "level": level,
            "headline": headline,
            "event": event,
            "description": description,
            "instruction": instruction,
            "start": format_unix_ms_time(start_ms),
            "end": format_unix_ms_time(end_ms),
            "_start_ms": start_ms,
            "_end_ms": end_ms,
        })

    parsed_warnings.sort(
        key=lambda warning: (
            warning.get("_start_ms") if isinstance(warning.get("_start_ms"), (int, float)) else float("inf"),
            -(warning.get("level") or 0),
            warning.get("headline") or "",
        )
    )

    return {
        "station_id":                  station_id,
        "station_name":                resolve_dwd_station_name(station_id),
        "current_temp_c":              series_value("temperature", current_index),
        "current_label":               map_dwd_icon_label(current_icon),
        "current_icon_code":           current_icon,
        "current_precipitation_mm_text": format_precipitation_text(series_value("precipitationTotal", current_index)),
        "current_humidity_text":       format_humidity_text(series_value("humidity", current_index)),
        "current_wind_text":           format_wind_text(current_wind, current_gust),
        "current_wind_dir_label":      compass_label(wind_dir_today),
        "current_pressure_text":       format_pressure_text(current_pressure),
        "today": {
            "min_temp_c":    scale_dwd_value(today.get("temperatureMin")),
            "max_temp_c":    scale_dwd_value(today.get("temperatureMax")),
            # Formatierte Zeiten werden im Cache gespeichert UND als rohe ms-Timestamps,
            # damit fetch_dwd_weather_content sie bei jeder Timezone-Änderung neu berechnen kann.
            "sunrise":       format_unix_ms_time(today.get("sunrise")),
            "sunset":        format_unix_ms_time(today.get("sunset")),
            "moonrise":      format_unix_ms_time(today.get("moonrise")),
            "moonset":       format_unix_ms_time(today.get("moonset")),
            "moonPhase":     _parse_moon_phase(today.get("moonPhase")),
            "_sunrise_ms":   today.get("sunrise"),
            "_sunset_ms":    today.get("sunset"),
            "_moonrise_ms":  today.get("moonrise"),
            "_moonset_ms":   today.get("moonset"),
            "sunshine_text": format_sunshine_text(today.get("sunshine")),
        },
        "hourly_all": hourly_all,
        "days":            weather_days,
        "warnings":        parsed_warnings,
    }


# ---------------------------------------------------------------------------
# Fetch + Cache
# ---------------------------------------------------------------------------

def fetch_dwd_weather(force_refresh: bool = False) -> dict | None:
    cfg = get_cfg()
    station_id = get_setting("DWD_WEATHER_STATION_ID", "10532").strip() or "10532"
    tz_name    = cfg.timezone or "Europe/Berlin"
    cache_seconds = get_int_setting("DWD_WEATHER_CACHE_SECONDS", DEFAULT_DWD_WEATHER_CACHE_SECONDS, 60, 86400)
    now = time.time()

    with DWD_WEATHER_LOCK:
        cache_age       = now - DWD_WEATHER_CACHE["fetched_at"]
        station_changed = DWD_WEATHER_CACHE["station_id"] != station_id
        # Timezone-Wechsel → Cache ungültig, damit _*_ms-Felder neu befüllt werden
        tz_changed      = DWD_WEATHER_CACHE["timezone"] != tz_name
        if (
            not force_refresh
            and not station_changed
            and not tz_changed
            and DWD_WEATHER_CACHE["data"] is not None
            and cache_age < cache_seconds
        ):
            return dict(DWD_WEATHER_CACHE["data"])
        # Backoff nach Fehlschlag (nur wenn Station/TZ unverändert, sonst sofort neu)
        if (
            not force_refresh
            and not station_changed
            and not tz_changed
            and now - DWD_WEATHER_CACHE["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS
        ):
            cached = DWD_WEATHER_CACHE.get("data")
            return dict(cached) if isinstance(cached, dict) else None
        DWD_WEATHER_CACHE["last_attempt_at"] = now

    try:
        url = DWD_STATION_OVERVIEW_URL.format(station_id=station_id)
        response = HTTP_SESSION.get(url, timeout=20)
        response.raise_for_status()
        data = build_dwd_weather_summary(response.json(), station_id)
        with DWD_WEATHER_LOCK:
            DWD_WEATHER_CACHE["fetched_at"] = now
            DWD_WEATHER_CACHE["station_id"] = station_id
            DWD_WEATHER_CACHE["timezone"]   = tz_name
            DWD_WEATHER_CACHE["data"]       = dict(data) if data is not None else None
        return data
    except Exception as exc:
        log.warning(f"fetch_dwd_weather: {exc} – nächster Versuch in {FETCH_RETRY_BACKOFF_SECONDS}s")
        with DWD_WEATHER_LOCK:
            cached = DWD_WEATHER_CACHE.get("data")
            return dict(cached) if isinstance(cached, dict) else None


def cached_warnings() -> list[dict]:
    """Warnungen aus dem letzten Abruf, ohne neu zu laden (für „dringend“ und Benachrichtigungen)."""
    with DWD_WEATHER_LOCK:
        data = DWD_WEATHER_CACHE.get("data")
        return [dict(w) for w in ((data or {}).get("warnings") or []) if isinstance(w, dict)]


def should_refresh_dwd_weather() -> bool:
    cfg = get_cfg()
    with DWD_WEATHER_LOCK:
        now = time.time()
        if now - DWD_WEATHER_CACHE["last_attempt_at"] < FETCH_RETRY_BACKOFF_SECONDS:
            # Nach Fehlschlag: Station-/TZ-Wechsel erlaubt trotzdem sofortigen Refresh
            station_id = get_setting("DWD_WEATHER_STATION_ID", "10532").strip() or "10532"
            if (
                DWD_WEATHER_CACHE["station_id"] == station_id
                and DWD_WEATHER_CACHE["timezone"] == (cfg.timezone or "Europe/Berlin")
            ):
                return False
        cache_age       = now - DWD_WEATHER_CACHE["fetched_at"]
        station_id      = get_setting("DWD_WEATHER_STATION_ID", "10532").strip() or "10532"
        cache_seconds   = get_int_setting("DWD_WEATHER_CACHE_SECONDS", DEFAULT_DWD_WEATHER_CACHE_SECONDS, 60, 86400)
        station_changed = DWD_WEATHER_CACHE["station_id"] != station_id
        tz_changed      = DWD_WEATHER_CACHE["timezone"]   != (cfg.timezone or "Europe/Berlin")
        return station_changed or tz_changed or cache_age >= cache_seconds
