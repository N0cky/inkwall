"""
DWD-Wetter-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 100): wird angezeigt wenn kein Prioritätsmodul
(z. B. Plex) aktiven Inhalt meldet.

Zeigt aktuelles Wetter, Stundenverlauf, Mehrtagesprognose, UV-Index und
Pollenflug basierend auf Daten des Deutschen Wetterdienstes (DWD).
"""

from __future__ import annotations

import time
from typing import Any

from PIL import Image

from app.module_base import InkwallModule
from app.module_services import ModuleRenderServices
from app.logger import get_logger

log = get_logger(__name__)

# Ab dieser DWD-Stufe (3 = Unwetter, 4 = extremes Unwetter) springt das Wetter nach vorn
URGENT_LEVEL = 3
# Auch eine Unwetterwarnung, die erst in dieser Zeit beginnt, zählt schon
URGENT_LOOKAHEAD_SECONDS = 3 * 3600


def severe_warnings(now_ms: float | None = None) -> list[dict]:
    """Unwetterwarnungen (ab URGENT_LEVEL), die gelten oder bald beginnen – nur aus dem Cache."""
    from .dwd import cached_warnings
    now_ms = time.time() * 1000 if now_ms is None else now_ms
    found: list[dict] = []
    for warning in cached_warnings():
        if (warning.get("level") or 0) < URGENT_LEVEL:
            continue
        start, end = warning.get("_start_ms"), warning.get("_end_ms")
        if isinstance(end, (int, float)) and end <= now_ms:
            continue
        if isinstance(start, (int, float)) and start > now_ms + URGENT_LOOKAHEAD_SECONDS * 1000:
            continue
        found.append(warning)
    return found


# ---------------------------------------------------------------------------
# Settings-Felder dieses Moduls
# ---------------------------------------------------------------------------

SETTINGS_FIELDS: list[dict] = [
    {
        "name":        "DWD_WEATHER_STATION_ID",
        "label":       "DWD-Station-ID",
        "type":        "text",
        "wide":        False,
        "default":     "10532",
        "placeholder": "10532",
        "help":        "Stations-ID für das DWD-Wettermodul, z. B. 10532 für Gießen.",
        "link_href":   (
            "https://www.dwd.de/DE/leistungen/klimadatendeutschland/statliste/"
            "statlex_html.html?view=nasPublication&nn=16102"
        ),
        "link_label":  "Stationsliste des DWD öffnen",
        "link_note":   (
            "Bitte auf das Enddatum achten: In der Liste stehen auch ältere, "
            "inzwischen geschlossene Stationen."
        ),
    },
    {
        "name":        "DWD_WEATHER_CACHE_SECONDS",
        "label":       "Wetter-Cache (s)",
        "type":        "number",
        "wide":        False,
        "default":     "900",
        "placeholder": "900",
        "min":         60,
        "max":         86400,
        "help":        "Wie lange DWD-Wetterdaten gecacht werden bevor ein neuer Abruf erfolgt.",
    },
    {
        "name":    "DWD_HOURLY_START",
        "label":   "Verlauf-Startpunkt",
        "type":    "select",
        "wide":    True,
        "default": "day_start",
        "options": [
            ("day_start",    "Ab Tagesbeginn (00:00) – aktueller Tag plus etwas Folgetag"),
            ("current_hour", "Ab aktueller voller Stunde – gleicher Umfang, aber ab jetzt"),
        ],
        "help": (
            "'Ab Tagesbeginn' startet um 00:00 und zeigt den aktuellen Tag plus etwas vom nächsten. "
            "'Ab aktueller Stunde' startet bei der letzten vollen Stunde."
        ),
    },
    {
        "name":    "DWD_HOURLY_INTERVAL_HOURS",
        "label":   "Verlauf-Intervall",
        "type":    "select",
        "wide":    False,
        "default": "2",
        "options": [
            ("1", "1 Stunde"),
            ("2", "2 Stunden"),
            ("3", "3 Stunden"),
            ("4", "4 Stunden"),
        ],
        "help": "Abstand zwischen Datenpunkten im Stundenverlauf.",
    },
    {
        "name":         "DWD_UV_CITY",
        "label":        "UV-Index-Stadt",
        "type":         "text",
        "wide":         False,
        "default":      "",
        "placeholder":  "z. B. Frankfurt/Main",
        "datalist_url": "/api/module-field-options/dwd_weather/DWD_UV_CITY",
        "help":         (
            "Stadtname für den DWD UV-Index. Tippen und Vorschlag auswählen. "
            "Leer = UV-Anzeige deaktiviert."
        ),
        "link_href":  "https://opendata.dwd.de/climate_environment/health/alerts/uvi.json",
        "link_label": "DWD UV-Index-API ansehen",
    },
    {
        "name":    "DWD_POLLEN_REGION",
        "label":   "Pollen-Region",
        "type":    "select",
        "wide":    True,
        "default": "",
        "options": [
            ("",        "Keine Pollenanzeige"),
            ("10:-1",    "Schleswig-Holstein und Hamburg – ganzes Land (höchster Wert)"),
            ("10:11",    "Schleswig-Holstein und Hamburg – Inseln und Marschen"),
            ("10:12",    "Schleswig-Holstein und Hamburg – Geest, Schleswig-Holstein und Hamburg"),
            ("20:-1",    "Mecklenburg-Vorpommern"),
            ("30:-1",    "Niedersachsen und Bremen – ganzes Land (höchster Wert)"),
            ("30:31",    "Niedersachsen und Bremen – Westl. Niedersachsen/Bremen"),
            ("30:32",    "Niedersachsen und Bremen – Östl. Niedersachsen"),
            ("40:-1",    "Nordrhein-Westfalen – ganzes Land (höchster Wert)"),
            ("40:41",    "Nordrhein-Westfalen – Rhein.-Westfäl. Tiefland"),
            ("40:42",    "Nordrhein-Westfalen – Ostwestfalen"),
            ("40:43",    "Nordrhein-Westfalen – Mittelgebirge NRW"),
            ("50:-1",    "Brandenburg und Berlin"),
            ("60:-1",    "Sachsen-Anhalt – ganzes Land (höchster Wert)"),
            ("60:61",    "Sachsen-Anhalt – Tiefland Sachsen-Anhalt"),
            ("60:62",    "Sachsen-Anhalt – Harz"),
            ("70:-1",    "Thüringen – ganzes Land (höchster Wert)"),
            ("70:71",    "Thüringen – Tiefland Thüringen"),
            ("70:72",    "Thüringen – Mittelgebirge Thüringen"),
            ("80:-1",    "Sachsen – ganzes Land (höchster Wert)"),
            ("80:81",    "Sachsen – Tiefland Sachsen"),
            ("80:82",    "Sachsen – Mittelgebirge Sachsen"),
            ("90:-1",    "Hessen – ganzes Land (höchster Wert)"),
            ("90:91",    "Hessen – Nordhessen und hess. Mittelgebirge"),
            ("90:92",    "Hessen – Rhein-Main"),
            ("100:-1",   "Rheinland-Pfalz und Saarland – ganzes Land (höchster Wert)"),
            ("100:101",  "Rheinland-Pfalz und Saarland – Rhein, Pfalz, Nahe und Mosel"),
            ("100:102",  "Rheinland-Pfalz und Saarland – Mittelgebirgsbereich Rheinland-Pfalz"),
            ("100:103",  "Rheinland-Pfalz und Saarland – Saarland"),
            ("110:-1",   "Baden-Württemberg – ganzes Land (höchster Wert)"),
            ("110:111",  "Baden-Württemberg – Oberrhein und unteres Neckartal"),
            ("110:112",  "Baden-Württemberg – Hohenlohe/mittlerer Neckar/Oberschwaben"),
            ("110:113",  "Baden-Württemberg – Mittelgebirge Baden-Württemberg"),
            ("120:-1",   "Bayern – ganzes Land (höchster Wert)"),
            ("120:121",  "Bayern – Allgäu/Oberbayern/Bay. Wald"),
            ("120:122",  "Bayern – Donauniederungen"),
            ("120:123",  "Bayern – Bayern nördl. der Donau, o. Bayr. Wald, o. Mainfranken"),
            ("120:124",  "Bayern – Mainfranken"),
        ],
        "help": (
            "Region für die DWD-Pollenflug-Vorhersage. Leer = Pollenanzeige deaktiviert. "
            "„Ganzes Land“ zeigt je Pollenart den höchsten Wert aller Teilregionen. "
            "Daten werden vom DWD einmal täglich aktualisiert."
        ),
        "link_href":  "https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json",
        "link_label": "DWD-Pollen-API ansehen",
    },
    {
        "name":    "DWD_POLLEN_ALLERGENS",
        "label":   "Angezeigte Pollen",
        "type":    "checkbox_group",
        "wide":    True,
        "default": "",
        "options": [
            ("Birke",    "Birke"),
            ("Esche",    "Esche"),
            ("Hasel",    "Hasel"),
            ("Erle",     "Erle"),
            ("Graeser",  "Gräser"),
            ("Roggen",   "Roggen"),
            ("Beifuss",  "Beifuß"),
            ("Ambrosia", "Ambrosia"),
        ],
        "help": (
            "Welche Pollen im Wetter-Modul angezeigt werden. "
            "Nichts ausgewählt = kein Pollenstreifen."
        ),
    },
]

SETTINGS_GROUPS: list[dict] = [
    {
        "title":  "Wetterstation",
        "desc":   "DWD-Station und Cache-Einstellungen.",
        "fields": ["DWD_WEATHER_STATION_ID", "DWD_WEATHER_CACHE_SECONDS"],
    },
    {
        "title":  "Stundenverlauf",
        "desc":   "Darstellung des Temperaturverlaufs.",
        "fields": ["DWD_HOURLY_START", "DWD_HOURLY_INTERVAL_HOURS"],
    },
    {
        "title":  "UV & Pollen",
        "desc":   "Gesundheitsdaten: UV-Belastung und Pollenflug.",
        "fields": ["DWD_UV_CITY", "DWD_POLLEN_REGION", "DWD_POLLEN_ALLERGENS"],
    },
]


# ---------------------------------------------------------------------------
# Modul-Implementierung
# ---------------------------------------------------------------------------

class DWDWeatherModule(InkwallModule):
    MODULE_ID          = "dwd_weather"
    MODULE_NAME        = "DWD Wetter"
    MODULE_DESCRIPTION = (
        "Zeigt aktuelles Wetter, Stundenverlauf, Mehrtagesprognose sowie optional "
        "UV-Index und Pollenflug basierend auf Daten des Deutschen Wetterdienstes."
    )
    MODULE_PRIORITY  = 100
    SETTINGS_FIELDS  = SETTINGS_FIELDS
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .renderer import fetch_dwd_weather_content, has_dwd_weather_content
        content = fetch_dwd_weather_content()
        return content if has_dwd_weather_content(content) else None

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_dwd_weather_module
        return render_dwd_weather_module(ModuleRenderServices.from_runtime(), content)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_dwd_weather_tile
        return render_dwd_weather_tile(ModuleRenderServices.from_runtime(), content, width, height)

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .renderer import should_refresh_dwd_weather_module
        return should_refresh_dwd_weather_module()

    def is_urgent(self, env: dict[str, str]) -> bool:
        """Unwetter (DWD-Stufe 3 oder 4) jetzt oder in den nächsten 3 Stunden."""
        return bool(severe_warnings())

    def get_alerts(self, env: dict[str, str]) -> list[dict]:
        alerts = []
        for w in severe_warnings():
            level = w.get("level") or 0
            alerts.append({
                # Über alle Aktualisierungen gleich: Ereignis + Beginn (warnId wechselt nicht, fehlt aber manchmal)
                "id": f"dwd:{w.get('warn_id') or w.get('event') or w.get('headline')}:{w.get('_start_ms')}",
                "title": w.get("headline") or w.get("event") or "Unwetterwarnung",
                "text": w.get("description") or "",
                "source": "Deutscher Wetterdienst",
                "severity": "extreme" if level >= 4 else "severe",
                "until": w.get("end") or "",
            })
        return alerts

    def get_state_key(self, content: Any) -> str:
        # Eindeutig über Station-ID; should_refresh() löst den Neu-Render aus
        if isinstance(content, dict):
            return content.get("station_id", "dwd")
        return "dwd_weather"

    def get_field_options(self, field_name: str, env: dict[str, str]) -> list | None:
        if field_name != "DWD_UV_CITY":
            return None
        from .dwd_uv import fetch_uv_city_names
        return fetch_uv_city_names()

    def handle_api_action(self, action: str, env: dict[str, str]) -> tuple[object, int] | None:
        if action != "uv-cities":
            return None
        return {"options": self.get_field_options("DWD_UV_CITY", env) or []}, 200

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from app.config import get_int_setting, get_setting
        from .dwd import resolve_dwd_station_name

        station_id = get_setting("DWD_WEATHER_STATION_ID", "10532").strip() or "10532"
        return {
            "DWD-Station":  resolve_dwd_station_name(station_id),
            "DWD-Cache":    f"{get_int_setting('DWD_WEATHER_CACHE_SECONDS', 900, 60, 86400)}s",
            "Stundenraster": f"{get_int_setting('DWD_HOURLY_INTERVAL_HOURS', 2, 1, 4)}h",
        }

    def summarize(self, env: dict[str, str]) -> str:
        from .dwd import resolve_dwd_station_name
        parts = [resolve_dwd_station_name(env.get("DWD_WEATHER_STATION_ID", "10532").strip() or "10532")]
        if env.get("DWD_UV_CITY", "").strip():
            parts.append(f"UV {env['DWD_UV_CITY'].strip()}")
        if env.get("DWD_POLLEN_REGION", "").strip() and env.get("DWD_POLLEN_ALLERGENS", "").strip():
            parts.append("Pollen")
        return " · ".join(parts)

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        station_id = env.get("DWD_WEATHER_STATION_ID", "").strip() or "10532"
        return {
            "ok": True,
            "enabled": self.is_enabled(env),
            "station_id": station_id,
            "uv_enabled": bool(env.get("DWD_UV_CITY", "").strip()),
            "pollen_enabled": bool(env.get("DWD_POLLEN_REGION", "").strip()),
        }

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        errors: list[str] = []
        station_id = env.get("DWD_WEATHER_STATION_ID", "").strip()
        hourly_start = env.get("DWD_HOURLY_START", "day_start").strip()
        if station_id and not station_id.isdigit():
            errors.append("DWD-Station-ID: Bitte nur numerische Stations-IDs verwenden.")
        if hourly_start not in {"day_start", "current_hour"}:
            errors.append("Verlauf-Startpunkt: Ungültige Auswahl.")
        return errors


module = DWDWeatherModule()
