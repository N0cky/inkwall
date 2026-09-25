"""
NINA-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 102). Zeigt Warnungen des Bevölkerungsschutzes
(warnung.bund.de: Katastrophenschutz, Hochwasser, Polizei, Warntag) für den
Kreis des eingestellten Orts. Ohne aktive Warnung hat das Modul keinen Inhalt
und erscheint nicht; mit Warnung ist es dringend und steht vorn in der
Rotation bzw. oben im Dashboard. Wetterwarnungen zeigt das DWD-Wettermodul.
"""

from __future__ import annotations

from typing import Any

from PIL import Image

from app.logger import get_logger
from app.module_base import InkwallModule
from app.module_services import ModuleRenderServices

log = get_logger(__name__)


def _severity_options() -> list[list[str]]:
    from .data_source import MIN_SEVERITY_OPTIONS
    return [list(o) for o in MIN_SEVERITY_OPTIONS]


SETTINGS_FIELDS: list[dict] = [
    {
        "name":        "NINA_REGION",
        "label":       "Ort",
        "type":        "text",
        "wide":        True,
        "default":     "",
        "placeholder": "Wetzlar",
        "help": (
            "Gemeinde oder Stadt – gewarnt wird für den ganzen Kreis (Wetzlar → Lahn-Dill-Kreis). "
            "Statt des Namens geht auch der Regionalschlüssel (065320000000). „Verbindung prüfen“ zeigt, "
            "welcher Ort gefunden wurde und was gerade gilt."
        ),
    },
    {
        "name":    "NINA_MIN_SEVERITY",
        "label":   "Zeigen ab",
        "type":    "select",
        "wide":    False,
        "default": "minor",
        "options": _severity_options(),
        "help":    "Auch Hinweise sind oft wichtig (Trinkwasser, Bombenentschärfung, Warntag).",
    },
    {
        "name":        "NINA_CACHE_SECONDS",
        "label":       "Warnungen-Refresh (s)",
        "type":        "number",
        "wide":        False,
        "default":     "300",
        "placeholder": "300",
        "min":         60,
        "max":         3600,
        "help":        "Wie oft nach neuen Warnungen gesehen wird.",
    },
]

SETTINGS_GROUPS: list[dict] = []


class NinaModule(InkwallModule):
    MODULE_ID          = "nina"
    MODULE_NAME        = "Warnungen (NINA)"
    MODULE_DESCRIPTION = (
        "Warnungen des Bevölkerungsschutzes für deinen Kreis – Katastrophenschutz, Hochwasser, "
        "Polizei, Warntag. Erscheint nur, solange eine Warnung gilt, und steht dann vorn."
    )
    MODULE_PRIORITY  = 102
    SETTINGS_FIELDS  = SETTINGS_FIELDS
    SETTINGS_GROUPS  = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle and bool(env.get("NINA_REGION", "").strip())

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import fetch_warnings
        try:
            content = fetch_warnings(False)
        except Exception as exc:
            log.error(f"NinaModule.fetch_content: {exc}", exc_info=True)
            return None
        # Ohne aktive Warnung kein Inhalt: das Modul fällt aus der Rotation und dem Dashboard
        return content if content and content.get("warnings") else None

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_nina_module
        return render_nina_module(ModuleRenderServices.from_runtime(), content)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_nina_module
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        return render_nina_module(services, content, compact=True)

    def should_refresh(self, env: dict[str, str]) -> bool:
        from .data_source import should_refresh_nina
        return should_refresh_nina()

    def get_state_key(self, content: Any) -> str:
        if isinstance(content, dict):
            return ";".join(f"{w['id']}:{w['version']}" for w in content.get("warnings", [])) or "nina:none"
        return "nina"

    def is_urgent(self, env: dict[str, str]) -> bool:
        from .data_source import cached_warnings
        return self.is_enabled(env) and bool(cached_warnings())

    def get_alerts(self, env: dict[str, str]) -> list[dict]:
        from .data_source import cached_warnings
        if not env.get("NINA_REGION", "").strip():
            return []
        alerts = []
        for w in cached_warnings():
            expires = w.get("expires", "")
            alerts.append({
                "id": f"nina:{w['id']}",           # gleiche ID über alle Aktualisierungen
                "title": w.get("headline") or "Warnung",
                "text": w.get("description") or "",
                "source": " · ".join(p for p in (w.get("provider_label"), w.get("sender")) if p),
                "severity": w.get("severity", "minor"),
                "until": f"{expires[8:10]}.{expires[5:7]}. {expires[11:16]}" if len(expires) >= 16 else "",
                "area": w.get("area", ""),
            })
        return alerts

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        return {"NINA-Ort": env.get("NINA_REGION", "").strip() or "Nicht konfiguriert"}

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        if not env.get("NINA_REGION", "").strip():
            return {"state": "missing", "reason": "Ort fehlt"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import fetch_warnings
        place = env.get("NINA_REGION", "").strip()
        if not place:
            return ""
        try:
            content = fetch_warnings(False)
        except Exception:
            content = None
        warnings = (content or {}).get("warnings") or []
        if not warnings:
            return f"{place} · keine Warnung"
        return f"{place} · {len(warnings)} Warnung{'en' if len(warnings) > 1 else ''}: {warnings[0]['headline'][:60]}"

    def probe(self, env: dict[str, str]) -> dict:
        """Ort auflösen, Warnungen neu laden, alles auflisten."""
        from .data_source import fetch_warnings, resolve_region
        region = resolve_region()
        if not region["ars"]:
            return {"ok": False, "message": "Ort nicht gefunden – anderen Namen oder den Regionalschlüssel eintragen",
                    "details": ["Die Gemeindeliste kommt vom Statistischen Bundesamt (xrepository.de)."]}
        details = ["Ort:", f"{region['name']} → Kreis {region['ars'][:5]} (Regionalschlüssel {region['ars']})"]
        if len(region["matches"]) > 1:
            details.append("Weitere Treffer: " + ", ".join(f"{n} ({c[:5]})" for c, n in region["matches"][1:6]))
        content = fetch_warnings(True)
        if content is None or (content.get("error") and not content.get("fetched_at")):
            return {"ok": False, "message": f"warnung.bund.de nicht erreichbar{': ' + content['error'] if content else ''}",
                    "details": details}
        warnings = content.get("warnings") or []
        if warnings:
            details.append("Aktive Warnungen:")
            for w in warnings:
                details.append(f"{w['severity_label']} · {w['provider_label']}: {w['headline']}")
        else:
            details.append("Gerade keine Warnung – das Modul erscheint erst, wenn eine gilt.")
        if content.get("excluded"):
            details.append(f"{content['excluded']} Wetterwarnung(en) ausgelassen – die zeigt das DWD-Wetter.")
        message = f"{len(warnings)} aktive Warnung{'en' if len(warnings) != 1 else ''} für {region['name']}"
        return {"ok": True, "message": message, "details": details}

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        return {"ok": True, "enabled": self.is_enabled(env), "configured": bool(env.get("NINA_REGION", "").strip())}

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        errors: list[str] = []
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        if self.MODULE_ID in idle and not env.get("NINA_REGION", "").strip():
            errors.append("Ort: Bitte einen Ort angeben, wenn die Warnungen aktiv sind.")
        value = env.get("NINA_CACHE_SECONDS", "").strip()
        if value and not value.isdigit():
            errors.append("Warnungen-Refresh (s): Muss eine ganze Zahl sein.")
        return errors


module = NinaModule()
