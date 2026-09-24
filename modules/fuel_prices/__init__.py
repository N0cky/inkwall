"""
Tankpreise-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 108). Zeigt die günstigsten Tankstellen im
Umkreis oder eine feste Auswahl mit Preisen je Kraftstoff, dazu eine selbst
gesammelte Historie: Tagesverlauf, 7 und 30 Tage, Uhrzeit-Profil und
Kennzahlen. Daten von Tankerkönig (MTS-K), Lizenz CC BY 4.0.
"""

from __future__ import annotations

from typing import Any

from PIL import Image

from app.logger import get_logger
from app.module_base import InkwallModule
from app.module_services import ModuleRenderServices

log = get_logger(__name__)


def _fuel_options() -> list[list[str]]:
    from .data_source import FUELS
    return [list(f) for f in FUELS]


def _section_options() -> list[list[str]]:
    from .data_source import SECTIONS
    return [list(s) for s in SECTIONS]


SETTINGS_FIELDS: list[dict] = [
    {
        "name":        "FUEL_API_KEY",
        "label":       "Tankerkönig API-Key",
        "type":        "password",
        "wide":        True,
        "default":     "",
        "placeholder": "00000000-0000-0000-0000-000000000000",
        "help":        "Kostenlos nach Registrierung bei Tankerkönig. Die Daten kommen von der Markttransparenzstelle für Kraftstoffe.",
        "link_href":   "https://creativecommons.tankerkoenig.de/",
        "link_label":  "API-Key holen",
    },
    {
        "name":        "FUEL_LOCATION",
        "label":       "Koordinaten (Breite, Länge)",
        "type":        "text",
        "wide":        False,
        "default":     "",
        "placeholder": "50.5556, 8.5045",
        "help":        "Mittelpunkt der Umkreissuche. In Google Maps: Rechtsklick auf den Ort, die erste Zeile im Menü sind die Koordinaten.",
    },
    {
        "name":        "FUEL_RADIUS_KM",
        "label":       "Umkreis (km)",
        "type":        "number",
        "wide":        False,
        "default":     "5",
        "placeholder": "5",
        "min":         1,
        "max":         25,
    },
    {
        "name":        "FUEL_STATIONS",
        "label":       "Feste Stationen",
        "type":        "list",
        "wide":        True,
        "item_fields": [
            {"name": "label", "label": "Name auf dem Display", "placeholder": "Aral Hermannsteiner"},
            {"name": "id", "label": "Tankerkönig-ID", "placeholder": "00000000-0000-0000-0000-000000000000", "wide": True},
        ],
        "help": (
            "Optional: Sind hier Stationen eingetragen, zeigt das Panel nur diese statt des Umkreises. "
            "Die IDs stehen unter „Verbindung prüfen“ neben jeder Station im Umkreis."
        ),
    },
    {
        "name":        "FUEL_MAX_STATIONS",
        "label":       "Stationen auf dem Display",
        "type":        "number",
        "wide":        False,
        "default":     "6",
        "placeholder": "6",
        "min":         1,
        "max":         12,
    },
    {
        "name":    "FUEL_TYPES",
        "label":   "Kraftstoffe",
        "type":    "checkbox_group",
        "wide":    False,
        "default": "e5,diesel",
        "options": [],   # aus data_source.FUELS
        "help":    "Welche Preise auf dem Display stehen.",
    },
    {
        "name":    "FUEL_PRIMARY",
        "label":   "Hauptkraftstoff",
        "type":    "select",
        "wide":    False,
        "default": "e5",
        "options": [],   # aus data_source.FUELS
        "help":    "Bestimmt Sortierung, großen Preis, Kachel, Historie und Preisalarm.",
    },
    {
        "name":        "FUEL_ALERT_PRICE",
        "label":       "Preisalarm ab (€/l)",
        "type":        "text",
        "wide":        False,
        "default":     "",
        "placeholder": "1,65",
        "help":        "Liegt der günstigste Preis des Hauptkraftstoffs darunter, wird der Inhalt dringend: Banner, früher in der Rotation, oben im Dashboard. Leer = aus.",
    },
    {
        "name":    "FUEL_SECTIONS",
        "label":   "Abschnitte auf dem Display",
        "type":    "checkbox_group",
        "wide":    True,
        "default": "day,week,month,hours,stats",
        "options": [],   # aus data_source.SECTIONS
        "help":    "Unter der Stationsliste. Was nicht mehr auf die Seite passt, fällt von unten weg. Wochentag- und Uhrzeit-Statistik brauchen zwei Wochen Daten.",
    },
    {
        "name":        "FUEL_CACHE_SECONDS",
        "label":       "Preise abfragen alle (s)",
        "type":        "number",
        "wide":        False,
        "default":     "300",
        "placeholder": "300",
        "min":         300,
        "max":         3600,
        "help":        "Tankerkönig erlaubt höchstens eine Abfrage alle fünf Minuten. Jede Abfrage ergänzt die Historie.",
    },
]

SETTINGS_GROUPS: list[dict] = [
    {"title": "Standort & Stationen", "desc": "Wo gesucht wird und was auf dem Display steht.",
     "fields": ["FUEL_LOCATION", "FUEL_RADIUS_KM", "FUEL_STATIONS", "FUEL_MAX_STATIONS"]},
    {"title": "Kraftstoffe", "desc": "Welche Preise zählen.", "fields": ["FUEL_TYPES", "FUEL_PRIMARY", "FUEL_ALERT_PRICE"]},
    {"title": "Darstellung", "desc": "Diagramme und Kennzahlen aus der gesammelten Historie.", "fields": ["FUEL_SECTIONS", "FUEL_CACHE_SECONDS"]},
    {"title": "Quelle", "desc": "Zugang zu Tankerkönig.", "fields": ["FUEL_API_KEY"]},
]


def _with_options(fields: list[dict]) -> list[dict]:
    out = []
    for f in fields:
        if f["name"] in ("FUEL_TYPES", "FUEL_PRIMARY"):
            f = {**f, "options": _fuel_options()}
        elif f["name"] == "FUEL_SECTIONS":
            f = {**f, "options": _section_options()}
        out.append(f)
    return out


class FuelPricesModule(InkwallModule):
    MODULE_ID          = "fuel_prices"
    MODULE_NAME        = "Tankpreise"
    MODULE_DESCRIPTION = (
        "Günstigste Tankstellen im Umkreis oder deine Stammstationen mit Preisen je Kraftstoff, "
        "dazu Tagesverlauf, Wochen- und Monatsstatistik, beste Tankzeit und Preisalarm."
    )
    MODULE_PRIORITY  = 108
    SETTINGS_FIELDS  = _with_options(SETTINGS_FIELDS)
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    # ── Grundlagen ───────────────────────────────────────────────────────────

    @staticmethod
    def _configured(env: dict[str, str]) -> bool:
        from .data_source import parse_location, parse_stations
        return bool(env.get("FUEL_API_KEY", "").strip()) and (
            parse_location(env.get("FUEL_LOCATION", "")) is not None or bool(parse_stations(env.get("FUEL_STATIONS", ""))))

    def is_enabled(self, env: dict[str, str]) -> bool:
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle and self._configured(env)

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import fetch_fuel_content
        try:
            return fetch_fuel_content(False)
        except Exception as exc:
            log.error(f"FuelPricesModule.fetch_content: {exc}", exc_info=True)
            return None

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_fuel_module
        return render_fuel_module(ModuleRenderServices.from_runtime(), content)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_fuel_module
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        return render_fuel_module(services, content, compact=True)

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .data_source import should_refresh
        return should_refresh()

    def get_state_key(self, content: Any) -> str:
        # Preise der gezeigten Stationen + Alarm + Stunde: Diagramme wachsen mindestens stündlich weiter
        if isinstance(content, dict):
            parts = [f"{s['id'][:8]}:{s['prices'].get(content.get('primary'))}:{int(s.get('is_open', True))}" for s in content.get("stations", [])]
            hour = content.get("now", "")[:13]
            return f"{content.get('primary')}|{'alert' if content.get('alert') else ''}|{'stale' if content.get('stale_since') else ''}|{hour}|" + ";".join(parts)
        return "fuel"

    def get_background_poll_seconds(self, env: dict[str, str]) -> int | None:
        # Alle fünf Minuten abfragen, auch wenn gerade etwas anderes gezeigt wird – sonst hat die Historie Lücken
        from .data_source import cache_seconds
        return cache_seconds()

    def is_urgent(self, env: dict[str, str]) -> bool:
        """Preisalarm: der günstigste Preis des Hauptkraftstoffs liegt unter der Schwelle."""
        if not self.is_enabled(env):
            return False
        content = self.fetch_content(env)
        return bool(content and content.get("alert"))

    # ── Oberfläche ───────────────────────────────────────────────────────────

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import FUEL_SHORT, parse_fuels, parse_location, parse_stations
        fixed = parse_stations(env.get("FUEL_STATIONS", ""))
        loc = parse_location(env.get("FUEL_LOCATION", ""))
        where = f"{len(fixed)} feste Stationen" if fixed else (f"Umkreis {env.get('FUEL_RADIUS_KM', '5')} km" if loc else "Nicht konfiguriert")
        return {"Tankpreise": f"{where} · " + ", ".join(FUEL_SHORT[f] for f in parse_fuels(env.get("FUEL_TYPES", "")))}

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_location, parse_stations
        if not env.get("FUEL_API_KEY", "").strip():
            return {"state": "missing", "reason": "API-Key fehlt"}
        if parse_location(env.get("FUEL_LOCATION", "")) is None and not parse_stations(env.get("FUEL_STATIONS", "")):
            return {"state": "missing", "reason": "Koordinaten fehlen"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import FUEL_SHORT, fetch_fuel_content
        from .renderer import format_cents, format_price
        if not self._configured(env):
            return ""
        try:
            content = fetch_fuel_content(False)
        except Exception:
            content = None
        if not content:
            return "Preise noch nicht geladen"
        parts = []
        for fuel in content["fuels"]:
            best = content["cheapest"].get(fuel)
            if best:
                parts.append(f"{FUEL_SHORT[fuel]} ab {format_price(best['price'])} ({best['station']})")
        trend = (content.get("stats") or {}).get("trend")
        if trend and abs(trend["delta_ct"]) >= 0.05:
            parts.append(f"{'+' if trend['delta_ct'] > 0 else '−'}{format_cents(trend['delta_ct'])} ct seit {trend['reference_at']}")
        if content.get("alert"):
            parts.append("Preisalarm")
        return " · ".join(parts) or "Keine Preise"

    def probe(self, env: dict[str, str]) -> dict:
        """Stationen im Umkreis mit ID und Preis auflisten, feste Stationen prüfen, Stand der Historie zeigen."""
        from . import history
        from .data_source import (FUEL_SHORT, fetch_fuel_content, parse_fuels, parse_location, parse_stations, radius_km,
                                  remember_radius_result, search_radius, station_detail)
        from .renderer import format_price
        if not env.get("FUEL_API_KEY", "").strip():
            return {"ok": False, "message": "API-Key fehlt – kostenlos bei Tankerkönig registrieren"}
        fuels = parse_fuels(env.get("FUEL_TYPES", ""))
        primary = env.get("FUEL_PRIMARY", "").strip() if env.get("FUEL_PRIMARY", "").strip() in fuels else fuels[0]
        details: list[str] = []
        loc = parse_location(env.get("FUEL_LOCATION", ""))
        fixed = parse_stations(env.get("FUEL_STATIONS", ""))
        if loc is None and not fixed:
            return {"ok": False, "message": "Koordinaten oder feste Stationen fehlen"}
        ok = True
        fresh = False                   # True: die Umkreissuche ist schon der aktuelle Stand
        if fixed:
            details.append("Feste Stationen:")
            for label, sid in fixed:
                detail = station_detail(sid, force=True)
                if detail:
                    details.append(f"{label or detail['name']}: {detail['full_name'] or detail['name']}, {detail['street']} {detail['place']} ({sid})".replace("  ", " "))
                else:
                    details.append(f"{label or sid}: nicht gefunden – ID prüfen")
                    ok = False
        if loc is not None:
            try:
                found = search_radius(loc[0], loc[1], radius_km())
            except Exception as exc:
                return {"ok": False, "message": f"Tankerkönig nicht erreichbar: {str(exc)[:100]}", "details": details}
            details.append(f"Umkreis {radius_km()} km um {loc[0]}, {loc[1]}: {len(found)} Stationen" + (" (nur zur Auswahl, das Display zeigt die festen Stationen)" if fixed else "") + ":")
            found.sort(key=lambda s: (s["prices"].get(primary) is None, s["prices"].get(primary) or 99, s["dist_km"] or 0))
            for s in found[:15]:
                prices = " · ".join(f"{FUEL_SHORT[f]} {format_price(s['prices'][f])}" for f in fuels if s["prices"].get(f) is not None)
                where = f"{s['street']}, {s['place']}".strip(", ")
                details.append(f"{s['name']} ({s['dist_km']} km, {where}): {prices or 'keine Preise'}{'' if s['is_open'] else ' · geschlossen'} · ID {s['id']}")
            # Ohne feste Stationen ist das genau die Abfrage fürs Display – nicht gleich nochmal schicken
            fresh = remember_radius_result(found)
        content = fetch_fuel_content(not fresh)
        if not content:
            return {"ok": False, "message": "Keine Preise ladbar – API-Key und Koordinaten prüfen", "details": details}
        days = history.days_collected(primary)
        ready_on = history.stats_ready_on(primary)
        details.append(f"Historie für {FUEL_SHORT[primary]}: {days} Tage gesammelt"
                       + (f", Wochentag- und Uhrzeit-Statistik ab {ready_on.day}.{ready_on.month}." if ready_on and not history.stats_ready(primary) else ""))
        best = content["cheapest"].get(primary)
        message = (f"{len(content['stations'])} Stationen · {FUEL_SHORT[primary]} ab {format_price(best['price'])} bei {best['station']}"
                   if best else f"{len(content['stations'])} Stationen, aber kein Preis für {FUEL_SHORT[primary]}")
        return {"ok": ok, "message": message, "details": details}

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        from .data_source import parse_stations
        return {"ok": True, "enabled": self.is_enabled(env), "fixed_stations": len(parse_stations(env.get("FUEL_STATIONS", "")))}

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        from .data_source import (FUEL_LABELS, MAX_FIXED_STATIONS, MIN_CACHE_SECONDS, STATION_ID, parse_fuels, parse_location,
                                  parse_price)
        errors: list[str] = []
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        active = self.MODULE_ID in idle
        key = env.get("FUEL_API_KEY", "").strip()
        loc_raw = env.get("FUEL_LOCATION", "").strip()
        stations_raw = env.get("FUEL_STATIONS", "").strip()
        if active and not key:
            errors.append("Tankerkönig API-Key: Bitte den API-Key eintragen, wenn Tankpreise aktiv sind.")
        if loc_raw and parse_location(loc_raw) is None:
            errors.append("Koordinaten (Breite, Länge): Bitte als „50.5556, 8.5045“ angeben.")
        if active and not loc_raw and not stations_raw:
            errors.append("Koordinaten (Breite, Länge): Ohne Koordinaten oder feste Stationen kann nichts gesucht werden.")
        if stations_raw:
            chunks = [c.strip() for c in stations_raw.replace("\n", ";").split(";") if c.strip()]
            bad = [c for c in chunks if not STATION_ID.match((c.split("|", 1)[1] if "|" in c else c).strip())]
            if bad:
                errors.append("Feste Stationen: Jede Zeile braucht eine Tankerkönig-ID (Form 00000000-0000-0000-0000-000000000000, siehe „Verbindung prüfen“).")
            if len(chunks) > MAX_FIXED_STATIONS:
                errors.append(f"Feste Stationen: Höchstens {MAX_FIXED_STATIONS} Stationen.")
        types_raw = env.get("FUEL_TYPES", "")
        if "FUEL_TYPES" in updates and not any(p.strip() in FUEL_LABELS for p in types_raw.split(",")):
            errors.append("Kraftstoffe: Bitte mindestens einen Kraftstoff auswählen.")
        primary = env.get("FUEL_PRIMARY", "").strip()
        if primary and primary not in parse_fuels(types_raw):
            errors.append("Hauptkraftstoff: Muss einer der ausgewählten Kraftstoffe sein.")
        alert = env.get("FUEL_ALERT_PRICE", "").strip()
        if alert and parse_price(alert) is None:
            errors.append("Preisalarm ab (€/l): Bitte einen Preis wie „1,65“ angeben (zwischen 0,50 und 5,00).")
        for key_name, label, lo, hi in (("FUEL_RADIUS_KM", "Umkreis (km)", 1, 25), ("FUEL_MAX_STATIONS", "Stationen auf dem Display", 1, 12),
                                        ("FUEL_CACHE_SECONDS", "Preise abfragen alle (s)", MIN_CACHE_SECONDS, 3600)):
            value = env.get(key_name, "").strip()
            if value and (not value.isdigit() or not lo <= int(value) <= hi):
                errors.append(f"{label}: Bitte eine ganze Zahl zwischen {lo} und {hi}.")
        return errors


module = FuelPricesModule()
