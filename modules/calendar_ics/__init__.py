"""
Kalender-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 106). Liest einen oder mehrere ICS-Kalender
(Google, Nextcloud, iCloud, Outlook – der private ICS-Link reicht) und zeigt
die Termine von heute und der nächsten Tage, inklusive Wiederholungen.
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
        "name":        "CALENDAR_ICS_URLS",
        "secret":      True,          # steckt ein Zugangsweg drin: nicht im Export „ohne Geheimnisse“
        "label":       "Kalender",
        "type":        "list",
        "wide":        True,
        "item_fields": [
            {"name": "label", "label": "Name", "placeholder": "Familie"},
            {"name": "url",   "label": "ICS-Adresse", "placeholder": "https://calendar.google.com/calendar/ical/…/basic.ics", "wide": True},
        ],
        "help": (
            "Eine Zeile pro Kalender. Google: Kalender-Einstellungen → 'Privatadresse im iCal-Format'. "
            "Nextcloud: Kalender teilen → Link. webcal:// wird automatisch zu https://."
        ),
    },
    {
        "name":        "CALENDAR_DAYS_AHEAD",
        "label":       "Zeitraum (Tage)",
        "type":        "number",
        "wide":        False,
        "default":     "7",
        "placeholder": "7",
        "min":         1,
        "max":         60,
        "help":        "Wie viele Tage im Voraus gezeigt werden.",
    },
    {
        "name":        "CALENDAR_MAX_EVENTS",
        "label":       "Max. Termine",
        "type":        "number",
        "wide":        False,
        "default":     "14",
        "placeholder": "14",
        "min":         1,
        "max":         60,
        "help":        "Obergrenze für die Anzahl der gezeigten Termine (heute wird immer gezeigt).",
    },
    {
        "name":        "CALENDAR_CACHE_SECONDS",
        "label":       "Kalender-Refresh (s)",
        "type":        "number",
        "wide":        False,
        "default":     "900",
        "placeholder": "900",
        "min":         60,
        "max":         86400,
        "help":        "Wie oft die ICS-Dateien neu geladen werden.",
    },
    {
        "name":    "CALENDAR_HIDE_PAST_TODAY",
        "label":   "Vergangene Termine von heute",
        "type":    "select",
        "wide":    False,
        "default": "true",
        "options": [("true", "Ausblenden, sobald sie vorbei sind"), ("false", "Den ganzen Tag zeigen")],
        "help":    "Ganztägige Termine bleiben immer sichtbar.",
    },
]

SETTINGS_GROUPS: list[dict] = []


class CalendarModule(InkwallModule):
    MODULE_ID          = "calendar"
    MODULE_NAME        = "Kalender"
    MODULE_DESCRIPTION = (
        "Termine von heute und den nächsten Tagen aus ICS-Kalendern – "
        "mit Wiederholungen, mehreren Kalendern und Farbe je Quelle."
    )
    MODULE_PRIORITY  = 106
    SETTINGS_FIELDS  = SETTINGS_FIELDS
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        from .data_source import parse_sources
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle and bool(parse_sources(env.get("CALENDAR_ICS_URLS", "")))

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import fetch_calendar_content
        try:
            return fetch_calendar_content(False)
        except Exception as exc:
            log.error(f"CalendarModule.fetch_content: {exc}", exc_info=True)
            return None

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_calendar_module
        return render_calendar_module(ModuleRenderServices.from_runtime(), content)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_calendar_module
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        return render_calendar_module(services, content, compact=True)

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .data_source import should_refresh_calendar
        return should_refresh_calendar()

    def get_state_key(self, content: Any) -> str:
        # Tag + Fingerprint der sichtbaren Termine: neue/vorbei Termine → neues Bild
        if isinstance(content, dict):
            parts = []
            for day in content.get("days", []):
                for ev in day.get("events", []):
                    s = ev.get("start")
                    parts.append(f"{day['date']}|{s.strftime('%H%M') if hasattr(s, 'strftime') and not ev.get('all_day') else 'A'}|{ev.get('summary', '')[:30]}")
            return f"{content.get('today', '')}:" + ";".join(parts)
        return "calendar"

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_sources
        sources = parse_sources(env.get("CALENDAR_ICS_URLS", ""))
        return {
            "Kalender-Quellen": (", ".join(label or f"Kalender {i + 1}" for i, (label, _) in enumerate(sources))
                                 if sources else "Nicht konfiguriert"),
            "Kalender-Zeitraum": f"{env.get('CALENDAR_DAYS_AHEAD', '7')} Tage",
        }

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_sources, source_state
        sources = parse_sources(env.get("CALENDAR_ICS_URLS", ""))
        if not sources:
            return {"state": "missing", "reason": "ICS-Adresse fehlt"}
        # Nur aus dem Cache lesen (wird bei jedem Seitenaufruf gefragt): alle Quellen
        # gescheitert und nie etwas geladen → Fehler mit der ersten Meldung
        states = [source_state(url) for _, url in sources]
        if states and all(s["error"] and not s["loaded"] for s in states):
            return {"state": "error", "reason": f"nicht erreichbar ({states[0]['error']})"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import fetch_calendar_content, parse_sources
        sources = parse_sources(env.get("CALENDAR_ICS_URLS", ""))
        names = [label or f"Kalender {i + 1}" for i, (label, _) in enumerate(sources)]
        if not names:
            return ""
        parts = [", ".join(names), f"{env.get('CALENDAR_DAYS_AHEAD', '7')} Tage"]
        try:
            content = fetch_calendar_content(False)
        except Exception:
            content = None
        nxt = _next_event(content)
        if nxt:
            parts.append(f"nächster: {nxt}")
        if content and content.get("source_errors"):
            failed = ", ".join(e["label"] for e in content["source_errors"])
            parts.append(f"{failed} nicht erreichbar")
        return " · ".join(parts)

    def probe(self, env: dict[str, str]) -> dict:
        """
        Lädt alle Kalender neu und liefert Details: je Quelle Anzahl oder
        Fehler, dazu die nächsten Termine mit Quelle.
        """
        from .data_source import fetch_calendar_content, parse_sources, source_state
        sources = parse_sources(env.get("CALENDAR_ICS_URLS", ""))
        content = fetch_calendar_content(True)
        days_ahead = (content or {}).get("days_ahead") or env.get("CALENDAR_DAYS_AHEAD", "7")
        details: list[str] = ["Quellen:"]
        if content:
            for src in content["sources"]:
                if src["error"] and not src["loaded"]:
                    details.append(f"{src['label']}: Fehler – {src['error']}")
                elif src["error"]:
                    details.append(f"{src['label']}: {src['count']} Termine (gespeicherter Stand, Quelle meldet {src['error']})")
                else:
                    details.append(f"{src['label']}: {src['count']} Termine in den nächsten {days_ahead} Tagen")
        else:
            for i, (label, url) in enumerate(sources):
                state = source_state(url)
                details.append(f"{label or f'Kalender {i + 1}'}: Fehler – {state['error'] or 'keine Antwort'}")
            return {"ok": False, "message": "Kein Kalender ladbar", "details": details}

        upcoming = _upcoming_lines(content, limit=6)
        if upcoming:
            details.append("Nächste Termine:")
            details.extend(upcoming)
        else:
            details.append(f"Keine Termine in den nächsten {days_ahead} Tagen.")
        if content.get("stale_since"):
            details.append("Achtung: Mindestens eine Quelle ist gerade nicht erreichbar, gezeigt wird der letzte gespeicherte Stand.")
        ok = not content.get("source_errors") or any(s["loaded"] for s in content["sources"])
        failed = [e["label"] for e in content.get("source_errors", [])]
        message = f"{content.get('total_events', 0)} Termine in den nächsten {days_ahead} Tagen"
        if failed:
            message += f" · {', '.join(failed)} nicht erreichbar"
        return {"ok": ok, "message": message, "details": details}

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        from .data_source import parse_sources
        return {
            "ok": True,
            "enabled": self.is_enabled(env),
            "sources": len(parse_sources(env.get("CALENDAR_ICS_URLS", ""))),
        }

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        from .data_source import parse_sources
        errors: list[str] = []
        raw = env.get("CALENDAR_ICS_URLS", "").strip()
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        if self.MODULE_ID in idle and not raw:
            errors.append("Kalender: Bitte mindestens eine ICS-Adresse angeben, wenn das Modul aktiv ist.")
        if raw and not parse_sources(raw):
            errors.append("Kalender: Keine gültige ICS-Adresse gefunden (http://, https:// oder webcal://).")
        for key, label in (("CALENDAR_DAYS_AHEAD", "Zeitraum (Tage)"), ("CALENDAR_MAX_EVENTS", "Max. Termine")):
            value = env.get(key, "").strip()
            if value and not value.isdigit():
                errors.append(f"{label}: Muss eine ganze Zahl sein.")
        return errors


def _event_when(day: dict, ev: dict) -> str:
    """'heute 14:00', 'morgen ganztägig', 'Sa 12.09.' – kurz, für Zusammenfassung und Prüfen."""
    from app.config import format_weekday_short
    d = day["date"]
    prefix = {0: "heute", 1: "morgen"}.get(day["in_days"], f"{format_weekday_short(d)} {d.day:02d}.{d.month:02d}.")
    if ev.get("all_day"):
        return f"{prefix} ganztägig"
    start = ev.get("start")
    if ev.get("continues"):
        return f"{prefix} weiter"
    return f"{prefix} {start:%H:%M}" if hasattr(start, "strftime") else prefix


def _next_event(content: dict | None) -> str:
    if not content:
        return ""
    for day in content.get("days", []):
        for ev in day.get("events", []):
            title = (ev.get("summary") or "").strip()
            if len(title) > 32:
                title = title[:31].rstrip() + "…"
            return f"{_event_when(day, ev)} {title}"
    return ""


def _upcoming_lines(content: dict, limit: int = 6) -> list[str]:
    lines: list[str] = []
    for day in content.get("days", []):
        for ev in day.get("events", []):
            label = ev.get("label") or ""
            lines.append(f"{_event_when(day, ev)} · {ev.get('summary', '')}" + (f" ({label})" if label else ""))
            if len(lines) >= limit:
                return lines
    return lines


module = CalendarModule()
