"""
Abfahrten-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 107). Zeigt die nächsten Abfahrten von Bahn und
ÖPNV an bis zu drei Haltestellen: Uhrzeit mit Verspätung, Linie, Ziel, Gleis
und „in X min“. Daten kommen von einer transport.rest-Schnittstelle
(Standard: v6.db.transport.rest), Haltestellen als Nummer oder Name.
"""

from __future__ import annotations

from typing import Any

from PIL import Image

from app.logger import get_logger
from app.module_base import InkwallModule
from app.module_services import ModuleRenderServices

log = get_logger(__name__)


SETTINGS_FIELDS: list[dict] = [
    {
        "name":        "DEPARTURES_STOPS",
        "label":       "Haltestellen",
        "type":        "list",
        "wide":        True,
        "item_fields": [
            {"name": "label", "label": "Name auf dem Display", "placeholder": "Bahnhof"},
            {"name": "query", "label": "Haltestelle (Name oder Nummer)", "placeholder": "Wetzlar", "wide": True},
        ],
        "help": (
            "Bis zu drei Haltestellen. Ein Name reicht, das Modul sucht die Haltestelle einmal und merkt sie sich; "
            "„Verbindung prüfen“ zeigt, was gefunden wurde. Eine Nummer (IBNR, z. B. 8006429 für Wetzlar) ist eindeutig."
        ),
    },
    {
        "name":    "DEPARTURES_PRODUCTS",
        "label":   "Verkehrsmittel",
        "type":    "checkbox_group",
        "wide":    True,
        "default": "",
        "options": [],   # aus data_source.PRODUCTS
        "help":    "Nichts ausgewählt = alle.",
    },
    {
        "name":        "DEPARTURES_DURATION_MINUTES",
        "label":       "Zeitraum (min)",
        "type":        "number",
        "wide":        False,
        "default":     "60",
        "placeholder": "60",
        "min":         10,
        "max":         240,
        "help":        "Wie weit in die Zukunft geschaut wird.",
    },
    {
        "name":        "DEPARTURES_MAX",
        "label":       "Abfahrten je Haltestelle",
        "type":        "number",
        "wide":        False,
        "default":     "8",
        "placeholder": "8",
        "min":         1,
        "max":         20,
    },
    {
        "name":        "DEPARTURES_WALK_MINUTES",
        "label":       "Fußweg (min)",
        "type":        "number",
        "wide":        False,
        "default":     "0",
        "placeholder": "0",
        "min":         0,
        "max":         120,
        "help":        "Abfahrten, die du zu Fuß nicht mehr erreichst, werden weggelassen. 0 = alle zeigen.",
    },
    {
        "name":        "DEPARTURES_CACHE_SECONDS",
        "label":       "Abfahrten-Refresh (s)",
        "type":        "number",
        "wide":        False,
        "default":     "120",
        "placeholder": "120",
        "min":         30,
        "max":         3600,
        "help":        "Wie oft die Abfahrten neu geladen werden.",
    },
    {
        "name":        "DEPARTURES_API_URL",
        "secret":      True,          # steckt ein Zugangsweg drin: nicht im Export „ohne Geheimnisse“
        "label":       "Fahrplan-Schnittstelle",
        "type":        "text",
        "wide":        True,
        "default":     "",
        "placeholder": "https://v6.db.transport.rest",
        "help": (
            "Leer = v6.db.transport.rest (Deutsche Bahn samt Nahverkehr, ohne Anmeldung). Jede Instanz von "
            "hafas-rest-api geht, z. B. https://v6.vbb.transport.rest für Berlin oder ein eigener db-vendo-client."
        ),
        "link_href":  "https://transport.rest",
        "link_label": "transport.rest",
    },
]

SETTINGS_GROUPS: list[dict] = [
    {"title": "Haltestellen", "desc": "Was auf der Tafel steht.", "fields": ["DEPARTURES_STOPS", "DEPARTURES_PRODUCTS", "DEPARTURES_WALK_MINUTES"]},
    {"title": "Umfang", "desc": "Zeitraum, Anzahl, Aktualisierung.", "fields": ["DEPARTURES_DURATION_MINUTES", "DEPARTURES_MAX", "DEPARTURES_CACHE_SECONDS"]},
    {"title": "Quelle", "desc": "Woher die Fahrplandaten kommen.", "fields": ["DEPARTURES_API_URL"]},
]


def _with_product_options(fields: list[dict]) -> list[dict]:
    from .data_source import PRODUCTS
    out = []
    for f in fields:
        if f["name"] == "DEPARTURES_PRODUCTS":
            f = {**f, "options": [list(p) for p in PRODUCTS]}
        out.append(f)
    return out


class DeparturesModule(InkwallModule):
    MODULE_ID          = "departures"
    MODULE_NAME        = "Abfahrten"
    MODULE_DESCRIPTION = (
        "Nächste Abfahrten von Bahn und ÖPNV an deinen Haltestellen – mit Verspätung, "
        "Linie, Ziel, Gleis und Minuten bis zur Abfahrt."
    )
    MODULE_PRIORITY  = 107
    SETTINGS_FIELDS  = _with_product_options(SETTINGS_FIELDS)
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        from .data_source import parse_stops
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle and bool(parse_stops(env.get("DEPARTURES_STOPS", "")))

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import fetch_departures_content
        try:
            return fetch_departures_content(False)
        except Exception as exc:
            log.error(f"DeparturesModule.fetch_content: {exc}", exc_info=True)
            return None

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_departures_module
        return render_departures_module(ModuleRenderServices.from_runtime(), content)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_departures_module
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        return render_departures_module(services, content, compact=True)

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .data_source import should_refresh_departures
        return should_refresh_departures()

    def get_state_key(self, content: Any) -> str:
        # Minute + sichtbare Zeilen: jede Minute ein neues Bild, solange sich etwas ändert
        if isinstance(content, dict):
            parts = []
            for section in content.get("sections", []):
                for row in section.get("rows", []):
                    parts.append(f"{row['line']}|{row['when']:%H%M}|{row.get('delay_min')}|{row.get('cancelled')}|{row.get('in_minutes')}")
            return ";".join(parts) or f"none:{content.get('now', '')[:16]}"
        return "departures"

    def get_next_wake_info(self, env: dict[str, str], state: str) -> dict | None:
        # Die Tafel lebt von der Minute – im Vollbild alle 60 s prüfen, mehr braucht das Panel nicht
        return {"seconds": 60, "reason": "Abfahrten – minütlich aktualisiert"}

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_stops
        stops = parse_stops(env.get("DEPARTURES_STOPS", ""))
        return {"Abfahrten-Haltestellen": ", ".join(label or query for label, query in stops) if stops else "Nicht konfiguriert"}

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_stops
        if not parse_stops(env.get("DEPARTURES_STOPS", "")):
            return {"state": "missing", "reason": "Haltestelle fehlt"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import parse_stops, fetch_departures_content
        stops = parse_stops(env.get("DEPARTURES_STOPS", ""))
        if not stops:
            return ""
        parts = [", ".join(label or query for label, query in stops)]
        try:
            content = fetch_departures_content(False)
        except Exception:
            content = None
        rows = [r for s in (content or {}).get("sections", []) for r in s.get("rows", [])]
        if rows:
            nxt = min(rows, key=lambda r: r["when"])
            parts.append(f"nächste {nxt['line']} nach {nxt['direction'][:24]} {'fällt aus' if nxt.get('cancelled') else 'in ' + str(nxt['in_minutes']) + ' min'}")
        elif content:
            errors = [s["error"] for s in content.get("sections", []) if s.get("error")]
            if errors:
                parts.append(errors[0])
        return " · ".join(parts)

    def probe(self, env: dict[str, str]) -> dict:
        """Löst die Haltestellen neu auf und zeigt, was gefunden wurde und was als Nächstes fährt."""
        from .data_source import PRODUCT_LABELS, api_base_url, fetch_departures_content, parse_stops, resolve_stop, search_locations
        stops = parse_stops(env.get("DEPARTURES_STOPS", ""))
        details: list[str] = [f"Schnittstelle: {api_base_url()}", "Haltestellen:"]
        ok = True
        for label, query in stops:
            if query.isdigit():
                details.append(f"{label or query}: Nummer {query}")
                continue
            try:
                found = search_locations(query, results=3)
            except Exception as exc:
                details.append(f"{label or query}: Suche fehlgeschlagen – {str(exc)[:80]}")
                ok = False
                continue
            if not found:
                details.append(f"{label or query}: nichts gefunden – anderen Namen oder die IBNR eintragen")
                ok = False
                continue
            resolve_stop(query, force=True)
            details.append(f"{label or query}: {found[0]['name']} ({found[0]['id']})"
                           + (f" – weitere Treffer: {', '.join(f'{x['name']} ({x['id']})' for x in found[1:])}" if len(found) > 1 else ""))
        content = fetch_departures_content(True) if ok or stops else None
        if not content:
            return {"ok": False, "message": "Keine Abfahrten ladbar – Schnittstelle oder Haltestellen prüfen", "details": details}
        details.append("Nächste Abfahrten:")
        rows = sorted((r for s in content["sections"] for r in s["rows"]), key=lambda r: r["when"])[:6]
        for r in rows:
            delay = f" (+{r['delay_min']})" if r.get("delay_min") else ""
            details.append(f"{r['when']:%H:%M}{delay} {r['line']} → {r['direction']}"
                           + (f" · Gl. {r['platform']}" if r.get("platform") else "") + (" · fällt aus" if r.get("cancelled") else ""))
        failed = [s["label"] for s in content["sections"] if s.get("error")]
        message = f"{sum(len(s['rows']) for s in content['sections'])} Abfahrten an {len(content['sections'])} Haltestelle{'n' if len(content['sections']) != 1 else ''}"
        if failed:
            message += f" · {', '.join(failed)} nicht ladbar"
        products = env.get("DEPARTURES_PRODUCTS", "").strip()
        if products:
            details.append("Verkehrsmittel: " + ", ".join(PRODUCT_LABELS.get(p.strip(), p) for p in products.split(",") if p.strip()))
        return {"ok": ok and not failed, "message": message, "details": details}

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        from .data_source import parse_stops
        return {"ok": True, "enabled": self.is_enabled(env), "stops": len(parse_stops(env.get("DEPARTURES_STOPS", "")))}

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        from .data_source import MAX_STOPS
        errors: list[str] = []
        raw = env.get("DEPARTURES_STOPS", "").strip()
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        if self.MODULE_ID in idle and not raw:
            errors.append("Haltestellen: Bitte mindestens eine Haltestelle angeben, wenn Abfahrten aktiv sind.")
        if raw and len([c for c in raw.replace("\n", ";").split(";") if c.strip()]) > MAX_STOPS:
            errors.append(f"Haltestellen: Höchstens {MAX_STOPS} Haltestellen.")
        api = env.get("DEPARTURES_API_URL", "").strip()
        if api and not api.lower().startswith(("http://", "https://")):
            errors.append("Fahrplan-Schnittstelle: Bitte eine Adresse mit http:// oder https:// angeben.")
        for key, label in (("DEPARTURES_DURATION_MINUTES", "Zeitraum (min)"), ("DEPARTURES_MAX", "Abfahrten je Haltestelle"),
                           ("DEPARTURES_WALK_MINUTES", "Fußweg (min)"), ("DEPARTURES_CACHE_SECONDS", "Abfahrten-Refresh (s)")):
            value = env.get(key, "").strip()
            if value and not value.isdigit():
                errors.append(f"{label}: Muss eine ganze Zahl sein.")
        return errors


module = DeparturesModule()
