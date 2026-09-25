"""
Müllabfuhr-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 105). Liest ICS-Abfuhrkalender (eine oder
mehrere Adressen), zeigt den nächsten Abfuhrtag groß und die Termine der
nächsten Tage als Wochenstreifen oder Liste. Tonnenfarben werden aus dem
Terminnamen abgeleitet und lassen sich pro Stichwort überschreiben.

Am Vorabend (ab GARBAGE_REMINDER_HOUR) und am Abfuhrtag bis GARBAGE_DONE_HOUR
ist das Modul "dringend": der Hero bekommt ein farbiges Band und das Modul
wird in der Rotation vorgezogen bzw. im Dashboard nach oben sortiert.
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
        "name":        "GARBAGE_ICS_URLS",
        "secret":      True,          # steckt ein Zugangsweg drin: nicht im Export „ohne Geheimnisse“
        "label":       "Abfuhrkalender",
        "type":        "list",
        "wide":        True,
        "item_fields": [
            {"name": "label", "label": "Name", "placeholder": "Zuhause"},
            {"name": "url",   "label": "ICS-Adresse", "placeholder": "https://…/abfuhrtermine-{year}.php?…&icalDownload=true", "wide": True},
        ],
        "help": (
            "Eine Zeile pro Adresse. Steht das Jahr in der URL, ersetze es durch {year} – dann lädt das "
            "Modul jedes Jahr automatisch den passenden Kalender und ab Ende November zusätzlich das Folgejahr."
        ),
    },
    {
        "name":        "GARBAGE_DAYS_AHEAD",
        "label":       "Zeitraum (Tage)",
        "type":        "number",
        "wide":        False,
        "default":     "14",
        "placeholder": "14",
        "min":         1,
        "max":         90,
        "help":        "Wie viele Tage im Voraus der Wochenstreifen bzw. die Terminliste zeigt.",
    },
    {
        "name":        "GARBAGE_UPCOMING_STYLE",
        "label":       "Darstellung der nächsten Tage",
        "type":        "select",
        "wide":        False,
        "default":     "strip",
        "options":     [("strip", "Wochenstreifen mit Symbolen"), ("list", "Liste mit Zeilen")],
        "help":        "Der Streifen zeigt jeden Tag als Zelle mit Tonnen-Symbolen, die Liste eine Zeile je Abfuhrtag.",
    },
    {
        "name":        "GARBAGE_LAYOUT",
        "label":       "Mehrere Adressen",
        "type":        "select",
        "wide":        False,
        "default":     "merged",
        "options":     [("merged", "Zusammen, Adresse hinter der Tonne"), ("columns", "Eine Spalte je Adresse")],
        "help":        "Nur relevant bei zwei oder mehr Kalendern. Spalten lohnen sich, wenn die Adressen meist verschiedene Termine haben.",
    },
    {
        "name":        "GARBAGE_REMINDER_HOUR",
        "label":       "Erinnerung ab (Uhr)",
        "type":        "number",
        "wide":        False,
        "default":     "18",
        "placeholder": "18",
        "min":         0,
        "max":         23,
        "help":        "Ab dieser Uhrzeit am Vortag wird „Morgen rausstellen“ hervorgehoben und das Modul in der Rotation vorgezogen.",
    },
    {
        "name":        "GARBAGE_DONE_HOUR",
        "label":       "Erledigt ab (Uhr)",
        "type":        "number",
        "wide":        False,
        "default":     "12",
        "placeholder": "12",
        "min":         0,
        "max":         23,
        "help":        "Ab dieser Uhrzeit am Abfuhrtag gilt die Tonne als geleert; das Display zeigt dann schon den nächsten Termin.",
    },
    {
        "name":        "GARBAGE_CACHE_SECONDS",
        "label":       "Kalender-Refresh (s)",
        "type":        "number",
        "wide":        False,
        "default":     "21600",
        "placeholder": "21600",
        "min":         300,
        "max":         604800,
        "help":        "Wie oft die ICS-Dateien neu geladen werden. Abfuhrkalender ändern sich selten.",
    },
    {
        "name":        "GARBAGE_TYPE_COLORS",
        "label":       "Tonnenfarben",
        "type":        "mapping",
        "wide":        True,
        "item_fields": [
            {"name": "key",   "label": "Stichwort im Terminnamen", "placeholder": "bio"},
            {"name": "value", "label": "Farbe"},
        ],
        "value_options": [("black", "Schwarz"), ("green", "Grün"), ("yellow", "Gelb"), ("blue", "Blau"), ("red", "Rot")],
        "help": (
            "Optional. Ohne Angabe gilt die eingebaute Zuordnung: Restmüll schwarz, Bio grün, "
            "Gelbe Tonne gelb, Papier blau, Sperrmüll rot. „Verbindung prüfen“ zeigt, wie jeder Terminname erkannt wird."
        ),
    },
]

SETTINGS_GROUPS: list[dict] = []


def _hour_setting(env: dict[str, str], key: str, default: int) -> int:
    try:
        return max(0, min(23, int(str(env.get(key, "")).strip() or default)))
    except ValueError:
        return default


class GarbageModule(InkwallModule):
    MODULE_ID          = "garbage"
    MODULE_NAME        = "Müllabfuhr"
    MODULE_DESCRIPTION = (
        "Zeigt die nächsten Abfuhrtermine aus ICS-Kalendern der Kommune – "
        "nächster Termin groß mit Tonne, Erinnerung am Vorabend, dann die kommenden Tage."
    )
    MODULE_PRIORITY  = 105
    SETTINGS_FIELDS  = SETTINGS_FIELDS
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        from .data_source import parse_sources
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle and bool(parse_sources(env.get("GARBAGE_ICS_URLS", "")))

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import fetch_garbage_content
        try:
            return fetch_garbage_content(False)
        except Exception as exc:
            log.error(f"GarbageModule.fetch_content: {exc}", exc_info=True)
            return None

    def _render_options(self, env: dict[str, str]) -> dict:
        layout = (env.get("GARBAGE_LAYOUT", "") or "merged").strip().lower()
        upcoming = (env.get("GARBAGE_UPCOMING_STYLE", "") or "strip").strip().lower()
        return {
            "layout": layout if layout in ("merged", "columns") else "merged",
            "upcoming": upcoming if upcoming in ("strip", "list") else "strip",
        }

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_garbage_module
        return render_garbage_module(ModuleRenderServices.from_runtime(), content, **self._render_options(env))

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_garbage_module
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        return render_garbage_module(services, content, compact=True, **self._render_options(env))

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .data_source import should_refresh_garbage
        return should_refresh_garbage()

    def is_urgent(self, env: dict[str, str]) -> bool:
        """Abfuhr heute (bis „Erledigt ab“) oder morgen ab „Erinnerung ab“."""
        if not self.is_enabled(env):
            return False
        content = self.fetch_content(env)
        return bool(content and content.get("urgent"))

    def get_state_key(self, content: Any) -> str:
        # Tag + nächster Termin + Dringlichkeit: neuer Tag oder Abend → neues Bild
        if isinstance(content, dict):
            nxt = content.get("next") or {}
            names = ",".join(ev.get("summary", "") for ev in nxt.get("events", []))
            flags = ("urgent" if content.get("urgent") else "") + ("stale" if content.get("stale_since") else "")
            return f"{content.get('today', '')}:{nxt.get('date', '')}:{names}:{flags}"
        return "garbage"

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_sources
        sources = parse_sources(env.get("GARBAGE_ICS_URLS", ""))
        return {
            "Müll-Kalender": f"{len(sources)} Quelle{'n' if len(sources) != 1 else ''}" if sources else "Nicht konfiguriert",
            "Müll-Zeitraum": f"{env.get('GARBAGE_DAYS_AHEAD', '14')} Tage",
        }

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_sources
        if not parse_sources(env.get("GARBAGE_ICS_URLS", "")):
            return {"state": "missing", "reason": "ICS-Adresse fehlt"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import parse_sources, fetch_garbage_content
        sources = parse_sources(env.get("GARBAGE_ICS_URLS", ""))
        if not sources:
            return ""
        parts = [f"{len(sources)} Adresse{'n' if len(sources) != 1 else ''}"]
        try:
            content = fetch_garbage_content(False)
        except Exception:
            content = None
        nxt = (content or {}).get("next")
        if nxt:
            from app.config import format_weekday_short
            d = nxt["date"]
            names = ", ".join(sorted({ev["summary"] for ev in nxt["events"]})[:2])
            parts.append(f"nächste Abfuhr {format_weekday_short(d)} {d.day:02d}.{d.month:02d}. {names}")
        if content and content.get("missing_years"):
            parts.append(f"Kalender {', '.join(str(y) for y in content['missing_years'])} fehlt")
        return " · ".join(parts)

    def probe(self, env: dict[str, str]) -> dict:
        """
        Lädt die Kalender neu und liefert neben der Meldung Details: die
        nächsten drei Abfuhrtage und wie jeder Terminname erkannt wird.
        """
        from app.config import format_weekday_short
        from .data_source import COLOR_LABELS, ICON_LABELS, fetch_garbage_content
        content = fetch_garbage_content(True)
        if not content:
            return {"ok": False, "message": "Kalender nicht ladbar oder keine kommenden Termine"}
        details: list[str] = []
        days = content.get("days") or []
        nxt = content.get("next")
        shown = days[:3] if days else ([nxt] if nxt else [])
        if shown:
            details.append("Nächste Termine:")
            for day in shown:
                per_type: dict[str, list[str]] = {}
                for ev in day["events"]:
                    per_type.setdefault(ev["summary"], [])
                    if ev.get("label") and ev["label"] not in per_type[ev["summary"]]:
                        per_type[ev["summary"]].append(ev["label"])
                items = [name + (f" ({', '.join(labels)})" if labels else "") for name, labels in per_type.items()]
                d = day["date"]
                details.append(f"{format_weekday_short(d)} {d.day:02d}.{d.month:02d}. · {day['relative']} · {', '.join(items)}")
        kinds = content.get("kinds") or []
        if kinds:
            details.append("Erkannte Tonnen:")
            for k in kinds:
                details.append(f"{k['summary']} → {COLOR_LABELS.get(k['color'], k['color'])}, {ICON_LABELS.get(k['icon'], k['icon'])}")
        if content.get("missing_years"):
            details.append(f"Kalender {', '.join(str(y) for y in content['missing_years'])} antwortet mit 404 – noch nicht online.")
        if content.get("stale_since"):
            details.append("Achtung: Die Quelle ist gerade nicht erreichbar, gezeigt wird der letzte gespeicherte Stand.")
        if not days and content.get("missing_years"):
            return {"ok": False, "message": "Keine Termine – der Jahreskalender ist noch nicht online", "details": details}
        return {
            "ok": True,
            "message": f"{len(days)} Abfuhrtage in den nächsten {content.get('days_ahead', 14)} Tagen",
            "details": details,
        }

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        from .data_source import parse_sources
        sources = parse_sources(env.get("GARBAGE_ICS_URLS", ""))
        return {
            "ok": True,
            "enabled": self.is_enabled(env),
            "sources": len(sources),
        }

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        from .data_source import parse_sources, parse_type_overrides, VALID_COLORS
        errors: list[str] = []
        raw = env.get("GARBAGE_ICS_URLS", "").strip()
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        if self.MODULE_ID in idle and not raw:
            errors.append("Müllabfuhr: Bitte mindestens eine ICS-Adresse angeben, wenn das Modul aktiv ist.")
        if raw and not parse_sources(raw):
            errors.append("Müllabfuhr: Keine gültige ICS-Adresse gefunden (muss mit http:// oder https:// beginnen).")
        colors_raw = env.get("GARBAGE_TYPE_COLORS", "").strip()
        if colors_raw:
            chunks = [c for c in colors_raw.split(",") if c.strip()]
            if len(parse_type_overrides(colors_raw)) != len(chunks):
                errors.append(f"Tonnenfarben: Format 'stichwort=farbe', Farben: {', '.join(VALID_COLORS)}.")
        days = env.get("GARBAGE_DAYS_AHEAD", "").strip()
        if days and not days.isdigit():
            errors.append("Zeitraum (Tage): Muss eine ganze Zahl sein.")
        for key, label in (("GARBAGE_REMINDER_HOUR", "Erinnerung ab (Uhr)"), ("GARBAGE_DONE_HOUR", "Erledigt ab (Uhr)")):
            raw_hour = env.get(key, "").strip()
            if raw_hour and (not raw_hour.isdigit() or int(raw_hour) > 23):
                errors.append(f"{label}: Bitte eine Stunde von 0 bis 23 angeben.")
        return errors


module = GarbageModule()
