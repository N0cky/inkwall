"""
Dashboard-Modus (IDLE_LAYOUT=dashboard): mehrere Idle-Module teilen sich ein
Bild. Jedes Modul liefert über render_tile() eine Kachel, das Framework setzt
sie untereinander zusammen – mit einer Kopfzeile (Datum, Stand) und Trennlinien.

Vorteil gegenüber der Rotation: weniger Display-Refreshes, alles auf einen Blick.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from PIL import Image

from app.config import RuntimeConfig, format_date_long, load_font, now_local
from app.image_rendering import SPECTRA6_COLORS
from app.logger import get_logger
from app.module_base import InkwallModule
from app.text_rendering import new_draw

log = get_logger(__name__)

_PALETTES = {
    "dark":  {"bg": (26, 28, 34), "header": (225, 228, 235), "muted": (150, 156, 168), "line": (255, 255, 255, 40)},
    "light": {"bg": (244, 241, 236), "header": (40, 34, 28), "muted": (120, 112, 102), "line": (0, 0, 0, 40)},
    "eink":  {"bg": SPECTRA6_COLORS["white"], "header": SPECTRA6_COLORS["black"],
              "muted": SPECTRA6_COLORS["blue"], "line": (*SPECTRA6_COLORS["black"], 255)},
}


@dataclass
class TileResult:
    module_id: str
    state_key: str
    image: Image.Image | None      # None → Modul hat keine Kachel geliefert


def resolve_tile_layout(tiles: tuple[tuple[str, int], ...], available_ids: list[str]) -> list[tuple[str, int]]:
    """
    Wandelt (modul, prozent)-Angaben in eine Liste mit Prozenten um, die sich zu 100 addieren.
    Fehlt eine Angabe (0), teilen sich diese Module den Rest gleichmäßig. Unbekannte Module
    werden übersprungen. Ohne Konfiguration: alle verfügbaren Module gleich hoch.
    """
    chosen = [(mid, pct) for mid, pct in tiles if mid in available_ids]
    if not chosen:
        chosen = [(mid, 0) for mid in available_ids]
    if not chosen:
        return []
    fixed_total = sum(pct for _, pct in chosen if pct > 0)
    unsized = [mid for mid, pct in chosen if pct <= 0]
    if fixed_total > 100 or (fixed_total == 100 and unsized):
        # Überzeichnet: proportional stauchen, Rest für die unbenannten lassen
        factor = (100 - (10 * len(unsized))) / fixed_total if unsized else 100 / fixed_total
        chosen = [(mid, int(pct * factor) if pct > 0 else 0) for mid, pct in chosen]
        fixed_total = sum(pct for _, pct in chosen if pct > 0)
    remainder = max(0, 100 - fixed_total)
    share = remainder // len(unsized) if unsized else 0
    result = [(mid, pct if pct > 0 else share) for mid, pct in chosen]
    if not unsized and fixed_total < 100:
        # Nur feste Angaben unter 100: Rest der letzten Kachel geben
        mid, pct = result[-1]
        result[-1] = (mid, pct + remainder)
    return [(mid, pct) for mid, pct in result if pct > 0]


@dataclass
class PreparedDashboard:
    """Inhalte und State-Key eines Dashboards – gerendert wird erst, wenn sich der Key ändert."""
    prefix: str
    keys: list                     # [(module_id, State-Key der Kachel)]
    width: int
    height: int
    theme: str
    tiles: list                    # [(Modul, Inhalt oder None, Kachelhöhe)]
    env: dict
    skipped: set = field(default_factory=set)   # Module ohne Kachelbild (erst nach dem Rendern bekannt)

    @property
    def state_key(self) -> str:
        return self.prefix + "|".join(f"{mid}={key}" for mid, key in self.keys if mid not in self.skipped)


def prepare_dashboard(env: dict[str, str], cfg: RuntimeConfig, modules: list[InkwallModule],
                      width: int | None = None, height: int | None = None) -> PreparedDashboard | None:
    """
    Holt die Inhalte aller Kacheln und bildet den State-Key, ohne zu rendern.
    Der Worker fragt das bei jedem Poll; gerendert (Kacheln samt Fotos und
    Textsatz) wird nur, wenn sich der Key ändert oder ein Modul neu will.
    None, wenn keine einzige Kachel Inhalt hat.
    """
    width = width or cfg.render_width
    height = height or cfg.render_height

    layout = resolve_tile_layout(cfg.dashboard_tiles, [m.MODULE_ID for m in modules])
    by_id = {m.MODULE_ID: m for m in modules}
    if not layout:
        return None
    # Dringende Kacheln (z. B. Müllabfuhr morgen) nach oben, Höhen bleiben
    urgent_ids = set()
    for module_id, _ in layout:
        try:
            if by_id[module_id].is_urgent(env):
                urgent_ids.add(module_id)
        except Exception as exc:
            log.warning(f"is_urgent [{module_id}]: {exc}")
    if urgent_ids:
        layout = [t for t in layout if t[0] in urgent_ids] + [t for t in layout if t[0] not in urgent_ids]

    scale = max(0.5, min(width / 1200.0, 1.4))
    header_h = int(64 * scale)
    gap = int(8 * scale)
    usable_h = height - header_h - gap * (len(layout) - 1)

    tiles: list = []
    keys: list[tuple[str, str]] = []
    for module_id, pct in layout:
        mod = by_id[module_id]
        tile_h = max(1, int(usable_h * pct / 100))
        try:
            content = mod.fetch_content(env)
        except Exception as exc:
            log.error(f"dashboard fetch [{module_id}]: {exc}", exc_info=True)
            content = None
        key = "none"
        if content is not None:
            try:
                key = str(mod.get_state_key(content))
            except Exception as exc:
                log.error(f"dashboard state [{module_id}]: {exc}", exc_info=True)
                content = None
        tiles.append((mod, content, tile_h))
        keys.append((module_id, key))

    if all(content is None for _, content, _ in tiles):
        return None
    now = now_local()
    return PreparedDashboard(f"dashboard:{now.date().isoformat()}:", keys, width, height, cfg.display_theme, tiles, env)


def render_dashboard(prepared: PreparedDashboard) -> Image.Image | None:
    """Rendert ein vorbereitetes Dashboard. None, wenn keine Kachel ein Bild geliefert hat."""
    width, height, theme, env = prepared.width, prepared.height, prepared.theme, prepared.env
    pal = _PALETTES.get(theme, _PALETTES["dark"])
    flat = theme == "eink"
    scale = max(0.5, min(width / 1200.0, 1.4))
    header_h = int(64 * scale)
    gap = int(8 * scale)
    margin = int(24 * scale)

    images: list[Image.Image] = []
    for mod, content, tile_h in prepared.tiles:
        module_id = mod.MODULE_ID
        if content is None:
            images.append(_empty_tile(mod, width, tile_h, pal, flat, scale))
            continue
        try:
            image = mod.render_tile(env, content, width, tile_h)
        except Exception as exc:
            log.error(f"dashboard tile [{module_id}]: {exc}", exc_info=True)
            image = None
        if image is None:
            log.warning(f"Dashboard: Modul '{module_id}' liefert keine Kachel – übersprungen")
            prepared.skipped.add(module_id)
            continue
        if image.size != (width, tile_h):
            image = image.resize((width, tile_h))
        images.append(image.convert("RGB"))

    if not images:
        return None

    canvas = Image.new("RGB", (width, height), pal["bg"])
    draw = new_draw(canvas, "RGBA", flat=flat)

    # Kopfzeile: Datum links, Stand rechts
    now = now_local()
    font_date = load_font(int(30 * scale), True)
    font_stand = load_font(int(22 * scale), False)
    draw.text((margin, int(16 * scale)), format_date_long(now), font=font_date, fill=pal["header"])
    stand = f"Stand {now.strftime('%H:%M')}"
    sw = draw.textlength(stand, font=font_stand)
    draw.text((width - margin - sw, int(22 * scale)), stand, font=font_stand, fill=pal["muted"])

    y = header_h
    for index, image in enumerate(images):
        if index > 0:
            line_y = y - gap // 2
            draw.line([(margin, line_y), (width - margin, line_y)], fill=pal["line"], width=3 if flat else 1)
        canvas.paste(image, (0, y))
        y += image.height + gap
    return canvas


def compose_dashboard(env: dict[str, str], cfg: RuntimeConfig, modules: list[InkwallModule],
                      width: int | None = None, height: int | None = None) -> tuple[Image.Image, str] | None:
    """
    Rendert das Dashboard. modules: aktive Idle-Module in Reihenfolge der
    Kachel-Konfiguration. Rückgabe (Bild, State-Key) oder None, wenn keine
    einzige Kachel Inhalt hat.
    """
    prepared = prepare_dashboard(env, cfg, modules, width, height)
    if prepared is None:
        return None
    image = render_dashboard(prepared)
    if image is None:
        return None
    return image, prepared.state_key


def _empty_tile(mod: InkwallModule, width: int, height: int, pal: dict, flat: bool, scale: float) -> Image.Image:
    img = Image.new("RGB", (width, height), pal["bg"])
    draw = new_draw(img, flat=flat)
    draw.text((int(24 * scale), int(16 * scale)), mod.MODULE_NAME, font=load_font(int(26 * scale), True), fill=pal["header"])
    draw.text((int(24 * scale), int(56 * scale)), "Keine Daten", font=load_font(int(24 * scale), False), fill=pal["muted"])
    return img
