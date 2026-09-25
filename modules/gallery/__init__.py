"""
Gallery-Modul für Inkwall.

Idle-Modul (MODULE_PRIORITY = 120): zeigt Bilder aus lokalen Ordnern an,
wenn kein Prioritätsmodul aktiv ist.
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
        "name":        "GALLERY_PATHS",
        "label":       "Bildordner",
        "type":        "list",
        "wide":        True,
        "default":     "",
        "item_fields": [{"name": "path", "label": "Ordner", "placeholder": "/pictures oder C:\\Bilder", "wide": True}],
        "help":        "Ein oder mehrere Ordner auf dem Server. Im Docker-Container muss der Ordner als Volume eingehängt sein.",
    },
    {
        "name":    "GALLERY_RECURSIVE",
        "label":   "Unterordner einbeziehen",
        "type":    "select",
        "wide":    False,
        "default": "true",
        "options": [
            ("true", "Ja – Ordner rekursiv durchsuchen"),
            ("false", "Nein – nur den Ordner selbst"),
        ],
        "help": "Legt fest, ob Bilder auch aus Unterordnern geladen werden.",
    },
    {
        "name":    "GALLERY_ORDER",
        "label":   "Bildauswahl",
        "type":    "select",
        "wide":    False,
        "default": "random",
        "options": [
            ("random", "Zufällig"),
            ("sequential", "Der Reihe nach"),
        ],
        "help": "Standardmäßig zufällig, auf Wunsch stabil der Reihe nach.",
    },
    {
        "name":    "GALLERY_INTERVAL_MODE",
        "label":   "Wechselintervall",
        "type":    "select",
        "wide":    False,
        "default": "idle_rotation",
        "options": [
            ("idle_rotation", "Idle-Rotation verwenden"),
            ("custom", "Eigenes Intervall"),
        ],
        "help": "Steuert, ob die Galerie dem allgemeinen Idle-Takt folgt oder ein eigenes Intervall nutzt.",
    },
    {
        "name":        "GALLERY_INTERVAL_SECONDS",
        "label":       "Eigenes Intervall (s)",
        "type":        "number",
        "wide":        False,
        "default":     "300",
        "placeholder": "300",
        "min":         30,
        "max":         86400,
        "help":        "Wird nur genutzt, wenn 'Eigenes Intervall' ausgewählt ist.",
    },
    {
        "name":        "GALLERY_AVOID_RECENT_COUNT",
        "label":       "Recent-Avoidance",
        "type":        "number",
        "wide":        False,
        "default":     "5",
        "placeholder": "5",
        "min":         0,
        "max":         50,
        "help":        "Wie viele zuletzt gezeigte Bilder bei Zufallsauswahl möglichst vermieden werden.",
    },
    {
        "name":    "GALLERY_FIT_MODE",
        "label":   "Bilddarstellung",
        "type":    "select",
        "wide":    False,
        "default": "fit_blur_bg",
        "options": [
            ("fit_blur_bg", "Passend skalieren + Blur-Hintergrund"),
            ("cover", "Vollflächig mit Crop"),
        ],
        "help": "Wie Bilder auf die Displayfläche eingepasst werden.",
    },
    {
        "name":    "GALLERY_OVERLAY_MODE",
        "label":   "Overlay",
        "type":    "select",
        "wide":    False,
        "default": "none",
        "options": [
            ("none", "Kein Overlay"),
            ("filename", "Dateiname"),
            ("folder", "Ordnername"),
            ("filename_folder", "Datei + Ordner"),
        ],
        "help": "Legt fest, ob und welche Bildinfos unten eingeblendet werden.",
    },
]

SETTINGS_GROUPS: list[dict] = [
    {
        "title":  "Quelle",
        "desc":   "Lokale Bildordner und Suchbereich.",
        "fields": ["GALLERY_PATHS", "GALLERY_RECURSIVE", "GALLERY_ORDER"],
    },
    {
        "title":  "Timing",
        "desc":   "Wechselintervall der angezeigten Bilder.",
        "fields": ["GALLERY_INTERVAL_MODE", "GALLERY_INTERVAL_SECONDS", "GALLERY_AVOID_RECENT_COUNT"],
    },
    {
        "title":  "Darstellung",
        "desc":   "Wie Bilder eingepasst und beschriftet werden.",
        "fields": ["GALLERY_FIT_MODE", "GALLERY_OVERLAY_MODE"],
    },
]


class GalleryModule(InkwallModule):
    MODULE_ID = "gallery"
    MODULE_NAME = "Gallery"
    MODULE_DESCRIPTION = (
        "Zeigt Bilder aus lokalen Ordnern als Idle-Modul an. "
        "Unterstützt Zufallsauswahl, eigenes Intervall und Blur-Hintergrund."
    )
    MODULE_PRIORITY = 120
    SETTINGS_FIELDS = SETTINGS_FIELDS
    SETTINGS_GROUPS = SETTINGS_GROUPS

    def is_enabled(self, env: dict[str, str]) -> bool:
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self.MODULE_ID in idle

    def fetch_content(self, env: dict[str, str]) -> dict | None:
        from .data_source import choose_gallery_image, parse_gallery_paths

        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        if not paths:
            return None
        recursive = env.get("GALLERY_RECURSIVE", "true").strip().lower() == "true"
        order = env.get("GALLERY_ORDER", "random")
        return choose_gallery_image(paths, recursive, order)

    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        from .renderer import render_gallery_image

        services = ModuleRenderServices.from_runtime()
        fit_mode = env.get("GALLERY_FIT_MODE", "fit_blur_bg").strip().lower()
        overlay_mode = env.get("GALLERY_OVERLAY_MODE", "none").strip().lower()
        return render_gallery_image(services, content, fit_mode, overlay_mode)

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        from .renderer import render_gallery_image
        base = ModuleRenderServices.from_runtime()
        services = ModuleRenderServices(render_width=width, render_height=height,
                                        display_theme=base.display_theme, load_font=base.load_font)
        fit_mode = env.get("GALLERY_FIT_MODE", "fit_blur_bg").strip().lower()
        overlay_mode = env.get("GALLERY_OVERLAY_MODE", "none").strip().lower()
        return render_gallery_image(services, content, fit_mode, overlay_mode)

    def get_state_key(self, content: Any) -> str:
        if isinstance(content, dict):
            image_path = str(content.get("image_path", ""))
            slot = content.get("slot", 0)
            mtime_ns = content.get("mtime_ns", 0)
            return f"{slot}:{image_path}:{mtime_ns}"
        return "gallery"

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import parse_gallery_paths

        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        return {
            "Gallery-Ordner":   f"{len(paths)} Ordner" if paths else "Keine",
            "Gallery-Modus":    env.get("GALLERY_FIT_MODE", "fit_blur_bg"),
            "Gallery-Intervall": (
                "Idle-Rotation"
                if env.get("GALLERY_INTERVAL_MODE", "idle_rotation") != "custom"
                else f"{env.get('GALLERY_INTERVAL_SECONDS', '300')}s"
            ),
        }

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        from .data_source import is_path_allowed, parse_gallery_paths
        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        if not paths:
            return {"state": "missing", "reason": "Bildordner fehlt"}
        missing = [str(p) for p in paths if not (p.exists() and p.is_dir())]
        if missing:
            return {"state": "error", "reason": f"Ordner nicht gefunden: {missing[0]}"}
        blocked = [str(p) for p in paths if not is_path_allowed(p)]
        if len(blocked) == len(paths):
            return {"state": "error", "reason": f"Ordner nicht freigegeben: {blocked[0]}"}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        from .data_source import parse_gallery_paths
        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        if not paths:
            return ""
        mode = {"fit_blur_bg": "Einpassen mit Unschärfe", "cover": "Bildfüllend"}.get(env.get("GALLERY_FIT_MODE", "fit_blur_bg"), "")
        return " · ".join(x for x in (f"{len(paths)} Ordner", mode) if x)

    def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
        from .data_source import parse_gallery_paths

        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        existing = sum(1 for path in paths if path.exists() and path.is_dir())
        return {
            "ok": existing == len(paths),
            "enabled": self.is_enabled(env),
            "configured_paths": len(paths),
            "existing_paths": existing,
        }

    def get_next_wake_seconds(self, env: dict[str, str], state: str) -> int | None:
        if env.get("GALLERY_INTERVAL_MODE", "idle_rotation").strip().lower() == "custom":
            raw = env.get("GALLERY_INTERVAL_SECONDS", "300").strip()
            if raw.isdigit():
                return max(30, min(86400, int(raw)))
        return None

    def get_next_wake_info(self, env: dict[str, str], state: str) -> dict[str, object] | None:
        if env.get("GALLERY_INTERVAL_MODE", "idle_rotation").strip().lower() != "custom":
            return None
        raw = env.get("GALLERY_INTERVAL_SECONDS", "300").strip()
        if not raw.isdigit():
            return None
        seconds = max(30, min(86400, int(raw)))
        return {
            "seconds": seconds,
            "reason": "Gallery – eigenes Bildwechsel-Intervall",
        }

    def get_background_poll_seconds(self, env: dict[str, str]) -> int | None:
        if env.get("GALLERY_INTERVAL_MODE", "idle_rotation").strip().lower() != "custom":
            return None
        raw = env.get("GALLERY_INTERVAL_SECONDS", "300").strip()
        if not raw.isdigit():
            return None
        return max(30, min(86400, int(raw)))

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        from .data_source import GALLERY_ROOTS_ENV, allowed_gallery_roots, is_path_allowed, parse_gallery_paths

        errors: list[str] = []
        paths = parse_gallery_paths(env.get("GALLERY_PATHS", ""))
        idle_modules = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        fit_mode = env.get("GALLERY_FIT_MODE", "fit_blur_bg").strip().lower()
        order = env.get("GALLERY_ORDER", "random").strip().lower()
        overlay_mode = env.get("GALLERY_OVERLAY_MODE", "none").strip().lower()
        interval_mode = env.get("GALLERY_INTERVAL_MODE", "idle_rotation").strip().lower()

        if fit_mode not in {"fit_blur_bg", "cover"}:
            errors.append("Bilddarstellung: Ungültige Auswahl.")
        if order not in {"random", "sequential"}:
            errors.append("Bildauswahl: Ungültige Auswahl.")
        if overlay_mode not in {"none", "filename", "folder", "filename_folder"}:
            errors.append("Overlay: Ungültige Auswahl.")
        if interval_mode not in {"idle_rotation", "custom"}:
            errors.append("Wechselintervall: Ungültige Auswahl.")
        if interval_mode == "custom":
            raw = env.get("GALLERY_INTERVAL_SECONDS", "").strip()
            if not raw.isdigit():
                errors.append("Eigenes Intervall (s): Muss eine ganze Zahl sein.")
        avoid_recent_raw = env.get("GALLERY_AVOID_RECENT_COUNT", "").strip()
        if avoid_recent_raw and not avoid_recent_raw.isdigit():
            errors.append("Recent-Avoidance: Muss eine ganze Zahl sein.")
        if self.MODULE_ID in idle_modules and not paths:
            errors.append("Bildordner: Bitte mindestens einen lokalen Ordner angeben, wenn Gallery aktiv ist.")
        roots = allowed_gallery_roots()
        for path in paths:
            if not path.exists():
                errors.append(f"Bildordner: Pfad nicht gefunden: {path}")
            elif not path.is_dir():
                errors.append(f"Bildordner: Kein Verzeichnis: {path}")
            elif not is_path_allowed(path, roots):
                allowed = ", ".join(str(r) for r in roots)
                errors.append(f"Bildordner: {path} liegt außerhalb der freigegebenen Ordner ({GALLERY_ROOTS_ENV}: {allowed}).")

        return errors


module = GalleryModule()
