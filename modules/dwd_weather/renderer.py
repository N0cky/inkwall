from __future__ import annotations

import functools
import math
import time as _time
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

from app.config import format_date_long, format_weekday_short
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from app.text_rendering import draw_lines, fit_wrapped_text, new_draw


PROJECT_DIR = Path(__file__).resolve().parents[2]
FONT_AWESOME_SOLID = PROJECT_DIR / "font" / "Font Awesome 7 Free-Solid-900.otf"

FA_ICONS = {
    # UI-Icons (Stat-Panels, Legende)
    "droplet":            "\uf043",
    "umbrella":           "\uf0e9",
    "wind":               "\uf72e",
    "gauge":              "\uf625",
    "compass":            "\uf14e",
    "sunrise":            "\uf766",
    "sunset":             "\uf767",
    "moon":               "\uf186",   # Mondaufgang UND -untergang – Richtung per Pfeil
    "radiation":          "\uf7b9",
    "warning":            "\uf071",
    # Wetter-Icons
    "sun":                "\uf185",   # 1  Sonnig
    "cloud_sun":          "\uf6c4",   # 2  Leicht bewölkt / sonnig
    "cloud":              "\uf0c2",   # 3–4 Bewölkt / Bedeckt
    "smog":               "\uf75f",   # 5–6 Nebel
    "cloud_rain":         "\uf73d",   # 7–8 Regen
    "cloud_showers":      "\uf740",   # 9  Starker Regen
    "icicles":            "\uf7ad",   # Glätte / Hagel
    "snowflake":          "\uf2dc",   # 14–16 Schneefall
    "cloud_sun_rain":     "\uf743",   # 18–21 Sonnig mit Regen
    "bolt":               "\uf0e7",   # 26–30 Gewitter
}

# DWD-Wetter-Icon-Codes 1–31 → interner Icon-Name
WEATHER_ICON_MAP = {
    1:  "sun",            # Sonnig
    2:  "cloud_sun",      # Leicht bewölkt
    3:  "cloud",          # Bewölkt
    4:  "cloud",          # Bedeckt
    5:  "smog",           # Nebel
    6:  "smog",           # Nebel mit Glätte
    7:  "cloud_rain",     # Leichter Regen
    8:  "cloud_rain",     # Regen
    9:  "cloud_showers",  # Starker Regen
    10: "cloud_rain",     # Leichter Regen mit Glätte
    11: "cloud_showers",  # Starker Regen mit Glätte
    12: "cloud_rain",     # Regen und Schneeschauer
    13: "cloud_rain",     # Regen und Schneefall
    14: "snowflake",      # Leichter Schneefall
    15: "snowflake",      # Schneefall
    16: "snowflake",      # Starker Schneefall
    17: "icicles",        # Wolkig mit Hagel
    18: "cloud_sun_rain", # Sonnig mit leichtem Regen
    19: "cloud_sun_rain", # Sonnig mit starkem Regen
    20: "cloud_sun_rain", # Sonnig mit Regen und Schneeschauern
    21: "cloud_sun_rain", # Sonnig mit Regen und Schneefall
    22: "cloud_sun",      # Sonnig mit leichtem Schneefall
    23: "cloud_sun",      # Sonnig mit Schneefall
    24: "cloud_sun",      # Sonnig mit Hagel
    25: "cloud_sun",      # Sonnig mit starkem Hagel
    26: "bolt",           # Gewitter
    27: "bolt",           # Gewitter mit Regen
    28: "bolt",           # Starkes Gewitter
    29: "bolt",           # Gewitter mit Hagel
    30: "bolt",           # Starkes Gewitter mit Hagel
    31: "wind",           # Windig
}

SNOW_OVERRIDE_TEMP_C = 4.0


# ---------------------------------------------------------------------------
# Farbpaletten  (alle Einträge sind RGBA-4-Tupel oder RGB-3-Tupel)
# ---------------------------------------------------------------------------

_DWD_DARK: dict = {
    # Hintergrund
    "bg_top":              (84,  108, 142),
    "bg_bottom":           (66,  84,  114),
    # Haupt-Panel
    "panel_tint":          (12,  20,  34,  160),
    "panel_outline":       (255, 255, 255, 30),
    # Kopfzeile
    "header_idle":         (210, 224, 240, 220),
    "header_station":      (180, 208, 236, 235),
    # Aktuelle Temp / Zustand
    "temp_big":            (255, 255, 255, 255),
    "condition":           (230, 242, 255, 245),
    # Heute-Badge
    "badge_fill":          (52,  110, 190, 185),
    "badge_outline":       (180, 215, 255, 80),
    "badge_text":          (255, 255, 255, 248),
    # Großes Wetter-Icon
    "icon_main":           (243, 248, 255, 240),
    # Sonnen- und Mondauf-/-untergang
    "sun_icon":            (255, 210, 80,  240),
    "sun_arrow":           (132, 189, 255, 235),
    "sun_label":           (176, 202, 229, 232),
    "sun_value":           (250, 252, 255, 248),
    "moon_icon":           (200, 215, 255, 240),   # silbrig-blaues Mondlicht
    "moon_arrow":          (140, 168, 220, 225),
    "moon_label":          (170, 196, 228, 225),
    "moon_value":          (225, 235, 255, 248),
    # Mondphasen-Scheibe
    "moon_disc_lit":       (228, 236, 255, 245),   # beleuchtete Seite
    "moon_disc_dark":      (12,  22,  44,  220),   # Schattenseite
    "moon_disc_border":    (120, 155, 210, 160),   # Rand
    "moon_disc_label":     (190, 210, 240, 228),   # Beschriftung
    # Trennlinie
    "divider":             (180, 205, 232, 40),
    # Stat-Panels (Glas)
    "stat_glass_tint":     (14,  28,  48,  140),
    "stat_glass_outline":  (255, 255, 255, 40),
    "stat_icon":           (140, 196, 255, 235),
    "stat_label":          (188, 214, 239, 232),
    "stat_value":          (255, 255, 255, 248),
    # Stundenverlauf
    "chart_title":         (225, 235, 247, 240),
    "chart_nodata":        (180, 193, 207, 220),
    "grid":                (180, 205, 232, 36),
    "baseline":            (180, 205, 232, 80),
    "axis_label":          (180, 205, 232, 200),
    "curve_fill":          (110, 176, 244, 30),
    "curve_line":          (122, 194, 255, 240),
    "point_outer":         (255, 255, 255, 240),
    "point_inner":         (122, 194, 255, 255),
    "hourly_icon":         (210, 230, 255, 244),
    "hourly_temp":         (246, 249, 255, 240),
    "hourly_time":         (192, 207, 223, 228),
    # Prognose-Streifen
    "fc_glass_tint":       (14,  26,  44,  148),
    "fc_glass_outline":    (255, 255, 255, 36),
    "fc_icon":             (210, 230, 255, 240),
    "fc_day":              (220, 234, 248, 248),
    "fc_temp":             (255, 255, 255, 252),
    "fc_sun_text":         (200, 220, 244, 220),
    "fc_wind_text":        (180, 208, 235, 210),
    "fc_uv_text":          (180, 208, 235, 210),
    "fc_sun_icon":         (255, 210, 80,  220),
    "fc_wind_icon":        (160, 200, 240, 200),
    # Pollenleiste
    "pollen_glass_tint":   (16,  30,  52,  170),
    "pollen_glass_outline":(140, 185, 240, 55),
    "pollen_title":        (200, 225, 255, 245),
    "pollen_label":        (225, 240, 255, 235),
    # Warnungen
    "warning_glass_tint":  (42,  24,  18,  182),
    "warning_glass_outline": (255, 190, 120, 92),
    "warning_title":       (255, 225, 196, 245),
    "warning_text":        (255, 241, 228, 245),
    "warning_meta":        (255, 214, 182, 220),
}

_DWD_LIGHT: dict = {
    # Hintergrund
    "bg_top":              (196, 216, 238),
    "bg_bottom":           (172, 196, 222),
    # Haupt-Panel
    "panel_tint":          (255, 255, 255, 210),
    "panel_outline":       (130, 165, 200, 100),
    # Kopfzeile
    "header_idle":         (35,  65,  100, 240),
    "header_station":      (45,  80,  120, 240),
    # Aktuelle Temp / Zustand
    "temp_big":            (15,  35,  65,  255),
    "condition":           (40,  75,  110, 250),
    # Heute-Badge
    "badge_fill":          (58,  118, 185, 215),
    "badge_outline":       (80,  148, 210, 120),
    "badge_text":          (255, 255, 255, 255),
    # Großes Wetter-Icon
    "icon_main":           (25,  75,  145, 245),
    # Sonnen- und Mondauf-/-untergang
    "sun_icon":            (215, 150, 15,  245),
    "sun_arrow":           (35,  95,  165, 240),
    "sun_label":           (55,  95,  135, 240),
    "sun_value":           (15,  40,  70,  255),
    "moon_icon":           (55,  80,  175, 240),   # tiefes Nachtblau
    "moon_arrow":          (45,  90,  160, 225),
    "moon_label":          (55,  95,  145, 235),
    "moon_value":          (10,  35,  80,  255),
    # Mondphasen-Scheibe
    "moon_disc_lit":       (240, 244, 255, 250),   # beleuchtete Seite
    "moon_disc_dark":      (30,  50,  100, 200),   # Schattenseite
    "moon_disc_border":    (70,  105, 170, 180),   # Rand
    "moon_disc_label":     (30,  65,  120, 245),   # Beschriftung
    # Trennlinie
    "divider":             (110, 155, 195, 70),
    # Stat-Panels (Glas)
    "stat_glass_tint":     (240, 246, 255, 215),
    "stat_glass_outline":  (130, 165, 200, 100),
    "stat_icon":           (40,  95,  155, 245),
    "stat_label":          (55,  95,  135, 245),
    "stat_value":          (12,  35,  65,  255),
    # Stundenverlauf
    "chart_title":         (30,  65,  105, 245),
    "chart_nodata":        (70,  110, 150, 220),
    "grid":                (90,  135, 175, 55),
    "baseline":            (90,  135, 175, 110),
    "axis_label":          (55,  100, 140, 215),
    "curve_fill":          (55,  115, 195, 35),
    "curve_line":          (25,  80,  165, 220),
    "point_outer":         (25,  80,  165, 255),
    "point_inner":         (255, 255, 255, 255),
    "hourly_icon":         (35,  95,  160, 245),
    "hourly_temp":         (12,  40,  75,  248),
    "hourly_time":         (65,  108, 148, 232),
    # Prognose-Streifen
    "fc_glass_tint":       (238, 244, 254, 215),
    "fc_glass_outline":    (130, 165, 200, 100),
    "fc_icon":             (35,  95,  160, 240),
    "fc_day":              (25,  62,  105, 252),
    "fc_temp":             (8,   30,  60,  255),
    "fc_sun_text":         (55,  108, 162, 224),
    "fc_wind_text":        (65,  112, 158, 218),
    "fc_uv_text":          (65,  112, 158, 218),
    "fc_sun_icon":         (215, 150, 15,  230),
    "fc_wind_icon":        (45,  95,  148, 215),
    # Pollenleiste
    "pollen_glass_tint":   (230, 240, 255, 220),
    "pollen_glass_outline":(85,  130, 185, 120),
    "pollen_title":        (20,  55,  100, 255),
    "pollen_label":        (8,   30,  62,  255),
    # Warnungen
    "warning_glass_tint":  (255, 243, 232, 230),
    "warning_glass_outline": (214, 138, 68, 130),
    "warning_title":       (130, 60, 10, 255),
    "warning_text":        (92,  40,  8,  255),
    "warning_meta":        (120, 72,  32, 236),
}


def _spectra(name: str, alpha: int = 255) -> tuple[int, int, int, int]:
    from app.image_rendering import SPECTRA6_COLORS as _C
    return (*_C[name], alpha)


# E-Ink: ausschließlich die sechs Spectra-Farben, alles deckend. Was hier
# steht, wird auf dem Display ohne Dithering dargestellt. "flat" schaltet
# in den Zeichenfunktionen Transparenzen und Blur ab.
_DWD_EINK: dict = {
    "flat":                True,
    "bg_top":              _spectra("white")[:3],
    "bg_bottom":           _spectra("white")[:3],
    "panel_tint":          _spectra("white"),
    "panel_outline":       _spectra("black"),
    "header_idle":         _spectra("black"),
    "header_station":      _spectra("blue"),
    "temp_big":            _spectra("black"),
    "condition":           _spectra("black"),
    "badge_fill":          _spectra("blue"),
    "badge_outline":       _spectra("blue"),
    "badge_text":          _spectra("white"),
    "icon_main":           _spectra("blue"),
    "sun_icon":            _spectra("yellow"),
    "sun_arrow":           _spectra("black"),
    "sun_label":           _spectra("black"),
    "sun_value":           _spectra("black"),
    "moon_icon":           _spectra("blue"),
    "moon_arrow":          _spectra("black"),
    "moon_label":          _spectra("black"),
    "moon_value":          _spectra("black"),
    # Weiß auf weißem Grund sah auf dem Panel wie ein leerer Kreis aus – die
    # beleuchtete Seite ist deshalb gelb, die Schattenseite schwarz.
    "moon_disc_lit":       _spectra("yellow"),
    "moon_disc_dark":      _spectra("black"),
    "moon_disc_border":    _spectra("black"),
    "moon_disc_label":     _spectra("black"),
    "divider":             _spectra("black"),
    "stat_glass_tint":     _spectra("white"),
    "stat_glass_outline":  _spectra("black"),
    "stat_icon":           _spectra("blue"),
    "stat_label":          _spectra("black"),
    "stat_value":          _spectra("black"),
    "chart_title":         _spectra("black"),
    "chart_nodata":        _spectra("black"),
    "grid":                _spectra("black"),
    "baseline":            _spectra("black"),
    "axis_label":          _spectra("black"),
    "curve_fill":          _spectra("blue"),
    "curve_line":          _spectra("black"),
    "point_outer":         _spectra("black"),
    "point_inner":         _spectra("white"),
    "hourly_icon":         _spectra("blue"),
    "hourly_temp":         _spectra("black"),
    "hourly_time":         _spectra("black"),
    "fc_glass_tint":       _spectra("white"),
    "fc_glass_outline":    _spectra("black"),
    "fc_icon":             _spectra("blue"),
    "fc_day":              _spectra("black"),
    "fc_temp":             _spectra("black"),
    "fc_sun_text":         _spectra("black"),
    "fc_wind_text":        _spectra("black"),
    "fc_uv_text":          _spectra("black"),
    "fc_sun_icon":         _spectra("yellow"),
    "fc_wind_icon":        _spectra("blue"),
    "pollen_glass_tint":   _spectra("white"),
    "pollen_glass_outline": _spectra("black"),
    "pollen_title":        _spectra("black"),
    "pollen_label":        _spectra("black"),
    "warning_glass_tint":  _spectra("yellow"),
    "warning_glass_outline": _spectra("black"),
    "warning_title":       _spectra("black"),
    "warning_text":        _spectra("black"),
    "warning_meta":        _spectra("black"),
}


def get_dwd_palette(theme: str) -> dict:
    if theme == "eink":
        return _DWD_EINK
    return _DWD_LIGHT if theme == "light" else _DWD_DARK


def warning_level_label(level: int | None) -> str:
    return {
        1: "Amtliche Warnung",
        2: "Markantes Wetter",
        3: "Unwetterwarnung",
        4: "Extremes Unwetter",
        5: "Extremes Unwetter",
    }.get(level, "Warnung")


def warning_level_color(level: int | None, flat: bool = False) -> tuple[int, int, int]:
    if flat:
        return SPECTRA6_COLORS["red"] if (level or 0) >= 3 else SPECTRA6_COLORS["yellow"]
    if level is None:
        return (222, 176, 36)
    if level >= 4:
        return (124, 18, 18)
    if level == 3:
        return (188, 38, 26)
    if level == 2:
        return (214, 132, 24)
    return (226, 186, 52)


# ---------------------------------------------------------------------------
# Wetter-Icon-Helpers
# ---------------------------------------------------------------------------

def get_weather_icon_name(icon_code: int | None, temp_hint_c: float | None = None) -> str:
    name = WEATHER_ICON_MAP.get(icon_code, "cloud")
    if temp_hint_c is not None and temp_hint_c > SNOW_OVERRIDE_TEMP_C:
        # Bei Temperaturen über 4 °C Schnee-Codes auf Regen umstellen
        if name == "snowflake":
            return "cloud_rain"
        # Regen+Schnee-Misch-Codes bei Wärme → reiner Regen
        if icon_code in {12, 13}:
            return "cloud_rain"
        # Sonnig+Schnee → sonnig mit Regen
        if icon_code in {20, 21}:
            return "cloud_sun_rain"
    return name


@functools.lru_cache(maxsize=32)
def load_fa_font(size: int):
    try:
        if FONT_AWESOME_SOLID.exists():
            return ImageFont.truetype(str(FONT_AWESOME_SOLID), size)
    except Exception:
        pass
    return None


def format_temp(temp_c: float | None) -> str:
    if temp_c is None:
        return "--"
    return f"{round(temp_c)}°"


def format_day_label(day_date: str) -> str:
    """'Mi 03.09.' – deutscher Wochentag unabhängig vom System-Locale."""
    try:
        parsed = datetime.strptime(day_date, "%Y-%m-%d")
        return f"{format_weekday_short(parsed)} {parsed.strftime('%d.%m.')}"
    except ValueError:
        return day_date


def fetch_dwd_weather_content() -> dict | None:
    from app.config import get_int_setting, get_setting
    from .dwd import fetch_dwd_weather, format_unix_ms_time
    from .dwd_pollen import fetch_dwd_pollen

    weather = fetch_dwd_weather(False)
    if weather is None:
        return None
    weather = dict(weather)

    # ── Sonnen-/Mondzeiten mit aktueller Zeitzone neu formatieren ────────────
    # Die formatierten Strings im Cache spiegeln die Zeitzone zum Zeitpunkt
    # des letzten API-Abrufs wider.  Durch Neuformatierung aus den gespeicherten
    # Roh-Timestamps greifen Timezone-Änderungen in den Settings sofort.
    today_raw = dict(weather.get("today") or {})
    for key in ("sunrise", "sunset", "moonrise", "moonset"):
        raw_ms = today_raw.get(f"_{key}_ms")
        if raw_ms is not None:
            today_raw[key] = format_unix_ms_time(raw_ms)
    weather["today"] = today_raw

    warning_items = []
    for warning in weather.get("warnings") or []:
        warning_copy = dict(warning)
        for key in ("start", "end"):
            raw_ms = warning_copy.get(f"_{key}_ms")
            if raw_ms is not None:
                warning_copy[key] = format_unix_ms_time(raw_ms)
        warning_items.append(warning_copy)
    weather["warnings"] = warning_items

    # ── Stündlichen Verlauf nach aktueller Einstellung filtern (ohne Neustart) ──
    hourly_all: list[dict] = weather.get("hourly_all") or []
    interval_hours = max(1, get_int_setting("DWD_HOURLY_INTERVAL_HOURS", 2, 1, 4))
    target_visible_points = 14
    raw_window_size = target_visible_points * interval_hours

    # day_start startet am Tagesbeginn und hält genau das Rohfenster vor,
    # das für 14 sichtbare Punkte im gewählten Intervall nötig ist.
    day_start_hourly = hourly_all[:raw_window_size] if raw_window_size > 0 else hourly_all

    if get_setting("DWD_HOURLY_START", "day_start") == "current_hour" and hourly_all:
        # Ab der aktuellen vollen Stunde starten, damit z. B. 19:30 mit 19:00
        # beginnt und nicht fälschlich schon bei 20:00.
        now_ms = int(_time.time() * 1000)
        current_hour_start_ms = now_ms - (now_ms % 3_600_000)
        filtered = [p for p in hourly_all if p.get("point_ts", 0) >= current_hour_start_ms]
        hourly   = filtered if filtered else hourly_all
        hourly = hourly[:raw_window_size] if raw_window_size > 0 else hourly
    else:
        hourly = day_start_hourly

    # Intervall bestimmt den Abstand, nicht die Anzahl: immer bis zu 14 Werte.
    weather["hourly_forecast"] = hourly[::interval_hours][:target_visible_points]

    # ── UV-Index ──────────────────────────────────────────────────────────────
    from .dwd_uv import fetch_dwd_uv
    uv_data = fetch_dwd_uv(False)
    if uv_data is not None:
        uvi_list: list = uv_data.get("uvi_by_index") or []
        # UV-Wert per Tagesindex in weather["days"] einbetten
        days = weather.get("days") or []
        new_days = []
        for i, day in enumerate(days):
            d = dict(day)
            d["uvi_max"] = uvi_list[i] if i < len(uvi_list) else None
            new_days.append(d)
        weather["days"] = new_days

    # ── Pollen ────────────────────────────────────────────────────────────────
    pollen = fetch_dwd_pollen(False)
    if pollen is not None:
        weather["pollen"] = pollen

    return weather


def has_dwd_weather_content(content: object) -> bool:
    return isinstance(content, dict) and bool(content)


# ---------------------------------------------------------------------------
# Hintergrund
# ---------------------------------------------------------------------------

def _create_gradient_background(render_width: int, render_height: int,
                                top_color: tuple, bot_color: tuple) -> Image.Image:
    rows = []
    for y in range(render_height):
        progress = y / max(render_height - 1, 1)
        r = int(top_color[0] + (bot_color[0] - top_color[0]) * progress)
        g = int(top_color[1] + (bot_color[1] - top_color[1]) * progress)
        b = int(top_color[2] + (bot_color[2] - top_color[2]) * progress)
        rows.append(bytes([r, g, b]) * render_width)
    return Image.frombytes("RGB", (render_width, render_height), b"".join(rows))


def create_weather_background(render_width: int, render_height: int) -> Image.Image:
    return _create_gradient_background(render_width, render_height, (84, 108, 142), (66, 84, 114))


def create_weather_background_light(render_width: int, render_height: int) -> Image.Image:
    return _create_gradient_background(render_width, render_height, (196, 216, 238), (172, 196, 222))


# ---------------------------------------------------------------------------
# Glas-Panel
# ---------------------------------------------------------------------------

def draw_fa_icon(draw, x, y, icon_name, size, fill):
    font = load_fa_font(size)
    glyph = FA_ICONS.get(icon_name)
    if not font or not glyph:
        return
    draw.text((x, y), glyph, font=font, fill=fill)


def draw_humidity_icon(draw, x, y, color, label_font=None, size: int = 20):
    fa = load_fa_font(size)
    if fa:
        draw.text((x, y - 1), FA_ICONS["droplet"], font=fa, fill=color)


def draw_weather_icon(draw, x, y, icon_code, size,
                      fill=(235, 244, 255, 248), temp_hint_c=None):
    draw_fa_icon(draw, x, y, get_weather_icon_name(icon_code, temp_hint_c), size, fill)


def apply_glass_panel(
    img: Image.Image,
    bounds: tuple[int, int, int, int],
    radius: int,
    tint: tuple,
    outline: tuple,
    blur_radius: int = 18,
):
    crop = img.crop(bounds).convert("RGBA")
    if blur_radius > 0:
        crop = crop.filter(ImageFilter.GaussianBlur(radius=blur_radius))
    crop.alpha_composite(Image.new("RGBA", crop.size, tint))

    mask = Image.new("L", crop.size, 0)
    mask_draw = new_draw(mask)
    mask_draw.rounded_rectangle((0, 0, crop.size[0], crop.size[1]), radius=radius, fill=255)
    crop.putalpha(mask)
    img.alpha_composite(crop, dest=(bounds[0], bounds[1]))

    draw = new_draw(img, "RGBA")
    draw.rounded_rectangle(bounds, radius=radius, outline=outline, width=2)


# ---------------------------------------------------------------------------
# Astronomische Ereignisse (Sonnen- / Mondauf- und -untergang)
# ---------------------------------------------------------------------------

def _draw_astro_arrow(draw, cx, cy, is_rise: bool, color, scale: float = 1.0):
    """Kleiner Pfeil ↑ (Aufgang) oder ↓ (Untergang) neben dem Himmels-Icon."""
    a = max(2, int(4 * scale))
    b = max(3, int(6 * scale))
    w = max(1, int(2 * scale))
    if is_rise:
        draw.line((cx, cy + b, cx, cy - a), fill=color, width=w)
        draw.line((cx - a, cy, cx, cy - a), fill=color, width=w)
        draw.line((cx + a, cy, cx, cy - a), fill=color, width=w)
    else:
        draw.line((cx, cy - a, cx, cy + b), fill=color, width=w)
        draw.line((cx - a, cy + 2, cx, cy + b), fill=color, width=w)
        draw.line((cx + a, cy + 2, cx, cy + b), fill=color, width=w)


def draw_astro_event(draw, x: int, y: int,
                     title: str, value: str,
                     is_rise: bool, is_moon: bool,
                     label_font, value_font, pal: dict, scale: float = 1.0):
    """
    Zeichnet ein Sonnen- oder Mond-Ereignis.

    • Sonne: gelbes ☀-Icon (f185) + Richtungspfeil
    • Mond:  silbernes ☾-Icon (f186) + Richtungspfeil
    Der Pfeil ↑/↓ zeigt ob es Auf- oder Untergang ist –
    dadurch brauchen Mondauf- und -untergang kein eigenes Glyph.
    """
    def px(v: float) -> int:
        return max(1, int(v * scale))

    if is_moon:
        glyph      = FA_ICONS.get("moon", "\uf186")
        icon_color = pal.get("moon_icon",  pal["sun_icon"])
        arr_color  = pal.get("moon_arrow", pal["sun_arrow"])
        lbl_color  = pal.get("moon_label", pal["sun_label"])
        val_color  = pal.get("moon_value", pal["sun_value"])
    else:
        glyph      = FA_ICONS.get("sun", "\uf185")
        icon_color = pal["sun_icon"]
        arr_color  = pal["sun_arrow"]
        lbl_color  = pal["sun_label"]
        val_color  = pal["sun_value"]

    fa = load_fa_font(px(22))
    if fa and glyph:
        draw.text((x, y + px(2)), glyph, font=fa, fill=icon_color)
        _draw_astro_arrow(draw, x + px(26), y + px(13), is_rise, arr_color, scale)
        text_x = x + px(40)
    else:
        text_x = x
    draw.text((text_x, y),          title, font=label_font, fill=lbl_color)
    draw.text((text_x, y + px(22)), value, font=value_font,  fill=val_color)


def draw_sun_event(draw, x, y, title, value, icon_name, label_font, value_font, pal: dict):
    """Rückwärtskompatible Alias für draw_astro_event (Sonne)."""
    draw_astro_event(draw, x, y, title, value,
                     is_rise=(icon_name == "sunrise"), is_moon=False,
                     label_font=label_font, value_font=value_font, pal=pal)


# ---------------------------------------------------------------------------
# Mondphase
# ---------------------------------------------------------------------------

def moon_phase_label(phase: float | None) -> str:
    """
    Gibt den deutschen Phasennamen für einen normalisierten Mondphasen-Wert 0–1 zurück.

    DWD liefert Integer 0–7, normalisiert auf exakte Werte:
      0 → 0.000 Neumond
      1 → 0.125 Zunehmende Sichel
      2 → 0.250 Erstes Viertel
      3 → 0.375 Zunehmender Mond
      4 → 0.500 Vollmond
      5 → 0.625 Abnehmender Mond
      6 → 0.750 Letztes Viertel
      7 → 0.875 Abnehmende Sichel
    Schwellwerte bei den Mittelpunkten zwischen je zwei Phasen.
    """
    if phase is None:
        return ""
    p = phase % 1.0
    if p < 0.0625:  return "Neumond"
    if p < 0.1875:  return "Zunehmende Sichel"
    if p < 0.3125:  return "Erstes Viertel"
    if p < 0.4375:  return "Zunehmender Mond"
    if p < 0.5625:  return "Vollmond"
    if p < 0.6875:  return "Abnehmender Mond"
    if p < 0.8125:  return "Letztes Viertel"
    return "Abnehmende Sichel"


def draw_moon_disc(img: Image.Image, cx: int, cy: int, r: int,
                   phase: float, pal: dict) -> None:
    """
    Zeichnet eine geometrisch korrekte Mondphasen-Scheibe.

    phase: 0.0 = Neumond · 0.25 = Erstes Viertel · 0.5 = Vollmond · 0.75 = Letztes Viertel

    Algorithmus:
      1. Dunkle Grundscheibe
      2. Beleuchteten Halbkreis überlagern (rechts bei zunehmend, links bei abnehmend)
      3. Terminator-Ellipse: a = r · cos(phase · 2π)
         a > 0 → dunkle Ellipse verkleinert die lit-Seite   (Sichel → Viertel)
         a < 0 → helle Ellipse erweitert auf die dark-Seite  (Viertel → Gibbous → Vollmond)
    """
    import math as _math

    LIT    = pal.get("moon_disc_lit",    (228, 236, 255, 245))
    DARK   = pal.get("moon_disc_dark",   (12,  22,  44,  220))
    BORDER = pal.get("moon_disc_border", (120, 155, 210, 160))

    pad  = 2
    size = (r + pad) * 2
    moon = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    md   = new_draw(moon, "RGBA")
    ox   = r + pad                                  # Mittelpunkt im moon-Image
    box  = [pad, pad, size - pad - 1, size - pad - 1]

    # 1. Dunkle Grundscheibe
    md.ellipse(box, fill=DARK)

    # 2. Beleuchteter Halbkreis
    if phase <= 0.5:
        md.pieslice(box, -90, 90,  fill=LIT)        # rechts = zunehmend
    else:
        md.pieslice(box, 90,  270, fill=LIT)        # links  = abnehmend

    # 3. Terminator-Ellipse
    a = r * _math.cos(phase * 2 * _math.pi)
    if abs(a) > 0.5:
        ea       = abs(int(round(a)))
        term_box = [ox - ea, pad, ox + ea, size - pad - 1]
        md.ellipse(term_box, fill=DARK if a > 0 else LIT)

    # 4. Rand (auf E-Ink etwas kräftiger, sonst geht er im Dithering unter)
    md.ellipse(box, outline=BORDER, width=max(1, r // 14) if pal.get("flat") else 1)

    # 5. Auf Hauptbild compositen
    img.alpha_composite(moon, dest=(cx - ox, cy - ox))


# ---------------------------------------------------------------------------
# Stat-Panels
# ---------------------------------------------------------------------------

def draw_stat_panel(img, bounds, title, value, icon_name, label_font, value_font, pal: dict,
                    load_font=None, scale: float = 1.0):
    def px(v: float) -> int:
        return max(1, int(v * scale))

    apply_glass_panel(img, bounds, radius=px(26),
                      tint=pal["stat_glass_tint"],
                      outline=pal["stat_glass_outline"],
                      blur_radius=14)
    draw = new_draw(img, "RGBA")
    left, top, _, _ = bounds

    if icon_name == "humidity":
        draw_humidity_icon(draw, left + px(18), top + px(16), pal["stat_icon"], label_font, size=px(20))
    else:
        draw_fa_icon(draw, left + px(18), top + px(18), icon_name, px(18), pal["stat_icon"])
    draw.text((left + px(54), top + px(16)), title, font=label_font, fill=pal["stat_label"])

    value_left = left + px(22)
    value_top  = top + px(54)
    max_value_width = bounds[2] - value_left - px(18)

    lines = value.split("\n")
    if len(lines) > 1:
        # Zweizeiliger Wert (z. B. Wind + Böen): beide Zeilen mit fester Schriftgröße
        line_font = load_font(px(20), True) if load_font else value_font
        for i, line in enumerate(lines):
            draw.text((value_left, value_top + i * px(26)), line,
                      font=line_font, fill=pal["stat_value"])
    else:
        chosen_font = value_font
        if load_font:
            # Schriftgröße schrittweise verkleinern bis der Text passt
            start_size = getattr(value_font, "size", px(28))
            for size in range(start_size, max(9, px(19)), -2):
                candidate = load_font(size, True)
                text_bbox = draw.textbbox((0, 0), value, font=candidate)
                if (text_bbox[2] - text_bbox[0]) <= max_value_width:
                    chosen_font = candidate
                    break
        draw.text((value_left, value_top), value, font=chosen_font, fill=pal["stat_value"])


# ---------------------------------------------------------------------------
# Warnungen
# ---------------------------------------------------------------------------

_WARNING_CARD_GAP = 10
_WARNING_TITLE_H = 30
_WARNING_TITLE_PAD_TOP = 12
_WARNING_TITLE_PAD_BOTTOM = 10
_WARNING_HINT_H = 24
_WARNING_CARD_LIMIT = 2


def _scaled(scale: float):
    """px()-Helfer für einen Maßstab: Maße der 1200-px-Vorlage auf die Bildgröße."""
    def px(v: float) -> int:
        return max(1, int(round(v * scale)))
    return px


def _layout_warning_cards(warnings: list[dict], card_width: int, load_font, scale: float = 1.0,
                          limit: int = _WARNING_CARD_LIMIT) -> list[dict]:
    px = _scaled(scale)
    measure_img = Image.new("RGBA", (max(1, card_width), 220), (0, 0, 0, 0))
    measure_draw = new_draw(measure_img, "RGBA")
    layouts: list[dict] = []

    for warning in warnings[:max(1, limit)]:
        sev_label = warning_level_label(warning.get("level"))
        pill_font = load_font(max(8, px(14)), True)
        pill_bb = measure_draw.textbbox((0, 0), sev_label, font=pill_font)
        pill_tw = pill_bb[2] - pill_bb[0]
        pill_w = max(px(98), pill_tw + px(22))
        pill_h = px(24)

        inner_left = px(12)
        inner_right = px(12)
        headline_x = inner_left + pill_w + px(10)
        headline_width = max(px(80), card_width - headline_x - inner_right)
        headline = warning.get("headline") or warning.get("event") or "Amtliche Wetterwarnung"
        headline_font, headline_lines, headline_line_h, headline_spacing, headline_total_h = fit_wrapped_text(
            measure_draw,
            headline,
            max_width=headline_width,
            max_height=px(30),
            start_size=max(9, px(18)),
            min_size=max(8, px(15)),
            load_font=load_font,
            is_bold=True,
            max_lines=1,
            line_spacing=0.1,
        )

        detail_text = warning.get("description") or warning.get("event") or "Wetterwarnung"
        meta_text = f"{_warning_time_label(warning)} · {detail_text}"
        meta_font, meta_lines, meta_line_h, meta_spacing, meta_total_h = fit_wrapped_text(
            measure_draw,
            meta_text,
            max_width=max(px(80), card_width - inner_left - inner_right),
            max_height=px(54),
            start_size=max(9, px(17)),
            min_size=max(8, px(13)),
            load_font=load_font,
            max_lines=3,
            line_spacing=0.1,
        )

        header_h = max(pill_h, headline_total_h or headline_line_h)
        card_h = px(12) + header_h + px(8) + meta_total_h + px(12)
        layouts.append({
            "warning": warning,
            "pill_text": sev_label,
            "pill_font": pill_font,
            "pill_w": pill_w,
            "pill_h": pill_h,
            "headline_font": headline_font,
            "headline_lines": headline_lines[:1],
            "headline_line_h": headline_line_h,
            "headline_spacing": headline_spacing,
            "meta_font": meta_font,
            "meta_lines": meta_lines[:3],
            "meta_line_h": meta_line_h,
            "meta_spacing": meta_spacing,
            "header_h": header_h,
            "card_h": card_h,
        })

    return layouts


def warning_strip_height(warnings: list[dict], card_width: int, load_font, scale: float = 1.0,
                         limit: int = _WARNING_CARD_LIMIT) -> int:
    if not warnings:
        return 0
    px = _scaled(scale)
    layouts = _layout_warning_cards(warnings, card_width, load_font, scale, limit)
    return (
        px(_WARNING_TITLE_PAD_TOP)
        + px(_WARNING_TITLE_H)
        + px(_WARNING_TITLE_PAD_BOTTOM)
        + sum(layout["card_h"] for layout in layouts)
        + (max(0, len(layouts) - 1) * px(_WARNING_CARD_GAP))
        + (px(_WARNING_HINT_H) if len(warnings) > len(layouts) else 0)
        + px(14)
    )


def warning_card_limit(warnings: list[dict], card_width: int, load_font, scale: float, budget: int) -> int:
    """So viele Warnkarten, wie ins Höhenbudget passen – mindestens eine (der Rest wird als „+N weitere“ genannt)."""
    for limit in range(_WARNING_CARD_LIMIT, 0, -1):
        if warning_strip_height(warnings, card_width, load_font, scale, limit) <= budget:
            return limit
    return 1


def _warning_time_label(warning: dict) -> str:
    start = warning.get("start") or "--:--"
    end = warning.get("end") or "--:--"
    if start == "--:--" and end == "--:--":
        return "Zeit unbekannt"
    return f"{start} - {end}"


def draw_warning_strip(
    img: Image.Image,
    bounds: tuple[int, int, int, int],
    warnings: list[dict],
    title_font,
    small_font,
    pal: dict,
    load_font,
    scale: float = 1.0,
    limit: int = _WARNING_CARD_LIMIT,
) -> None:
    """
    Warnleiste mit bis zu `limit` Karten. Flach (E-Ink): gelbe Leiste, weiße
    Karten mit schwarzem Rand, Stufen-Pille rot (ab Unwetter) oder gelb –
    alles deckend in Panelfarben, keine Tönungen, die zu Dithering-Rauschen würden.
    """
    if not warnings:
        return

    px = _scaled(scale)
    flat = bool(pal.get("flat"))
    left, top, right, bottom = bounds
    apply_glass_panel(
        img,
        bounds,
        radius=px(22),
        tint=pal["warning_glass_tint"],
        outline=pal["warning_glass_outline"],
        blur_radius=0 if flat else 10,
    )
    draw = new_draw(img, "RGBA")

    title_y = top + px(_WARNING_TITLE_PAD_TOP)
    draw_fa_icon(draw, left + px(18), title_y + 1, "warning", px(18), pal["warning_title"])
    count = len(warnings)
    title = "Amtliche Warnungen"
    if count == 1:
        title += " · 1 Warnung"
    else:
        title += f" · {count} Warnungen"
    draw.text((left + px(44), title_y), title, font=title_font, fill=pal["warning_title"])

    card_left = left + px(14)
    card_right = right - px(14)
    card_top = top + px(_WARNING_TITLE_PAD_TOP) + px(_WARNING_TITLE_H) + px(_WARNING_TITLE_PAD_BOTTOM)
    layouts = _layout_warning_cards(warnings, card_right - card_left, load_font, scale, limit)

    current_top = card_top
    for layout in layouts:
        warning = layout["warning"]
        card_bounds = (card_left, current_top, card_right, current_top + layout["card_h"])
        sev_rgb = warning_level_color(warning.get("level"), flat=flat)
        if flat:
            card_tint, card_outline = _spectra("white"), _spectra("black")
            pill_fill = (*sev_rgb, 255)
            pill_text_fill = _spectra("white") if sev_rgb == SPECTRA6_COLORS["red"] else _spectra("black")
            pill_outline = None if sev_rgb == SPECTRA6_COLORS["red"] else _spectra("black")
        else:
            card_tint, card_outline = (*sev_rgb, 34), (*sev_rgb, 120)
            pill_fill, pill_text_fill, pill_outline = (*sev_rgb, 220), (255, 255, 255, 255), None
        apply_glass_panel(
            img,
            card_bounds,
            radius=px(18),
            tint=card_tint,
            outline=card_outline,
            blur_radius=0 if flat else 8,
        )
        draw = new_draw(img, "RGBA")

        pill_left = card_left + px(12)
        pill_top = current_top + px(12)
        pill_h = layout["pill_h"]
        pill_text = layout["pill_text"]
        pill_font = layout["pill_font"]
        pill_bb = draw.textbbox((0, 0), pill_text, font=pill_font)
        pill_tw = pill_bb[2] - pill_bb[0]
        pill_th = pill_bb[3] - pill_bb[1]
        pill_w = layout["pill_w"]
        draw.rounded_rectangle(
            (pill_left, pill_top, pill_left + pill_w, pill_top + pill_h),
            radius=px(12),
            fill=pill_fill,
            outline=pill_outline,
            width=1 if pill_outline else 0,
        )
        draw.text(
            (pill_left + (pill_w - pill_tw) / 2, pill_top + (pill_h - pill_th) / 2 - pill_bb[1]),
            pill_text,
            font=pill_font,
            fill=pill_text_fill,
        )

        draw_lines(
            draw,
            pill_left + pill_w + px(10),
            current_top + px(13),
            layout["headline_lines"],
            layout["headline_font"],
            pal["warning_text"],
            layout["headline_line_h"],
            layout["headline_spacing"],
        )

        meta_y = current_top + px(12) + layout["header_h"] + px(8)
        draw_lines(
            draw,
            pill_left,
            meta_y,
            layout["meta_lines"],
            layout["meta_font"],
            pal["warning_meta"],
            layout["meta_line_h"],
            layout["meta_spacing"],
        )
        current_top += layout["card_h"] + px(_WARNING_CARD_GAP)

    remaining = len(warnings) - len(layouts)
    if remaining > 0:
        hint = f"+{remaining} weitere Warnung" if remaining == 1 else f"+{remaining} weitere Warnungen"
        draw.text((card_left + 2, current_top - px(_WARNING_CARD_GAP) + px(4)), hint, font=small_font, fill=pal["warning_meta"])


# ---------------------------------------------------------------------------
# Stundenverlauf
# ---------------------------------------------------------------------------

def interpolate_curve(points, segments=14):
    if len(points) <= 2:
        return points
    smoothed = []
    for index in range(len(points) - 1):
        p0 = points[index - 1] if index > 0 else points[index]
        p1 = points[index]
        p2 = points[index + 1]
        p3 = points[index + 2] if index + 2 < len(points) else points[index + 1]
        for step in range(segments):
            t = step / segments
            t2, t3 = t * t, t * t * t
            x = 0.5 * ((2 * p1[0]) + (-p0[0] + p2[0]) * t
                        + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2
                        + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * ((2 * p1[1]) + (-p0[1] + p2[1]) * t
                        + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2
                        + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            smoothed.append((x, y))
    smoothed.append(points[-1])
    return smoothed


def _draw_hatched_polygon(img: Image.Image, points, color, spacing: int = 9, width: int = 2) -> None:
    """
    Schraffur statt Fläche. Auf dem Spectra-6-Panel wurde aus der blauen
    Temperaturfläche ein massiver Block; feine 45°-Linien bleiben luftig und
    dithern nicht, weil nur echte Panelfarben gezeichnet werden.
    """
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    x0, y0, x1, y1 = int(min(xs)), int(min(ys)), int(max(xs)) + 1, int(max(ys)) + 1
    w, h = x1 - x0, y1 - y0
    if w <= 0 or h <= 0:
        return
    mask = Image.new("L", (w, h), 0)
    new_draw(mask).polygon([(x - x0, y - y0) for x, y in points], fill=255)
    hatch = Image.new("L", (w, h), 0)
    hd = new_draw(hatch)
    for d in range(-h, w + h, max(3, spacing)):
        hd.line((d, h, d + h, 0), fill=255, width=max(1, width))
    layer = Image.new("RGBA", (w, h), (*color[:3], 255))
    layer.putalpha(ImageChops.multiply(mask, hatch))
    img.alpha_composite(layer, dest=(x0, y0))


def draw_hourly_strip(draw, bounds, hourly_points, title_font, time_font, temp_font, pal: dict,
                      img: Image.Image | None = None, scale: float = 1.0):
    """
    Tagesverlauf-Graph mit dynamischer 5°-Temperaturachse links.
    img: für die Schraffur im flachen (E-Ink-)Theme; ohne img wird die Fläche gefüllt.
    """
    def px(v: float) -> int:
        return max(1, int(v * scale))

    left, top, right, bottom = bounds

    max_day_offset = max((int(p.get("day_offset", 1 if p.get("is_next_day") else 0)) for p in (hourly_points or [])), default=0)
    has_next_day = max_day_offset >= 1
    has_second_next_day = max_day_offset >= 2
    if has_second_next_day:
        title_text = "Verlauf heute, morgen & übermorgen früh"
    elif has_next_day:
        title_text = "Verlauf heute & morgen früh"
    else:
        title_text = "Heute im Verlauf"
    draw.text((left, top), title_text, font=title_font, fill=pal["chart_title"])

    if not hourly_points:
        draw.text((left, top + px(36)), "Keine Stundenwerte verfügbar",
                  font=time_font, fill=pal["chart_nodata"])
        return

    AXIS_W = px(38)          # Breite für Temperaturskala links

    icon_row_y     = top + px(38)
    temp_row_y     = top + px(76)
    chart_y_top    = temp_row_y + px(30)
    chart_y_bottom = bottom - px(30)
    time_row_y     = bottom - px(24)
    chart_left     = left + AXIS_W    # Datenfläche beginnt rechts der Skala

    chart_height = chart_y_bottom - chart_y_top
    if chart_height < px(30):
        return

    temps = [p.get("temp_c") for p in hourly_points if p.get("temp_c") is not None]
    if not temps:
        temps = [0.0]
    min_temp = min(temps)
    max_temp = max(temps)

    # Achse auf 5°-Schritte runden
    axis_min = math.floor(min_temp / 5) * 5
    axis_max = math.ceil(max_temp / 5) * 5
    if axis_max == axis_min:
        axis_max = axis_min + 5
    spread = float(axis_max - axis_min)

    step_x = (right - chart_left) / max(len(hourly_points) - 1, 1)

    # Datenpunkt-Koordinaten relativ zur gerundeten Achse
    points = []
    for i, point in enumerate(hourly_points):
        px_x = chart_left + i * step_x
        temp = point.get("temp_c") if point.get("temp_c") is not None else float(axis_min)
        norm = (temp - axis_min) / spread
        py   = chart_y_bottom - norm * chart_height
        points.append((px_x, py))

    # Gitternetz-Linien und Temperaturachse in 5°-Schritten
    ticks = list(range(axis_min, axis_max + 1, 5))
    for tick in ticks:
        norm = (tick - axis_min) / spread
        gy   = int(chart_y_bottom - norm * chart_height)
        if not (chart_y_top - 2 <= gy <= chart_y_bottom + 2):
            continue
        line_col = pal["baseline"] if tick == axis_min else pal["grid"]
        draw.line((chart_left, gy, right - px(4), gy), fill=line_col, width=1)
        lbl    = f"{tick}°"
        bb     = draw.textbbox((0, 0), lbl, font=time_font)
        lbl_h  = bb[3] - bb[1]
        lbl_w  = bb[2] - bb[0]
        draw.text((left + AXIS_W - lbl_w - px(4), gy - lbl_h // 2),
                  lbl, font=time_font, fill=pal["axis_label"])

    # Trennlinien bei Tageswechseln (gestrichelt)
    transition_labels = {
        1: "morgen",
        2: "übermorgen",
    }
    grid_color = pal["grid"]
    sep_color = (*grid_color[:3], min(255, (grid_color[3] if len(grid_color) > 3 else 200) + 80))
    for day_boundary in range(1, max_day_offset + 1):
        boundary_idx = next(
            (
                i for i, p in enumerate(hourly_points)
                if int(p.get("day_offset", 1 if p.get("is_next_day") else 0)) >= day_boundary
            ),
            None,
        )
        if boundary_idx is None or boundary_idx <= 0:
            continue
        sep_x = int(chart_left + (boundary_idx - 0.5) * step_x)
        dash_y = icon_row_y
        while dash_y < time_row_y + px(8):
            draw.line((sep_x, dash_y, sep_x, min(dash_y + px(7), time_row_y + px(8))),
                      fill=sep_color, width=1)
            dash_y += px(12)
        lbl = transition_labels.get(day_boundary, f"+{day_boundary}d")
        lbl_bb = draw.textbbox((0, 0), lbl, font=time_font)
        lbl_w = lbl_bb[2] - lbl_bb[0]
        draw.text((sep_x - lbl_w // 2, icon_row_y - px(16)),
                  lbl, font=time_font, fill=pal["axis_label"])

    # Kurve – flach (E-Ink) als Schraffur, sonst als Fläche
    curve_points = interpolate_curve(points)
    if len(curve_points) > 1:
        fill_pts = [(curve_points[0][0], chart_y_bottom)] + curve_points + [(curve_points[-1][0], chart_y_bottom)]
        if pal.get("flat") and img is not None:
            _draw_hatched_polygon(img, fill_pts, pal["curve_fill"], spacing=max(6, px(9)), width=max(1, px(2)))
        else:
            draw.polygon(fill_pts, fill=pal["curve_fill"])
        draw.line(curve_points, fill=pal["curve_line"], width=max(2, px(4)))

    # Datenpunkte und Beschriftung
    r_outer, r_inner = max(2, px(5)), max(1, px(3))
    for i, point in enumerate(hourly_points):
        px_x, py = points[i]
        day_offset = int(point.get("day_offset", 1 if point.get("is_next_day") else 0))
        # Flat-Themes (E-Ink): keine Transparenz, sonst dithert es
        if pal.get("flat"):
            alpha = None
        else:
            alpha = 160 if day_offset == 1 else 110 if day_offset >= 2 else None
        outer_fill = (*pal["point_outer"][:3], alpha) if alpha is not None else pal["point_outer"]
        inner_fill = (*pal["point_inner"][:3], alpha) if alpha is not None else pal["point_inner"]
        icon_fill  = (*pal["hourly_icon"][:3], alpha) if alpha is not None else pal["hourly_icon"]
        temp_fill  = (*pal["hourly_temp"][:3], alpha) if alpha is not None else pal["hourly_temp"]
        time_fill  = (*pal["hourly_time"][:3], alpha) if alpha is not None else pal["hourly_time"]

        draw.ellipse((px_x - r_outer, py - r_outer, px_x + r_outer, py + r_outer), fill=outer_fill)
        draw.ellipse((px_x - r_inner, py - r_inner, px_x + r_inner, py + r_inner), fill=inner_fill)

        draw_weather_icon(draw, int(px_x - px(10)), int(icon_row_y), point.get("icon_code"),
                          px(18), icon_fill, temp_hint_c=point.get("temp_c"))

        temp_text = format_temp(point.get("temp_c"))
        t_bbox = draw.textbbox((0, 0), temp_text, font=temp_font)
        t_w    = t_bbox[2] - t_bbox[0]
        draw.text((px_x - t_w / 2, temp_row_y), temp_text, font=temp_font, fill=temp_fill)

        time_text = point.get("time", "--:--")
        ti_bbox = draw.textbbox((0, 0), time_text, font=time_font)
        ti_w    = ti_bbox[2] - ti_bbox[0]
        draw.text((px_x - ti_w / 2, time_row_y), time_text, font=time_font, fill=time_fill)


# ---------------------------------------------------------------------------
# Kompakter Prognose-Streifen
# ---------------------------------------------------------------------------

def draw_compact_forecast_strip(img, bounds, forecast_days, day_font, temp_font, meta_font, pal: dict,
                                scale: float = 1.0):
    """Mehrtages-Vorschau-Streifen mit Palette. Zeigt UV-Index wenn vorhanden."""
    from .dwd_uv import uv_level_color, uv_level_label

    if not forecast_days:
        return

    def px(v: float) -> int:
        return max(1, int(v * scale))

    left, top, right, bottom = bounds
    h = bottom - top
    count = len(forecast_days)
    gap = px(10)
    card_w = int((right - left - gap * (count - 1)) / count)

    # ── Zeilen von unten aufbauen: so viele Meta-Zeilen, wie unter der Temperatur Platz ist ──
    ROW_H      = px(22)   # Zeilenhöhe (Icon 16 px + 6 px Luft)
    BOTTOM_PAD = px(10)   # Abstand Unterkante Karte
    DAY_LABEL_BOTTOM = top + px(30)   # Label (19 px) + 8 px oben + Luft
    TEMP_H = px(24)

    has_uv = any(day.get("uvi_max") is not None for day in forecast_days)

    def fits(rows: int) -> bool:
        return DAY_LABEL_BOTTOM + TEMP_H + rows * ROW_H + BOTTOM_PAD <= bottom

    if has_uv and fits(3):
        meta_rows, show_uv = 3, True
    elif fits(2):
        meta_rows, show_uv = 2, False
    else:
        meta_rows, show_uv = 0, False       # nur Tag und Temperatur – lieber weniger als überlappend

    uv_y = wind_y = sun_y = None
    if meta_rows:
        cursor = bottom - BOTTOM_PAD
        if show_uv:
            cursor -= ROW_H
            uv_y = cursor
        cursor -= ROW_H
        wind_y = cursor
        cursor -= ROW_H
        sun_y = cursor
        meta_top = sun_y
    else:
        meta_top = bottom - BOTTOM_PAD

    # Temperatur vertikal zwischen Tag-Label-Bereich und Meta-Bereich zentrieren
    avail_for_temp = meta_top - DAY_LABEL_BOTTOM
    temp_y = DAY_LABEL_BOTTOM + max(0, (avail_for_temp - TEMP_H) // 2)

    for idx, day in enumerate(forecast_days):
        card_left  = left + idx * (card_w + gap)
        card_right = card_left + card_w
        apply_glass_panel(
            img,
            (card_left, top, card_right, bottom),
            radius=px(18),
            tint=pal["fc_glass_tint"],
            outline=pal["fc_glass_outline"],
            blur_radius=10,
        )
        draw = new_draw(img, "RGBA")
        pad = px(12)

        draw_weather_icon(draw, card_right - px(38), top + px(8), day.get("icon_code"),
                          px(24), pal["fc_icon"], temp_hint_c=day.get("max_temp_c"))

        draw.text((card_left + pad, top + px(10)), format_day_label(day.get("day_date", "")),
                  font=day_font, fill=pal["fc_day"])

        temp_text = f"{format_temp(day.get('max_temp_c'))} / {format_temp(day.get('min_temp_c'))}"
        draw.text((card_left + pad, temp_y), temp_text,
                  font=temp_font, fill=pal["fc_temp"])

        if meta_rows:
            sun_text  = day.get("sunshine_text") or "--"
            wind_kmh  = day.get("wind_kmh")
            wind_dir  = day.get("wind_dir_label") or ""
            wind_text = f"{wind_kmh} km/h {wind_dir}" if wind_kmh is not None else "--"
            uvi       = day.get("uvi_max")

            fa     = load_fa_font(px(16))   # Icons etwas größer als bisher (13 → 16)
            ICON_W = px(20)                 # Platz den das Icon horizontal belegt

            # Sonne (Scheindauer)
            if fa:
                draw.text((card_left + pad,          sun_y),
                          FA_ICONS["sun"], font=fa, fill=pal["fc_sun_icon"])
                draw.text((card_left + pad + ICON_W, sun_y + 1),
                          sun_text, font=meta_font, fill=pal["fc_sun_text"])
            else:
                draw.text((card_left + pad, sun_y + 1), sun_text,
                          font=meta_font, fill=pal["fc_sun_text"])

            # Wind
            if fa:
                draw.text((card_left + pad,          wind_y),
                          FA_ICONS["wind"], font=fa, fill=pal["fc_wind_icon"])
                draw.text((card_left + pad + ICON_W, wind_y + 1),
                          wind_text, font=meta_font, fill=pal["fc_wind_text"])
            else:
                draw.text((card_left + pad, wind_y + 1), wind_text,
                          font=meta_font, fill=pal["fc_wind_text"])

            # UV-Index
            if show_uv and uv_y is not None:
                uv_rgba = _uv_icon_color(uvi) if pal.get("flat") else (*uv_level_color(uvi), 230)
                uv_lbl  = uv_level_label(uvi)
                uv_val  = f"{uvi:.0f}" if uvi is not None else "--"
                uv_str  = f"{uv_val}  {uv_lbl}"

                if fa:
                    draw.text((card_left + pad,          uv_y),
                              FA_ICONS.get("radiation") or FA_ICONS["sun"], font=fa, fill=uv_rgba)
                    draw.text((card_left + pad + ICON_W, uv_y + 1),
                              uv_str, font=meta_font, fill=pal["fc_uv_text"])
                else:
                    draw.text((card_left + pad, uv_y + 1), uv_str,
                              font=meta_font, fill=uv_rgba)


def _uv_icon_color(uvi: float | None) -> tuple[int, int, int, int]:
    """UV-Stufe in Panelfarben: ohne Wert schwarz, gering grün, mittel gelb, ab hoch rot."""
    if uvi is None:
        return _spectra("black")
    if uvi <= 2:
        return _spectra("green")
    if uvi <= 5:
        return _spectra("yellow")
    return _spectra("red")


# ---------------------------------------------------------------------------
# Pollen-Strip
# ---------------------------------------------------------------------------

_POLLEN_DAY_KEYS   = ("today",  "tomorrow", "dayafter_to")
_POLLEN_DAY_LABELS = ("Heute",  "Morgen",   "Überm.")


def _pollen_load_color(value: float | None, flat: bool = False) -> tuple[int, int, int]:
    """Belastungswert 0..3 → RGB. flat=True nutzt nur Spectra-6-Farben (E-Ink)."""
    if flat:
        if value is None or value == 0.0:
            return SPECTRA6_COLORS["white"]
        if value <= 1.0:
            return SPECTRA6_COLORS["green"]
        if value <= 2.0:
            return SPECTRA6_COLORS["yellow"]
        return SPECTRA6_COLORS["red"]
    if value is None or value == 0.0:
        return (148, 155, 165)   # Grau  – keine Belastung
    if value <= 1.0:
        return (38,  165, 70)    # Grün  – gering
    if value <= 2.0:
        return (215, 145, 10)    # Amber – mittel
    return (205, 38,  38)        # Rot   – hoch


def _pollen_load_label(value: float | None) -> str:
    if value is None:  return "–"
    if value == 0.0:   return "0"
    if value <= 0.5:   return "0–1"
    if value <= 1.0:   return "1"
    if value <= 1.5:   return "1–2"
    if value <= 2.0:   return "2"
    if value <= 2.5:   return "2–3"
    return "3"


_POLLEN_ROW_H   = 54   # Höhe einer Allergen-Zeile
_POLLEN_ROW_GAP = 10   # Abstand zwischen Zeilen
_POLLEN_PAD_H   = 20   # horizontaler Innenabstand
_POLLEN_PAD_TOP = 14   # Abstand oben
_POLLEN_PAD_BOT = 16   # Abstand unten
_POLLEN_TITLE_H = 34   # Höhe der Titelzeile
_POLLEN_GAP_T   = 10   # Abstand Titel → Daten
_POLLEN_BIG_R   = 11   # Radius des Haupt-Farbpunktes
_POLLEN_SMALL_R = 8    # Radius der Tages-Farbpunkte
_POLLEN_NAME_W  = 120  # Reservierter Platz für Allergen-Namen (px)
_POLLEN_DAY_W   = 80   # Reservierter Platz pro Tag-Spalte (px)
_POLLEN_CHIP_H  = 36   # Höhe eines Chips (kompakte Darstellung)
_POLLEN_CHIP_GAP = 8   # Abstand zwischen Chips
_POLLEN_NO_LOAD_GRAY = (148, 155, 165)


def _allergen_has_load(days: dict) -> bool:
    """True wenn mindestens einer der drei Tage eine Belastung > 0 hat."""
    return any(
        v is not None and v > 0
        for v in (days.get(k) for k in _POLLEN_DAY_KEYS)
    )


def _peak(days: dict) -> float | None:
    valid = [v for v in (days.get(k) for k in _POLLEN_DAY_KEYS) if v is not None]
    return max(valid) if valid else None


def _chip_width_estimate(label: str, value_label: str, active: bool, s: float) -> int:
    """Breite eines Chips ohne Font: Name ~12 px/Zeichen (fett 19), Wert ~10 px/Zeichen (18)."""
    w = 12 + 20 + 8 + 12 * len(label) + 8 + 10 * len(value_label) + 12
    if active:
        w += 6 + 3 * 16
    return int(w * s)


def plan_pollen_layout(allergens: dict, width: int, max_height: int | None = None, scale: float = 1.0) -> dict:
    """
    Wählt die Darstellung des Pollen-Strips und liefert die Höhe dazu:

    - "none":  keine Daten → nur Titel und Hinweis
    - "text":  kein Allergen hat Flug → eine Textzeile
    - "grid":  Raster mit zwei Spalten, drei Tageswerte je Allergen
    - "chips": kompakte Chips (Peak der drei Tage), wenn das Raster nicht in
               max_height passt – z. B. bei vielen Allergenen mit „0-1“ oder
               auf kleinen Bildern. Das Raster würde sonst Stunden- und
               Tagesvorschau zusammenquetschen.

    Rückgabe: {"mode", "height", "rows" (nur chips: Liste von Zeilen mit
    Allergen-Schlüsseln), "dropped" (chips: Allergene ohne Flug, für die kein Platz war)}.
    """
    from .dwd_pollen import ALLERGEN_LABELS

    s = max(0.4, scale)
    head = int((_POLLEN_PAD_TOP + _POLLEN_TITLE_H + _POLLEN_GAP_T) * s)
    foot = int(_POLLEN_PAD_BOT * s)
    single_line = head + int(36 * s) + foot

    if not allergens:
        return {"mode": "none", "height": single_line, "rows": [], "dropped": []}

    active = [a for a, d in allergens.items() if _allergen_has_load(d)]
    inactive = [a for a in allergens if a not in active]
    if not active:
        return {"mode": "text", "height": single_line, "rows": [], "dropped": []}

    n_active = len(active)
    cols = 2 if n_active > 1 else 1
    rows = (n_active + cols - 1) // cols
    grid_h = int((rows * _POLLEN_ROW_H + max(0, rows - 1) * _POLLEN_ROW_GAP) * s)
    extra = int((_POLLEN_ROW_GAP + 30) * s) if inactive else 0
    grid_total = head + grid_h + extra + foot
    grid_fits_width = width - int(2 * _POLLEN_PAD_H * s) >= cols * int((_POLLEN_BIG_R * 2 + 7 + _POLLEN_NAME_W + 3 * _POLLEN_DAY_W) * s)
    if (max_height is None or grid_total <= max_height) and grid_fits_width:
        return {"mode": "grid", "height": grid_total, "rows": [], "dropped": []}

    # Chips: aktive zuerst, dann die ohne Flug – so viele Zeilen, wie hineinpassen
    usable = width - int(2 * _POLLEN_PAD_H * s)
    chip_h = int(_POLLEN_CHIP_H * s)
    chip_gap = int(_POLLEN_CHIP_GAP * s)
    ordered = [(a, True) for a in active] + [(a, False) for a in inactive]
    chip_rows: list[list[str]] = []
    row: list[str] = []
    row_w = 0
    for allergen, is_active in ordered:
        cw = _chip_width_estimate(ALLERGEN_LABELS.get(allergen, allergen), _pollen_load_label(_peak(allergens[allergen])), is_active, s)
        if row and row_w + chip_gap + cw > usable:
            chip_rows.append(row)
            row, row_w = [], 0
        row.append(allergen)
        row_w += (chip_gap if row_w else 0) + cw
    if row:
        chip_rows.append(row)

    def chips_height(n_rows: int) -> int:
        return head + n_rows * chip_h + max(0, n_rows - 1) * chip_gap + foot

    dropped: list[str] = []
    if max_height is not None:
        # Zeilen von hinten streichen, aber nie eine Zeile mit aktiven Allergenen
        while len(chip_rows) > 1 and chips_height(len(chip_rows)) > max_height \
                and all(a in inactive for a in chip_rows[-1]):
            dropped = chip_rows.pop() + dropped
    return {"mode": "chips", "height": chips_height(len(chip_rows)), "rows": chip_rows, "dropped": dropped}


def pollen_strip_height(allergens: dict, width: int = 1200, max_height: int | None = None, scale: float = 1.0) -> int:
    """Benötigte Höhe des Pollen-Strips (siehe plan_pollen_layout)."""
    return plan_pollen_layout(allergens, width, max_height, scale)["height"]


def _pollen_dot(draw: ImageDraw.ImageDraw, cx: int, cy: int, r: int, value: float | None, pal: dict, alpha: int = 225) -> None:
    """
    Farbpunkt für einen Belastungswert. Flach (E-Ink): „0“ ist ein hohler Kreis
    – ein weißer Punkt auf weißem Grund wäre unsichtbar, und genau so sah der
    Strip auf dem Panel bei lauter „0“ und „0-1“ aus wie zusammengefallen.
    """
    flat = bool(pal.get("flat"))
    color = _pollen_load_color(value, flat=flat)
    box = [(cx - r, cy - r), (cx + r, cy + r)]
    if flat and (value is None or value == 0.0):
        draw.ellipse(box, fill=(*SPECTRA6_COLORS["white"], 255), outline=(*SPECTRA6_COLORS["black"], 255), width=2)
    else:
        draw.ellipse(box, fill=(*color, 255 if flat else alpha))


def _soft(pal: dict, color: tuple, alpha: int) -> tuple:
    """Zurückgenommene Farbe – flach (E-Ink) deckend, sonst mit alpha. Halbtransparentes Schwarz wäre Grau, und Grau dithert."""
    return (*color[:3], 255 if pal.get("flat") else alpha)


def _pollen_value_color(value: float | None, pal: dict) -> tuple:
    """Textfarbe eines Tageswerts: flach immer schwarz, sonst die Belastungsfarbe (grau bleibt Label-Farbe)."""
    if pal.get("flat"):
        return pal["pollen_label"]
    color = _pollen_load_color(value)
    return pal["pollen_label"] if color == _POLLEN_NO_LOAD_GRAY else (*color, 235)


def draw_pollen_strip(img: Image.Image, bounds: tuple,
                      pollen_data: dict,
                      title_font, label_font, small_font,
                      pal: dict, plan: dict | None = None, scale: float = 1.0) -> None:
    """
    Pollenleiste. Die Darstellung kommt aus plan_pollen_layout() (Raster mit
    max. 2 Spalten oder kompakte Chips); ohne plan wird sie aus den Grenzen
    bestimmt.

    Raster: Allergene ohne jeglichen Pollenflug (alle 3 Tage = 0) werden nicht
    gezeigt, sondern kompakt als Textzeile aufgelistet. Haben ALLE Allergene
    keinen Pollenflug, entfällt das Raster ganz.
    Chips: ein Chip je Allergen mit Peak-Wert und drei kleinen Tagespunkten.
    """
    from .dwd_pollen import ALLERGEN_LABELS

    left, top, right, bottom = bounds
    w = right - left
    s = max(0.4, scale)

    def px(v: float) -> int:
        return max(1, int(v * s))

    allergens: dict = pollen_data.get("allergens", {})
    if plan is None:
        plan = plan_pollen_layout(allergens, w, bottom - top, s)
    mode = plan["mode"]

    apply_glass_panel(img, bounds, radius=px(22),
                      tint=pal["pollen_glass_tint"],
                      outline=pal["pollen_glass_outline"],
                      blur_radius=8)
    draw = new_draw(img, "RGBA")

    pad_h = px(_POLLEN_PAD_H)
    active   = {a: d for a, d in allergens.items() if _allergen_has_load(d)}
    inactive = [ALLERGEN_LABELS.get(a, a) for a in allergens if a not in active]

    # ── Titelzeile ────────────────────────────────────────────────────────
    title_y   = top + px(_POLLEN_PAD_TOP)
    title_mid = title_y + px(_POLLEN_TITLE_H) // 2
    draw.text((left + pad_h, title_y + px(4)), "Pollenflug", font=title_font, fill=pal["pollen_title"])
    title_w = int(draw.textlength("Pollenflug", font=title_font))

    if mode == "none":
        draw.text((left + pad_h + title_w + px(24), title_y + px(4)),
                  "keine Daten", font=label_font, fill=pal["pollen_label"])
        return

    if mode == "text":
        text_y = title_y + px(_POLLEN_TITLE_H) + px(_POLLEN_GAP_T)
        draw.text((left + pad_h, text_y), f"Kein Pollenflug: {', '.join(inactive)}",
                  font=label_font, fill=pal["pollen_label"])
        return

    data_top = top + px(_POLLEN_PAD_TOP) + px(_POLLEN_TITLE_H) + px(_POLLEN_GAP_T)

    # ── Legende rechts in der Titelzeile ──────────────────────────────────
    if mode == "grid":
        leg_d, leg_gap = px(12), px(18)
        lx = right - pad_h
        for lbl in reversed(_POLLEN_DAY_LABELS):
            bb = draw.textbbox((0, 0), lbl, font=small_font)
            tw = max(bb[2] - bb[0], 1)
            lx -= tw
            draw.text((lx, title_mid - (bb[3] - bb[1]) // 2), lbl, font=small_font, fill=pal["pollen_title"])
            lx -= leg_d + px(5)
            draw.ellipse([(lx, title_mid - leg_d // 2), (lx + leg_d, title_mid + leg_d // 2)],
                         fill=_soft(pal, pal["pollen_title"], 170))
            lx -= leg_gap
    else:
        # Chips: rechts steht, welche Allergene ohne Flug keinen Platz mehr hatten
        dropped = [ALLERGEN_LABELS.get(a, a) for a in plan.get("dropped", [])]
        if dropped:
            note = f"ohne Flug: {', '.join(dropped)}"
            max_w = right - pad_h - (left + pad_h + title_w + px(24))
            if draw.textlength(note, font=small_font) > max_w:
                note = f"{len(dropped)} ohne Flug"
            draw.text((right - pad_h - draw.textlength(note, font=small_font), title_y + px(8)),
                      note, font=small_font, fill=_soft(pal, pal["pollen_label"], 180))

    # ── Chips ─────────────────────────────────────────────────────────────
    if mode == "chips":
        chip_h, chip_gap = px(_POLLEN_CHIP_H), px(_POLLEN_CHIP_GAP)
        big_r, mini_r = px(10), px(5)
        flat = bool(pal.get("flat"))
        y = data_top
        for row in plan["rows"]:
            x = left + pad_h
            for allergen in row:
                days = allergens[allergen]
                is_active = allergen in active
                name = ALLERGEN_LABELS.get(allergen, allergen)
                peak = _peak(days)
                value_label = _pollen_load_label(peak)
                name_w = int(draw.textlength(name, font=label_font))
                value_w = int(draw.textlength(value_label, font=small_font))
                cw = px(12) + big_r * 2 + px(8) + name_w + px(8) + value_w + (px(6) + 3 * (mini_r * 2 + px(6)) if is_active else 0) + px(12)
                mid = y + chip_h // 2
                outline = (*pal["pollen_glass_outline"][:3], 255 if flat else 90)
                draw.rounded_rectangle([(x, y), (x + cw, y + chip_h)], radius=0 if flat else chip_h // 2,
                                       fill=(*pal["pollen_glass_tint"][:3], 0 if flat else 60), outline=outline, width=1)
                cx = x + px(12) + big_r
                _pollen_dot(draw, cx, mid, big_r, peak, pal)
                tx = cx + big_r + px(8)
                bb = draw.textbbox((0, 0), name, font=label_font)
                draw.text((tx, mid - (bb[3] - bb[1]) // 2 - bb[1]), name, font=label_font,
                          fill=pal["pollen_label"] if is_active else _soft(pal, pal["pollen_label"], 170))
                tx += name_w + px(8)
                bb = draw.textbbox((0, 0), value_label, font=small_font)
                draw.text((tx, mid - (bb[3] - bb[1]) // 2 - bb[1]), value_label, font=small_font,
                          fill=_pollen_value_color(peak, pal))
                tx += value_w + px(6)
                if is_active:
                    for day_val in (days.get(k) for k in _POLLEN_DAY_KEYS):
                        tx += mini_r
                        _pollen_dot(draw, tx, mid, mini_r, day_val, pal, alpha=218)
                        tx += mini_r + px(6)
                x += cw + chip_gap
            y += chip_h + chip_gap
        return

    # ── Datenraster für aktive Allergene ─────────────────────────────────
    n       = len(active)
    cols    = 2 if n > 1 else 1
    col_gap = px(18)
    col_w   = (w - 2 * pad_h - (cols - 1) * col_gap) // cols
    row_h, row_gap = px(_POLLEN_ROW_H), px(_POLLEN_ROW_GAP)
    big_r, small_r = px(_POLLEN_BIG_R), px(_POLLEN_SMALL_R)

    for idx, (allergen, days) in enumerate(active.items()):
        col_idx  = idx % cols
        row_idx  = idx // cols

        col_left = left + pad_h + col_idx * (col_w + col_gap)
        row_top  = data_top + row_idx * (row_h + row_gap)
        mid_y    = row_top + row_h // 2

        if mid_y + big_r > bottom:
            continue

        label_txt  = ALLERGEN_LABELS.get(allergen, allergen)
        day_values = [days.get(k) for k in _POLLEN_DAY_KEYS]

        # Haupt-Farbpunkt (Peak der 3 Tage)
        dot_cx = col_left + big_r
        _pollen_dot(draw, dot_cx, mid_y, big_r, _peak(days), pal)

        # Allergen-Name
        name_x = col_left + big_r * 2 + px(7)
        name_y = row_top + (row_h - px(18)) // 2
        draw.text((name_x, name_y), label_txt, font=label_font, fill=pal["pollen_label"])

        # Drei Tageswerte
        day_x = name_x + px(_POLLEN_NAME_W)
        for day_val in day_values:
            _pollen_dot(draw, day_x + small_r, mid_y, small_r, day_val, pal, alpha=218)
            val_y = row_top + (row_h - px(20)) // 2
            draw.text((day_x + small_r * 2 + px(5), val_y), _pollen_load_label(day_val),
                      font=small_font, fill=_pollen_value_color(day_val, pal))
            day_x += px(_POLLEN_DAY_W)

    # ── Kompakter Hinweis für inaktive Allergene ──────────────────────────
    if inactive:
        rows_used = (n + cols - 1) // cols
        hint_y = data_top + rows_used * row_h + max(0, rows_used - 1) * row_gap + row_gap
        draw.text((left + pad_h, hint_y), f"Kein Pollenflug: {', '.join(inactive)}",
                  font=small_font, fill=_soft(pal, pal["pollen_label"], 160))


# ---------------------------------------------------------------------------
# Haupt-Render-Funktion
# ---------------------------------------------------------------------------

def render_dwd_weather_module(context: ModuleRenderServices, content: object) -> Image.Image:
    """
    Vollbild. Entworfen für 1200×1600; kleinere Bilder (600×800, 800×480)
    skalieren Maße und Schriften mit s = min(1, Breite/1200, Höhe/1200),
    größere behalten die Originalgröße und bekommen mehr Luft.
    """
    data = content if isinstance(content, dict) else {}
    rw, rh = context.render_width, context.render_height
    theme = getattr(context, "display_theme", "dark")
    pal = get_dwd_palette(theme)
    s = max(0.35, min(1.0, rw / 1200.0, rh / 1200.0))

    def px(v: float) -> int:
        return max(1, int(round(v * s)))

    # Hintergrund
    if pal.get("flat"):
        img = Image.new("RGBA", (rw, rh), (*pal["bg_top"], 255))   # E-Ink: flach, kein Verlauf
    elif theme == "light":
        img = create_weather_background_light(rw, rh).convert("RGBA")
    else:
        img = create_weather_background(rw, rh).convert("RGBA")
    draw = new_draw(img, "RGBA")

    # Schriften
    font_idle          = context.load_font(px(32), False)
    font_eyebrow       = context.load_font(px(28), True)
    font_big           = context.load_font(px(88), True)
    font_condition     = context.load_font(px(34), True)
    font_value         = context.load_font(px(28), True)
    font_label         = context.load_font(px(21), True)
    flat               = bool(pal.get("flat"))
    font_micro         = context.load_font(px(18 if flat else 16), False)
    font_warning_title = context.load_font(px(22), True)
    font_chart_title   = context.load_font(px(22), True)
    font_chart_time    = context.load_font(px(16 if flat else 13), False)
    font_chart_temp    = context.load_font(px(18 if flat else 16), True)
    font_forecast_day  = context.load_font(px(20), True)
    font_forecast_temp = context.load_font(px(22), True)
    font_forecast_meta = context.load_font(px(19), False)

    # Kopfzeile
    draw.text((px(70), px(52)), format_date_long(),
              font=font_idle, fill=pal["header_idle"])
    station_label = data.get("station_name") or f"DWD-Station {data.get('station_id', '--')}"
    draw.text((px(70), px(92)), f"DWD Wetter  ·  {station_label}",
              font=font_eyebrow, fill=pal["header_station"])

    # Haupt-Panel
    panel_left   = px(50)
    panel_right  = rw - px(50)
    panel_top    = px(134)
    panel_bottom = rh - px(18)
    panel_w      = panel_right - panel_left
    inset        = px(42)

    apply_glass_panel(img, (panel_left, panel_top, panel_right, panel_bottom),
                      radius=px(34), tint=pal["panel_tint"], outline=pal["panel_outline"],
                      blur_radius=0)
    draw = new_draw(img, "RGBA")

    # Zone 1: Aktuelles Wetter
    cur_top   = panel_top + px(20)
    cur_left  = panel_left + inset
    cur_right = panel_right - inset

    icon_size = px(130)
    icon_x = cur_right - icon_size - px(10)
    draw_weather_icon(draw, icon_x, cur_top + px(8), data.get("current_icon_code"),
                      icon_size, pal["icon_main"],
                      temp_hint_c=data.get("current_temp_c"))

    current_temp  = format_temp(data.get("current_temp_c"))
    current_label = data.get("current_label") or "Keine aktuellen Daten"
    today         = data.get("today", {})
    today_min     = format_temp(today.get("min_temp_c"))
    today_max     = format_temp(today.get("max_temp_c"))

    draw.text((cur_left, cur_top + px(10)),  current_temp,  font=font_big,       fill=pal["temp_big"])
    draw.text((cur_left, cur_top + px(116)), current_label, font=font_condition,  fill=pal["condition"])

    # Heute-Badge (Min/Max)
    badge_top    = cur_top + px(162)
    badge_bottom = badge_top + px(42)
    badge_w      = px(290)
    draw.rounded_rectangle(
        (cur_left, badge_top, cur_left + badge_w, badge_bottom),
        radius=px(20), fill=pal["badge_fill"], outline=pal["badge_outline"], width=1,
    )
    draw.text((cur_left + px(18), badge_top + px(10)),
              f"Heute  {today_min} – {today_max}", font=font_value, fill=pal["badge_text"])

    # Mondphase: Scheibe + Label rechts neben dem Badge
    moon_phase = today.get("moonPhase")
    if moon_phase is not None:
        disc_r  = px(14)
        disc_cx = cur_left + badge_w + px(18)     # 18 px Abstand nach Badge-Rechtsrand
        disc_cy = badge_top + px(21)              # vertikal mittig im Badge
        draw_moon_disc(img, disc_cx, disc_cy, disc_r, moon_phase, pal)
        phase_lbl = moon_phase_label(moon_phase)
        draw.text((disc_cx + disc_r + px(7), badge_top + px(11)),
                  phase_lbl, font=font_micro, fill=pal["moon_disc_label"])

    # Sonne + Mond: 4 Ereignisse gleichmäßig über die verfügbare Breite verteilen
    astro_y  = cur_top + px(214)
    ev_step  = (cur_right - cur_left) // 4
    astro_events = [
        ("Aufgang",   today.get("sunrise",  "--:--"), True,  False),
        ("Untergang", today.get("sunset",   "--:--"), False, False),
        ("Aufgang",   today.get("moonrise", "--:--"), True,  True),
        ("Untergang", today.get("moonset",  "--:--"), False, True),
    ]
    for i, (lbl, val, is_rise, is_moon) in enumerate(astro_events):
        draw_astro_event(draw, cur_left + i * ev_step, astro_y,
                         lbl, val, is_rise, is_moon,
                         font_micro, font_label, pal, scale=s)

    cur_bottom = cur_top + px(268)
    draw.line((cur_left, cur_bottom + px(6), cur_right, cur_bottom + px(6)),
              fill=pal["divider"], width=1)

    # Zone 2: Amtliche Warnungen (optional)
    warning_items = data.get("warnings") or []
    warning_gap = px(10)
    stat_top = cur_bottom + px(20)
    warning_card_w = cur_right - cur_left - px(28)
    # Höhenbudget: Stat-Panels, Pollenleiste (mind. eine Zeile), Stundenverlauf (mind. 200)
    # und Tagesvorschau (110) behalten ihren Platz – sonst lieber eine Karte und „+1 weitere“
    pollen_reserve = px(110) + px(10) if data.get("pollen") else 0
    warning_budget = ((panel_bottom - px(14)) - stat_top - warning_gap - px(110) - px(14)
                      - pollen_reserve - px(200) - px(10) - px(110))
    warning_limit = warning_card_limit(warning_items, warning_card_w, context.load_font, s, warning_budget)
    warning_h = warning_strip_height(warning_items, warning_card_w, context.load_font, s, warning_limit)
    if warning_h > 0:
        warning_top = stat_top
        draw_warning_strip(
            img,
            (cur_left, warning_top, cur_right, warning_top + warning_h),
            warning_items,
            font_warning_title,
            font_micro,
            pal,
            context.load_font,
            scale=s,
            limit=warning_limit,
        )
        stat_top = warning_top + warning_h + warning_gap

    # Zone 3: Stat-Panels
    stat_h   = px(110)
    stat_gap = px(14)
    stat_w   = int((cur_right - cur_left - stat_gap * 3) / 4)

    wind_text = data.get("current_wind_text", "--")
    wind_dir  = data.get("current_wind_dir_label", "")
    if wind_dir and wind_dir != "--":
        if "\n" in wind_text:
            # Zweizeilig: Richtung hinter Windgeschwindigkeit (Zeile 1), Böen auf Zeile 2
            parts = wind_text.split("\n", 1)
            wind_display = f"{parts[0]}  {wind_dir}\n{parts[1]}"
        else:
            wind_display = f"{wind_text}  {wind_dir}"
    else:
        wind_display = wind_text

    stat_defs = [
        ("Niederschlag", data.get("current_precipitation_mm_text", "--"), "umbrella"),
        ("Feuchte",      data.get("current_humidity_text",         "--"), "humidity"),
        ("Wind",         wind_display,                                    "wind"),
        ("Luftdruck",    data.get("current_pressure_text",         "--"), "gauge"),
    ]
    for idx, (title, value, icon_name) in enumerate(stat_defs):
        sx = cur_left + idx * (stat_w + stat_gap)
        draw_stat_panel(img, (sx, stat_top, sx + stat_w, stat_top + stat_h),
                        title, value, icon_name, font_label, font_value, pal,
                        load_font=context.load_font, scale=s)

    stat_bottom = stat_top + stat_h

    # Zone 4: Pollenleiste (optional – nur wenn Pollen-Region + Allergene konfiguriert)
    pollen_data       = data.get("pollen")
    font_pollen_title = context.load_font(px(24), True)
    font_pollen_label = context.load_font(px(19), True)
    font_pollen_small = context.load_font(px(18), False)

    next_zone_top = stat_bottom + px(14)
    if pollen_data:
        pollen_allergens = pollen_data.get("allergens", {})
        pollen_gap = px(10)
        # Höhenbudget: Stundenverlauf (mind. 200) und Tagesvorschau (110) behalten ihren Platz
        pollen_budget = max(px(110), (panel_bottom - px(14)) - next_zone_top - pollen_gap - px(200) - px(10) - px(110))
        pollen_plan = plan_pollen_layout(pollen_allergens, cur_right - cur_left, pollen_budget, s)
        pollen_h   = pollen_plan["height"]
        pollen_top = next_zone_top
        draw_pollen_strip(
            img,
            (cur_left, pollen_top, cur_right, pollen_top + pollen_h),
            pollen_data, font_pollen_title, font_pollen_label, font_pollen_small, pal,
            plan=pollen_plan, scale=s,
        )
        draw = new_draw(img, "RGBA")
        zone3_top = pollen_top + pollen_h + pollen_gap
    else:
        zone3_top = next_zone_top

    # Zone 5 + 6: Stundenverlauf & Prognose (responsiv)
    remaining      = (panel_bottom - px(14)) - zone3_top
    forecast_h     = max(px(110), min(px(140), int(remaining * 0.24)))
    hourly_alloc   = max(px(80), remaining - forecast_h - px(10))
    hourly_top     = zone3_top
    hourly_bottom  = hourly_top + hourly_alloc
    forecast_top   = hourly_bottom + px(10)
    forecast_bottom = panel_bottom - px(14)

    draw_hourly_strip(
        draw,
        (cur_left, hourly_top, cur_right, hourly_bottom),
        data.get("hourly_forecast") or [],
        font_chart_title, font_chart_time, font_chart_temp,
        pal, img=img, scale=s,
    )

    forecast_days = data.get("days", [])[:5]
    if forecast_days and forecast_bottom > forecast_top + px(40):
        draw_compact_forecast_strip(
            img,
            (cur_left, forecast_top, cur_right, forecast_bottom),
            forecast_days,
            font_forecast_day, font_forecast_temp, font_forecast_meta,
            pal, scale=s,
        )

    return img.convert("RGB")


def render_dwd_weather_tile(context: ModuleRenderServices, content: object,
                            width: int, height: int) -> Image.Image:
    """
    Kompakte Wetter-Kachel für den Dashboard-Modus: aktuelle Temperatur,
    Zustand, Min/Max, Icon; ab ~300 px Höhe die vier Stat-Panels, ab ~460 px
    zusätzlich die Mehrtages-Vorschau. Skaliert mit der Kachelbreite.
    """
    data = content if isinstance(content, dict) else {}
    theme = getattr(context, "display_theme", "dark")
    pal = get_dwd_palette(theme)
    flat = bool(pal.get("flat"))
    s = max(0.5, min(width / 1200.0, 1.4))

    def px(v: float) -> int:
        return max(1, int(v * s))

    if flat:
        img = Image.new("RGBA", (width, height), (*pal["bg_top"], 255))
    elif theme == "light":
        img = create_weather_background_light(width, height).convert("RGBA")
    else:
        img = create_weather_background(width, height).convert("RGBA")
    draw = new_draw(img, "RGBA")

    font_title     = context.load_font(px(26), True)
    font_big       = context.load_font(px(84), True)
    font_condition = context.load_font(px(30), True)
    font_value     = context.load_font(px(26), True)
    font_label     = context.load_font(px(20), True)
    font_fc_day    = context.load_font(px(20), True)
    font_fc_temp   = context.load_font(px(22), True)
    font_fc_meta   = context.load_font(px(19), False)

    left, right = px(40), width - px(40)
    station_label = data.get("station_name") or f"DWD-Station {data.get('station_id', '--')}"
    draw.text((left, px(14)), f"Wetter  ·  {station_label}", font=font_title, fill=pal["header_station"])

    # Aktuell: Temperatur, Zustand, Badge links – Icon rechts
    top = px(50)
    icon_size = px(120)
    draw_weather_icon(draw, right - icon_size, top, data.get("current_icon_code"),
                      icon_size, pal["icon_main"], temp_hint_c=data.get("current_temp_c"))
    draw.text((left, top - px(6)), format_temp(data.get("current_temp_c")), font=font_big, fill=pal["temp_big"])
    draw.text((left, top + px(96)), data.get("current_label") or "Keine aktuellen Daten",
              font=font_condition, fill=pal["condition"])
    today = data.get("today", {})
    badge_top = top + px(140)
    badge_text = f"Heute  {format_temp(today.get('min_temp_c'))} – {format_temp(today.get('max_temp_c'))}"
    badge_w = int(draw.textlength(badge_text, font=font_value)) + px(36)
    draw.rounded_rectangle((left, badge_top, left + badge_w, badge_top + px(40)),
                           radius=0 if flat else px(20), fill=pal["badge_fill"], outline=pal["badge_outline"], width=1)
    draw.text((left + px(18), badge_top + px(9)), badge_text, font=font_value, fill=pal["badge_text"])

    y = badge_top + px(58)

    # Stat-Panels, wenn Platz (110 wie im Vollbild: zweizeiliger Wind braucht die Höhe)
    stat_h = px(110)
    if height - y >= stat_h + px(16):
        wind_text = data.get("current_wind_text", "--")
        wind_dir = data.get("current_wind_dir_label", "")
        if wind_dir and wind_dir != "--":
            parts = wind_text.split("\n", 1)
            wind_text = f"{parts[0]}  {wind_dir}" + (f"\n{parts[1]}" if len(parts) > 1 else "")
        stat_defs = [
            ("Niederschlag", data.get("current_precipitation_mm_text", "--"), "umbrella"),
            ("Feuchte",      data.get("current_humidity_text", "--"),         "humidity"),
            ("Wind",         wind_text,                                       "wind"),
            ("Luftdruck",    data.get("current_pressure_text", "--"),         "gauge"),
        ]
        gap = px(12)
        stat_w = int((right - left - gap * 3) / 4)
        for idx, (title, value, icon_name) in enumerate(stat_defs):
            sx = left + idx * (stat_w + gap)
            draw_stat_panel(img, (sx, y, sx + stat_w, y + stat_h), title, value, icon_name,
                            font_label, font_value, pal, load_font=context.load_font, scale=s)
        y += stat_h + px(14)

    # Vorschau, wenn noch Platz (140: Tag, Temperatur und drei Meta-Zeilen ohne Überlappung)
    forecast_days = data.get("days", [])[:5]
    fc_h = px(140)
    if forecast_days and height - y >= fc_h + px(8):
        draw_compact_forecast_strip(img, (left, y, right, y + fc_h), forecast_days,
                                    font_fc_day, font_fc_temp, font_fc_meta, pal, scale=s)
        y += fc_h + px(12)

    # Pollen, wenn danach noch Platz ist (kompakt als Chips, Raster nur bei viel Höhe)
    pollen_data = data.get("pollen")
    if pollen_data:
        budget = height - y - px(8)
        plan = plan_pollen_layout(pollen_data.get("allergens", {}), right - left, budget, s)
        if plan["height"] <= budget:
            draw_pollen_strip(img, (left, y, right, y + plan["height"]), pollen_data,
                              context.load_font(px(24), True), context.load_font(px(19), True),
                              context.load_font(px(18), False), pal, plan=plan, scale=s)

    return img.convert("RGB")


def should_refresh_dwd_weather_module() -> bool:
    from .dwd import should_refresh_dwd_weather
    from .dwd_pollen import should_refresh_dwd_pollen
    from .dwd_uv import should_refresh_dwd_uv

    return (
        should_refresh_dwd_weather()
        or should_refresh_dwd_uv()
        or should_refresh_dwd_pollen()
    )

