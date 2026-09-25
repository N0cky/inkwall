"""
Abfahrten-Renderer: Anzeigetafel wie am Bahnsteig. Je Haltestelle ein
Abschnitt mit Zeilen aus Uhrzeit (+Verspätung), Linien-Kachel, Ziel,
Gleis und „in X min“. Drei Themes, Vollbild und kompakte Kachel.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image

from app.config import format_date_long
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from app.text_rendering import ellipsize, fit_wrapped_text, new_draw

Color = tuple[int, int, int, int]


def _c(name: str) -> Color:
    return (*SPECTRA6_COLORS[name], 255)


_PALETTES: dict[str, dict] = {
    "dark": {
        "flat": False,
        "bg":        (26, 28, 34, 255),
        "header":    (225, 228, 235, 255),
        "title":     (255, 255, 255, 255),
        "muted":     (160, 166, 178, 255),
        "text":      (240, 242, 246, 255),
        "time":      (235, 238, 245, 255),
        "delay":     (245, 120, 110, 255),
        "ontime":    (110, 200, 140, 255),
        "cancel":    (245, 100, 90, 255),
        "row_line":  (255, 255, 255, 26),
        "badge_text": (255, 255, 255, 255),
        "products": {"suburban": (46, 139, 87), "subway": (30, 100, 200), "tram": (200, 40, 40), "bus": (140, 60, 170),
                     "regional": (90, 110, 140), "regionalExpress": (90, 110, 140), "national": (60, 60, 70),
                     "nationalExpress": (60, 60, 70), "ferry": (30, 120, 170), "": (90, 95, 105)},
    },
    "light": {
        "flat": False,
        "bg":        (244, 241, 236, 255),
        "header":    (60, 54, 46, 255),
        "title":     (24, 20, 16, 255),
        "muted":     (120, 112, 102, 255),
        "text":      (30, 26, 22, 255),
        "time":      (24, 20, 16, 255),
        "delay":     (198, 50, 46, 255),
        "ontime":    (48, 140, 76, 255),
        "cancel":    (198, 50, 46, 255),
        "row_line":  (0, 0, 0, 24),
        "badge_text": (255, 255, 255, 255),
        "products": {"suburban": (0, 128, 64), "subway": (0, 90, 190), "tram": (190, 30, 30), "bus": (130, 50, 160),
                     "regional": (80, 100, 130), "regionalExpress": (80, 100, 130), "national": (40, 40, 50),
                     "nationalExpress": (40, 40, 50), "ferry": (20, 110, 160), "": (100, 100, 110)},
    },
    "eink": {
        "flat": True,
        "bg":        _c("white"),
        "header":    _c("black"),
        "title":     _c("black"),
        "muted":     _c("blue"),
        "text":      _c("black"),
        "time":      _c("black"),
        "delay":     _c("red"),
        "ontime":    _c("green"),
        "cancel":    _c("red"),
        "row_line":  _c("black"),
        "badge_text": _c("white"),
        "products": {"suburban": SPECTRA6_COLORS["green"], "subway": SPECTRA6_COLORS["blue"], "tram": SPECTRA6_COLORS["red"],
                     "bus": SPECTRA6_COLORS["black"], "regional": SPECTRA6_COLORS["black"], "regionalExpress": SPECTRA6_COLORS["black"],
                     "national": SPECTRA6_COLORS["black"], "nationalExpress": SPECTRA6_COLORS["black"],
                     "ferry": SPECTRA6_COLORS["blue"], "": SPECTRA6_COLORS["black"]},
    },
}


def get_palette(theme: str) -> dict:
    return _PALETTES.get(theme, _PALETTES["dark"])


def _stale_text(data: dict) -> str:
    raw = str(data.get("stale_since", "") or "")
    if not raw:
        return ""
    try:
        stale = datetime.fromisoformat(raw)
    except ValueError:
        return ""
    return f"Stand {stale:%H:%M}"


def _minutes_text(row: dict) -> str:
    if row.get("cancelled"):
        return "fällt aus"
    m = int(row.get("in_minutes", 0))
    if m <= 0:
        return "jetzt"
    if m >= 100:
        return f"{m // 60} h"
    return f"{m} min"


def render_departures_module(services: ModuleRenderServices, content: object, compact: bool = False) -> Image.Image:
    """compact=True: Dashboard-Kachel – Titelzeile statt Datum + großem Titel, Abschnitte zusammengeschoben."""
    data = content if isinstance(content, dict) else {}
    rw, rh = services.render_width, services.render_height
    pal = get_palette(services.display_theme)
    flat = pal["flat"]
    load_font = services.load_font

    img = Image.new("RGBA", (rw, rh), pal["bg"])
    draw = new_draw(img, "RGBA")
    margin = max(40, rw // 20) if not compact else max(24, rw // 30)
    scale = max(0.5, min(rw / 1200.0, 1.4)) if compact else max(0.35, min(1.0, rw / 1200.0, rh / 1200.0))

    def px(v: float) -> int:
        return max(1, int(v * scale))

    stale_text = _stale_text(data)
    font_small = load_font(px(22), False)

    # ── Kopfzeile ────────────────────────────────────────────────────────────
    if compact:
        draw.text((margin, px(14)), "Abfahrten", font=load_font(px(26), True), fill=pal["title"])
        if stale_text:
            draw.text((rw - margin - draw.textlength(stale_text, font=font_small), px(18)), stale_text, font=font_small, fill=pal["muted"])
        y = px(56)
    else:
        font_date = load_font(px(32), False)
        font_title = load_font(px(60), True)
        draw.text((margin, px(46)), format_date_long(), font=font_date, fill=pal["header"])
        draw.text((margin, px(86)), "Abfahrten", font=font_title, fill=pal["title"])
        right_text = stale_text or f"Stand {datetime.fromisoformat(data['now']):%H:%M}" if data.get("now") else stale_text
        if right_text:
            f = load_font(px(24), False)
            draw.text((rw - margin - draw.textlength(right_text, font=f), px(52)), right_text, font=f, fill=pal["muted"])
        y = px(170)

    sections = data.get("sections") or []
    font_section = load_font(px(30), True)
    font_time = load_font(px(30), True)
    font_delay = load_font(px(22), True)
    font_badge = load_font(px(24), True)
    font_dir = load_font(px(30), False)
    font_platform = load_font(px(22), False)
    font_minutes = load_font(px(30), True)
    row_h = px(64)
    time_w = px(150)
    badge_w = px(120)
    minutes_w = px(120)
    platform_w = px(90)
    bottom_limit = rh - margin // 2

    for s_index, section in enumerate(sections):
        if y > bottom_limit - row_h:
            break
        # Abschnittsüberschrift (bei einer Haltestelle in der Kachel weglassen)
        if not (compact and len(sections) == 1):
            heading = section.get("label") or section.get("name") or "Haltestelle"
            if section.get("name") and section.get("label") and section["name"] != section["label"]:
                heading = f"{section['label']}  ·  {section['name']}"
            draw.text((margin, y), ellipsize(draw, heading, font_section, rw - 2 * margin), font=font_section, fill=pal["title"])
            if section.get("stale_since") and not stale_text:
                pass
            y += px(42)
            draw.line([(margin, y), (rw - margin, y)], fill=pal["row_line"], width=2 if flat else 1)
            y += px(10)

        rows = section.get("rows") or []
        if not rows:
            msg = section.get("error") or "Keine Abfahrten im Zeitraum"
            draw.text((margin, y + px(8)), msg, font=font_dir, fill=pal["muted"])
            y += row_h
        for row in rows:
            if y > bottom_limit - row_h:
                break
            mid = y + row_h // 2
            cancelled = bool(row.get("cancelled"))
            # Uhrzeit + Verspätung
            when = row.get("when")
            time_text = when.strftime("%H:%M") if hasattr(when, "strftime") else "--:--"
            draw.text((margin, mid - px(18)), time_text, font=font_time, fill=pal["cancel"] if cancelled else pal["time"])
            delay = row.get("delay_min")
            if not cancelled and isinstance(delay, int) and delay != 0:
                sign = "+" if delay > 0 else "−"
                dtext = f"{sign}{abs(delay)}"
                draw.text((margin + px(90), mid - px(14)), dtext, font=font_delay, fill=pal["delay"] if delay > 0 else pal["ontime"])
            # Linien-Kachel
            bx = margin + time_w
            color = pal["products"].get(row.get("product", ""), pal["products"][""])
            badge_text = str(row.get("line", "?"))[:6]
            bw = max(px(56), int(draw.textlength(badge_text, font=font_badge)) + px(20))
            draw.rounded_rectangle([(bx, mid - px(18)), (bx + bw, mid + px(18))], radius=0 if flat else px(8), fill=(*color, 255))
            draw.text((bx + (bw - draw.textlength(badge_text, font=font_badge)) / 2, mid - px(14)), badge_text, font=font_badge, fill=pal["badge_text"])
            # Ziel
            dir_x = bx + badge_w
            dir_w = rw - margin - dir_x - minutes_w - platform_w
            direction = str(row.get("direction") or "")
            f, lines, lh, sp, th = fit_wrapped_text(draw, direction, dir_w, px(36), px(30), px(20), load_font, is_bold=False, max_lines=1)
            draw.text((dir_x, mid - lh // 2), lines[0] if lines else "", font=f, fill=pal["muted"] if cancelled else pal["text"])
            if cancelled and lines and not flat:
                tw = draw.textlength(lines[0], font=f)
                draw.line([(dir_x, mid), (dir_x + tw, mid)], fill=pal["cancel"], width=2)
            # Gleis
            platform = str(row.get("platform") or "")
            if platform:
                ptext = f"Gl. {platform}" if platform[:1].isdigit() else platform
                changed = row.get("planned_platform") and row.get("planned_platform") != platform
                draw.text((rw - margin - minutes_w - platform_w + px(6), mid - px(12)), ptext[:8], font=font_platform,
                          fill=pal["delay"] if changed else pal["muted"])
            # Minuten
            mtext = _minutes_text(row)
            tw = draw.textlength(mtext, font=font_minutes)
            draw.text((rw - margin - tw, mid - px(18)), mtext, font=font_minutes, fill=pal["cancel"] if cancelled else pal["time"])
            y += row_h
            draw.line([(margin, y), (rw - margin, y)], fill=pal["row_line"], width=1)
        y += px(20)

    if not sections:
        draw.text((margin, y), "Keine Haltestelle eingerichtet", font=font_dir, fill=pal["muted"])

    return img.convert("RGB")
