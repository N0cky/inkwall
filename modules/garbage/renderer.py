"""
Müllabfuhr-Renderer: nächster Abfuhrtag groß mit Tonne, darunter die
Termine der nächsten Tage als Wochenstreifen oder Liste. Drei Themes;
E-Ink flach in Spectra-Farben.

Besonderheiten:
- Ist die Abfuhr heute oder (abends) morgen, bekommt der Hero ein farbiges
  Band "Morgen rausstellen" in der Tonnenfarbe.
- Gleiche Tonne an mehreren Adressen ist eine Zeile; alternativ eine Spalte
  je Adresse (layout="columns").
- Symbole je Tonnenart: Tonne, Sack, Papierstapel, Sperrmüll-Sessel, Tannenbaum.
- Alte Daten zeigen "Stand vom …", fehlende Jahreskalender einen Hinweis.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta

from PIL import Image, ImageDraw

from app.config import format_date_long, format_weekday_short
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from app.text_rendering import draw_lines, fit_wrapped_text, new_draw

Color = tuple[int, int, int, int]

MAX_STRIP_DAYS = 21


def _c(name: str) -> Color:
    return (*SPECTRA6_COLORS[name], 255)


_PALETTES: dict[str, dict] = {
    "dark": {
        "flat": False,
        "bg":            (26, 28, 34, 255),
        "header":        (225, 228, 235, 255),
        "title":         (255, 255, 255, 255),
        "muted":         (160, 166, 178, 255),
        "text":          (240, 242, 246, 255),
        "accent":        (120, 180, 255, 255),
        "card_fill":     (40, 44, 54, 255),
        "card_outline":  (255, 255, 255, 40),
        "row_line":      (255, 255, 255, 28),
        "bin_outline":   (255, 255, 255, 90),
        "today_fill":    (120, 180, 255, 255),
        "today_text":    (20, 24, 32, 255),
        "bins": {
            "black":  (78, 80, 88),
            "green":  (58, 150, 84),
            "yellow": (232, 196, 48),
            "blue":   (70, 130, 220),
            "red":    (208, 62, 58),
        },
    },
    "light": {
        "flat": False,
        "bg":            (244, 241, 236, 255),
        "header":        (60, 54, 46, 255),
        "title":         (24, 20, 16, 255),
        "muted":         (120, 112, 102, 255),
        "text":          (30, 26, 22, 255),
        "accent":        (30, 100, 170, 255),
        "card_fill":     (255, 254, 251, 255),
        "card_outline":  (150, 142, 130, 90),
        "row_line":      (0, 0, 0, 24),
        "bin_outline":   (0, 0, 0, 70),
        "today_fill":    (30, 100, 170, 255),
        "today_text":    (255, 255, 255, 255),
        "bins": {
            "black":  (55, 55, 60),
            "green":  (48, 140, 76),
            "yellow": (236, 190, 30),
            "blue":   (40, 105, 200),
            "red":    (198, 50, 46),
        },
    },
    "eink": {
        "flat": True,
        "bg":            _c("white"),
        "header":        _c("black"),
        "title":         _c("black"),
        "muted":         _c("blue"),
        "text":          _c("black"),
        "accent":        _c("blue"),
        "card_fill":     _c("white"),
        "card_outline":  _c("black"),
        "row_line":      _c("black"),
        "bin_outline":   _c("black"),
        "today_fill":    _c("black"),
        "today_text":    _c("white"),
        "bins": {
            "black":  SPECTRA6_COLORS["black"],
            "green":  SPECTRA6_COLORS["green"],
            "yellow": SPECTRA6_COLORS["yellow"],
            "blue":   SPECTRA6_COLORS["blue"],
            "red":    SPECTRA6_COLORS["red"],
        },
    },
}


def get_palette(theme: str) -> dict:
    return _PALETTES.get(theme, _PALETTES["dark"])


def _text_on(color: tuple, pal: dict) -> Color:
    """Schriftfarbe auf einer Tonnenfarbe: Schwarz auf Gelb, sonst Weiß."""
    r, g, b = color[:3]
    return (*SPECTRA6_COLORS["black"], 255) if (r * 299 + g * 587 + b * 114) / 1000 > 150 else (255, 255, 255, 255)


# ---------------------------------------------------------------------------
# Symbole je Tonnenart
# ---------------------------------------------------------------------------

def draw_bin(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict) -> None:
    """Mülltonne: Deckel, Korpus mit Griffleiste, zwei Räder. Größe frei skalierbar."""
    flat = pal["flat"]
    outline = pal["bin_outline"]
    ow = max(2, w // 22) if flat else max(1, w // 40)
    fill = (*color[:3], 255)
    # E-Ink: Spectra-Grün wirkt auf dem Panel fast schwarz. Damit Restmüll und Bio
    # unterscheidbar bleiben, ist die schwarze Tonne hohl (weißer Korpus, schwarzer Deckel).
    hollow = flat and tuple(color[:3]) == tuple(SPECTRA6_COLORS["black"])
    body_fill = pal["card_fill"] if hollow else fill

    lid_h = max(4, int(h * 0.11))
    body_top = y + lid_h
    body_left = x + int(w * 0.08)
    body_right = x + w - int(w * 0.08)
    wheel_r = max(3, int(w * 0.09))
    body_bottom = y + h - wheel_r

    draw.rounded_rectangle([(x, y), (x + w, y + lid_h)], radius=0 if flat else lid_h // 2,
                           fill=fill, outline=outline, width=ow)
    handle_w = int(w * 0.28)
    draw.rectangle([(x + (w - handle_w) // 2, y - max(2, lid_h // 3)), (x + (w + handle_w) // 2, y)],
                   fill=fill, outline=outline, width=ow)
    draw.rounded_rectangle([(body_left, body_top), (body_right, body_bottom)],
                           radius=0 if flat else max(4, w // 12), fill=body_fill, outline=outline, width=ow)
    groove = (pal["text"] if hollow else pal["card_fill"]) if flat else (*pal["bg"][:3], 110)
    for i in (1, 2):
        gx = body_left + int((body_right - body_left) * i / 3)
        draw.line([(gx, body_top + int(h * 0.12)), (gx, body_bottom - int(h * 0.08))], fill=groove, width=max(2, ow))
    for wx in (body_left + wheel_r + ow, body_right - wheel_r - ow):
        draw.ellipse([(wx - wheel_r, body_bottom - wheel_r // 2), (wx + wheel_r, body_bottom + wheel_r + wheel_r // 2)],
                     fill=pal["text"] if flat else (*color[:3], 255), outline=outline, width=ow)


def draw_sack(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict) -> None:
    """Gelber Sack: zugebundener Beutel mit zwei Zipfeln."""
    flat = pal["flat"]
    outline = pal["bin_outline"]
    ow = max(2, w // 22) if flat else max(1, w // 40)
    fill = (*color[:3], 255)
    neck_y = y + int(h * 0.3)
    draw.ellipse([(x, neck_y - int(h * 0.05)), (x + w, y + h)], fill=fill, outline=outline, width=ow)
    # Hals und Zipfel
    cx = x + w // 2
    draw.polygon([(cx - int(w * 0.16), neck_y), (cx + int(w * 0.16), neck_y),
                  (cx + int(w * 0.10), y + int(h * 0.12)), (cx - int(w * 0.10), y + int(h * 0.12))],
                 fill=fill, outline=outline, width=ow)
    draw.polygon([(cx - int(w * 0.10), y + int(h * 0.14)), (cx - int(w * 0.30), y), (cx - int(w * 0.02), y + int(h * 0.08))],
                 fill=fill, outline=outline, width=ow)
    draw.polygon([(cx + int(w * 0.10), y + int(h * 0.14)), (cx + int(w * 0.30), y), (cx + int(w * 0.02), y + int(h * 0.08))],
                 fill=fill, outline=outline, width=ow)
    # Knoten
    draw.rectangle([(cx - int(w * 0.18), neck_y - int(h * 0.03)), (cx + int(w * 0.18), neck_y + int(h * 0.05))],
                   fill=pal["text"] if flat else outline, outline=outline, width=ow)


def draw_paper(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict) -> None:
    """Papierstapel: drei gebündelte Lagen mit Textzeilen."""
    flat = pal["flat"]
    outline = pal["bin_outline"]
    ow = max(2, w // 22) if flat else max(1, w // 40)
    fill = (*color[:3], 255)
    layer_h = int(h * 0.3)
    gap = int(h * 0.05)
    groove = pal["card_fill"] if flat else (*pal["bg"][:3], 140)
    for i in range(3):
        top = y + h - (i + 1) * layer_h - i * gap
        off = int(w * 0.06) if i % 2 else 0
        draw.rectangle([(x + off, top), (x + w - int(w * 0.06) + off, top + layer_h)],
                       fill=fill, outline=outline, width=ow)
        for k in (1, 2):
            ly = top + int(layer_h * k / 3)
            draw.line([(x + off + int(w * 0.15), ly), (x + w - int(w * 0.2) + off, ly)], fill=groove, width=max(2, ow))
    # Schnur
    draw.line([(x + w // 2, y + int(h * 0.02)), (x + w // 2, y + h)], fill=pal["text"] if flat else outline, width=max(2, ow))


def draw_bulky(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict) -> None:
    """Sperrmüll: Sessel mit Lehne, Sitz, Armlehnen und Füßen."""
    flat = pal["flat"]
    outline = pal["bin_outline"]
    ow = max(2, w // 22) if flat else max(1, w // 40)
    fill = (*color[:3], 255)
    r = 0 if flat else max(3, w // 12)
    draw.rounded_rectangle([(x + int(w * 0.14), y), (x + w - int(w * 0.14), y + int(h * 0.62))],
                           radius=r, fill=fill, outline=outline, width=ow)
    draw.rounded_rectangle([(x, y + int(h * 0.36)), (x + int(w * 0.2), y + int(h * 0.82))],
                           radius=r, fill=fill, outline=outline, width=ow)
    draw.rounded_rectangle([(x + w - int(w * 0.2), y + int(h * 0.36)), (x + w, y + int(h * 0.82))],
                           radius=r, fill=fill, outline=outline, width=ow)
    draw.rounded_rectangle([(x + int(w * 0.08), y + int(h * 0.55)), (x + w - int(w * 0.08), y + int(h * 0.82))],
                           radius=r, fill=fill, outline=outline, width=ow)
    leg_w = max(3, int(w * 0.1))
    for lx in (x + int(w * 0.14), x + w - int(w * 0.14) - leg_w):
        draw.rectangle([(lx, y + int(h * 0.82)), (lx + leg_w, y + h)], fill=pal["text"] if flat else outline)


def draw_tree(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict) -> None:
    """Tannenbaum: drei Stufen und Stamm."""
    flat = pal["flat"]
    outline = pal["bin_outline"]
    ow = max(2, w // 22) if flat else max(1, w // 40)
    fill = (*color[:3], 255)
    cx = x + w // 2
    trunk_h = int(h * 0.16)
    tiers = 3
    tier_h = (h - trunk_h) / (tiers * 0.72)
    for i in range(tiers):
        top = y + int(i * tier_h * 0.6)
        half = int(w * (0.28 + 0.22 * i))
        draw.polygon([(cx, top), (cx - half, top + int(tier_h)), (cx + half, top + int(tier_h))],
                     fill=fill, outline=outline, width=ow)
    draw.rectangle([(cx - int(w * 0.12), y + h - trunk_h), (cx + int(w * 0.12), y + h)],
                   fill=pal["text"] if flat else outline)


_ICON_DRAWERS = {"bin": draw_bin, "sack": draw_sack, "paper": draw_paper, "bulky": draw_bulky, "tree": draw_tree}


def draw_icon(draw: ImageDraw.ImageDraw, x: int, y: int, w: int, h: int, color: tuple, pal: dict, kind: str = "bin") -> None:
    _ICON_DRAWERS.get(kind, draw_bin)(draw, x, y, w, h, color, pal)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def _short_date(d: date) -> str:
    return f"{format_weekday_short(d)} {d.day:02d}.{d.month:02d}."


def _group_events(events: list[dict], with_labels: bool = True) -> list[dict]:
    """Gleiche Tonne an mehreren Adressen → eine Zeile mit allen Adressen."""
    groups: list[dict] = []
    for ev in events:
        for g in groups:
            if g["summary"] == ev.get("summary") and g["color"] == ev.get("color"):
                if with_labels and ev.get("label") and ev["label"] not in g["labels"]:
                    g["labels"].append(ev["label"])
                if ev.get("shifted_from") and not g["shifted_from"]:
                    g["shifted_from"] = ev["shifted_from"]
                    g["shifted_reason"] = ev.get("shifted_reason", "")
                break
        else:
            groups.append({
                "summary": ev.get("summary", ""),
                "color": ev.get("color", "black"),
                "icon": ev.get("icon", "bin"),
                "labels": [ev["label"]] if with_labels and ev.get("label") else [],
                "shifted_from": ev.get("shifted_from", ""),
                "shifted_reason": ev.get("shifted_reason", ""),
            })
    return groups


def _group_note(g: dict) -> str:
    parts = []
    if g["labels"]:
        parts.append(", ".join(g["labels"]))
    if g.get("shifted_from"):
        # „wegen Pfingstmontag verschoben“, sobald das Bundesland für Feiertage eingestellt ist
        reason = f"wegen {g['shifted_reason']} " if g.get("shifted_reason") else ""
        parts.append(f"{reason}verschoben, sonst {g['shifted_from']}")
    return " · ".join(parts)


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return text.rstrip() + "…"


def _parse_iso(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Hero: ein Abfuhrtag (Symbol links, Text rechts)
# ---------------------------------------------------------------------------

class _HeroText:
    """Fonts und Zeilenhöhen für den Hero-Text; hs < 1 verdichtet alles anteilig."""

    def __init__(self, px, load_font, small: bool, hs: float = 1.0):
        s = lambda v, m: max(m, int(px(v) * hs))  # noqa: E731
        self.font_rel   = load_font(s(54 if small else 78, 18), True)
        self.font_when  = load_font(s(26 if small else 32, 14), False)
        self.font_note  = load_font(s(22 if small else 26, 12), False)
        self.type_max   = s(40 if small else 60, 16)
        self.type_min   = s(24 if small else 28, 14)
        self.pad_top    = s(22 if small else 48, 4)
        self.rel_h      = s(60 if small else 96, 20)
        self.when_h     = s(38 if small else 56, 16)
        self.pad_bot    = s(18 if small else 40, 4)
        self.line_h     = s(50 if small else 66, 18)
        self.wrap_h     = s(28 if small else 34, 12)
        self.note_lift  = s(30 if small else 34, 10)
        self.extra_h    = s(34, 14)
        self.gap        = px(16)


def _measure_hero(draw, groups: list[dict], text_w: int, m: _HeroText, load_font, extra_lines: int) -> tuple[list[int], int]:
    probe_font = load_font(m.type_max, True)
    heights: list[int] = []
    for g in groups:
        note = _group_note(g)
        inline = not note or (draw.textlength(g["summary"], font=probe_font) + m.gap
                              + draw.textlength(note, font=m.font_note) <= text_w)
        heights.append(m.line_h + (0 if inline else m.wrap_h))
    needed = m.pad_top + m.rel_h + m.when_h + sum(heights) + m.pad_bot + extra_lines * m.extra_h
    return heights, needed


def _draw_hero_text(draw, x: int, y: int, text_w: int, day: dict, groups: list[dict], heights: list[int],
                    m: _HeroText, pal: dict, load_font, extra_lines: list[str]) -> None:
    flat = pal["flat"]
    ty = y
    draw.text((x, ty), day["relative"], font=m.font_rel, fill=pal["accent"] if flat else pal["title"])
    ty += m.rel_h
    draw.text((x, ty), format_date_long(day["date"]), font=m.font_when, fill=pal["muted"])
    ty += m.when_h
    for g, lh_total in zip(groups, heights):
        type_font, lines, lh, sp, th = fit_wrapped_text(
            draw, g["summary"], text_w, m.line_h * 2, m.type_max, m.type_min, load_font, is_bold=True, max_lines=1)
        draw_lines(draw, x, ty, lines, type_font, pal["text"], lh, sp)
        note = _group_note(g)
        if note:
            summary_w = draw.textlength(lines[0], font=type_font) if lines else 0
            if summary_w + m.gap + draw.textlength(note, font=m.font_note) <= text_w:
                draw.text((x + summary_w + m.gap, ty + max(0, th - m.note_lift)), note, font=m.font_note, fill=pal["muted"])
            else:
                draw.text((x, ty + th + 2), _ellipsize(draw, note, m.font_note, text_w), font=m.font_note, fill=pal["muted"])
        ty += lh_total
    for line in extra_lines:
        draw.text((x, ty), _ellipsize(draw, line, m.font_note, text_w), font=m.font_note, fill=pal["muted"])
        ty += m.extra_h


def _draw_hero_icons(draw, x: int, y_center: int, icon_max: int, groups: list[dict], pal: dict, px) -> int:
    """Großes Symbol der ersten Tonne, weitere klein daneben. Rückgabe: rechte Kante."""
    if not groups:
        return x
    big_h = icon_max
    big_w = int(big_h * 0.75)
    big_y = y_center - big_h // 2
    draw_icon(draw, x, big_y, big_w, big_h, pal["bins"][groups[0]["color"]], pal, groups[0]["icon"])
    ex = x + big_w + px(18)
    for g in groups[1:]:
        small_w = max(px(28), int(big_w * 0.36))
        small_h = int(small_w * 1.35)
        draw_icon(draw, ex, big_y + big_h - small_h, small_w, small_h, pal["bins"][g["color"]], pal, g["icon"])
        ex += small_w + px(12)
    return ex


def _draw_banner(draw, box: tuple, text: str, color: tuple, pal: dict, height: int, font) -> None:
    """Farbiges Band oben im Hero: 'Morgen rausstellen'."""
    x0, y0, x1, _ = box
    draw.rectangle([(x0, y0), (x1, y0 + height)], fill=(*color[:3], 255))
    tw = draw.textlength(text, font=font)
    draw.text((x0 + (x1 - x0 - tw) // 2, y0 + (height - font.size) // 2 - 2), text, font=font, fill=_text_on(color, pal))


# ---------------------------------------------------------------------------
# Wochenstreifen
# ---------------------------------------------------------------------------

def _strip_scale(compact: bool) -> float:
    return 0.8 if compact else 1.2


def _strip_height(px, rows: int, compact: bool) -> int:
    rs = _strip_scale(compact)
    return int(px(58 * rs)) + rows * int(px(46 * rs))


def _draw_strip(draw, x: int, y: int, w: int, days: list[dict], today: date, n_days: int, rows: int,
                pal: dict, px, load_font, compact: bool) -> int:
    """Tageszellen von heute bis heute+n_days mit Symbolen je Abfuhr. Rückgabe: Höhe."""
    rs = _strip_scale(compact)
    flat = pal["flat"]
    n = n_days + 1
    cell_w = w / n
    head_h = int(px(58 * rs))
    row_h = int(px(46 * rs))
    total_h = head_h + rows * row_h
    font_wd = load_font(int(px(18 * rs)), False)
    font_day = load_font(int(px(26 * rs)), True)
    by_date = {d["date"]: d for d in days}

    for i in range(n):
        d = today + timedelta(days=i)
        cx0 = int(x + i * cell_w)
        cx1 = int(x + (i + 1) * cell_w)
        is_today = i == 0
        weekend = d.weekday() >= 5
        if is_today:
            draw.rectangle([(cx0, y), (cx1 - 1, y + head_h - px(6))], fill=pal["today_fill"])
            col = pal["today_text"]
        else:
            col = pal["muted"] if weekend else pal["text"]
        wd = format_weekday_short(d)
        num = str(d.day)
        draw.text((cx0 + (cell_w - draw.textlength(wd, font=font_wd)) / 2, y + px(4 * rs)), wd, font=font_wd, fill=col)
        draw.text((cx0 + (cell_w - draw.textlength(num, font=font_day)) / 2, y + px(24 * rs)), num, font=font_day, fill=col)
        if i > 0:
            draw.line([(cx0, y + head_h), (cx0, y + total_h)], fill=pal["row_line"], width=1)
        day = by_date.get(d)
        if not day:
            continue
        groups = _group_events(day["events"], with_labels=False)[:rows]
        icon_w = min(int(cell_w) - px(10), int(px(30 * rs)))
        icon_h = int(icon_w * 1.3)
        for r, g in enumerate(groups):
            ix = int(cx0 + (cell_w - icon_w) / 2)
            iy = y + head_h + r * row_h + (row_h - icon_h) // 2
            draw_icon(draw, ix, iy, icon_w, icon_h, pal["bins"][g["color"]], pal, g["icon"])
    draw.line([(x, y + head_h), (x + w, y + head_h)], fill=pal["row_line"], width=2 if flat else 1)
    return total_h


# ---------------------------------------------------------------------------
# Layout
# ---------------------------------------------------------------------------

def render_garbage_module(services: ModuleRenderServices, content: object, compact: bool = False,
                          layout: str = "merged", upcoming: str = "strip") -> Image.Image:
    """
    compact=True: Dashboard-Kachel – kleine Titelzeile, kleinerer Hero.
    layout: "merged" (Adressen in einer Zeile) oder "columns" (Spalte je Adresse).
    upcoming: "strip" (Wochenstreifen) oder "list" (Zeilen).
    """
    data = content if isinstance(content, dict) else {}
    rw, rh = services.render_width, services.render_height
    pal = get_palette(services.display_theme)
    flat = pal["flat"]
    load_font = services.load_font

    img = Image.new("RGBA", (rw, rh), pal["bg"])
    draw = new_draw(img, "RGBA")

    margin = max(40, rw // 20) if not compact else max(24, rw // 30)
    scale = max(0.5, min(rw / 1200.0, 1.4)) if compact else min(rw / 1200.0, rh / 1600.0)

    def px(v: float) -> int:
        return max(1, int(v * scale))

    # ── Kopfzeile ────────────────────────────────────────────────────────────
    stale = _parse_iso(data.get("stale_since", ""))
    stale_text = f"Stand vom {stale.day:02d}.{stale.month:02d}. {stale:%H:%M}" if stale else ""
    if compact:
        draw.text((margin, px(14)), "Müllabfuhr", font=load_font(px(26), True), fill=pal["title"])
        if stale_text:
            f = load_font(px(20), False)
            draw.text((rw - margin - draw.textlength(stale_text, font=f), px(18)), stale_text, font=f, fill=pal["muted"])
        y = px(56)
    else:
        font_date  = load_font(px(32), False)
        font_title = load_font(px(60), True)
        draw.text((margin, px(46)), format_date_long(), font=font_date, fill=pal["header"])
        draw.text((margin, px(86)), "Müllabfuhr", font=font_title, fill=pal["title"])
        if stale_text:
            f = load_font(px(24), False)
            draw.text((rw - margin - draw.textlength(stale_text, font=f), px(52)), stale_text, font=f, fill=pal["muted"])
        y = px(180)

    next_day = data.get("next")
    days = data.get("days") or []
    today = date.fromisoformat(data["today"]) if data.get("today") else date.today()
    missing_years = data.get("missing_years") or []
    extra_lines: list[str] = []
    if next_day and data.get("next_outside_window"):
        extra_lines.append(f"Keine Abfuhr in den nächsten {data.get('days_ahead', 14)} Tagen")
    if missing_years:
        extra_lines.append(f"Kalender {', '.join(str(y) for y in missing_years)} noch nicht online")

    # ── Hero ─────────────────────────────────────────────────────────────────
    small = compact
    avail = rh - y - margin
    columns = data.get("by_label") or []
    use_columns = layout == "columns" and len(columns) >= 2 and rw >= 800
    urgent = bool(data.get("urgent")) and bool(next_day)
    banner_h = px(44 if small else 56) if urgent else 0
    font_banner = load_font(px(26 if small else 34), True)

    hero_needed = px(200) if small else px(420)
    hero_plans: list[dict] = []

    if next_day and use_columns:
        col_w = (rw - 2 * margin) // len(columns)
        icon_max = px(120 if small else 170)
        icon_w = int(icon_max * 0.75)
        text_w = col_w - px(24) - icon_w - px(24) - px(16)
        m = _HeroText(px, load_font, True, 0.9 if not small else 0.85)
        for col in columns:
            groups = _group_events(col["next"]["events"], with_labels=False)[:3]
            heights, needed = _measure_hero(draw, groups, text_w, m, load_font, 0)
            needed += px(34)  # Adresszeile
            hero_plans.append({"groups": groups, "heights": heights, "needed": needed, "m": m, "text_w": text_w,
                               "icon_max": icon_max, "day": col["next"], "title": col["label"]})
        hero_needed = max(p["needed"] for p in hero_plans) + banner_h
        if hero_needed > avail and avail > 0:
            hs = max(0.55, (avail - banner_h) / max(1, hero_needed - banner_h))
            m = _HeroText(px, load_font, True, hs * (0.9 if not small else 0.85))
            for p in hero_plans:
                p["m"] = m
                p["heights"], p["needed"] = _measure_hero(draw, p["groups"], text_w, m, load_font, 0)
                p["needed"] += px(34)
            hero_needed = max(p["needed"] for p in hero_plans) + banner_h
    elif next_day:
        groups = _group_events(next_day["events"])[:4]
        icon_max = px(200 if small else 280)
        icon_w = int(icon_max * 0.75)
        extra_n = max(0, len(groups) - 1)
        text_x = margin + px(48) + icon_w + (extra_n * (int(icon_w * 0.36) + px(12)) + px(6) if extra_n else 0) + px(40)
        text_w = rw - margin - text_x - px(32)
        m = _HeroText(px, load_font, small)
        heights, needed = _measure_hero(draw, groups, text_w, m, load_font, len(extra_lines))
        if needed + banner_h > avail > 0:
            hs = max(0.55, (avail - banner_h) / needed)
            m = _HeroText(px, load_font, small, hs)
            heights, needed = _measure_hero(draw, groups, text_w, m, load_font, len(extra_lines))
        hero_plans.append({"groups": groups, "heights": heights, "needed": needed, "m": m, "text_w": text_w,
                           "icon_max": icon_max, "day": next_day, "text_x": text_x})
        hero_needed = max(hero_needed, needed + banner_h)

    # Platz unter dem Hero: Streifen/Liste oder nur eine Zeile?
    upcoming_days = [d for d in days if not next_day or d["date"] != next_day["date"]]
    rs = 0.78 if compact else 1.0
    lp = lambda v: px(v * rs)  # noqa: E731
    strip_rows = max([len(_group_events(d["events"], with_labels=False)) for d in days] + [1])
    strip_rows = min(strip_rows, 3)
    n_strip = min(int(data.get("days_ahead", 14) or 14), MAX_STRIP_DAYS)
    gap_after = px(28 if small else 40)
    if upcoming == "strip" and days:
        below_need = gap_after + lp(36) + _strip_height(px, strip_rows, compact)
    else:
        below_need = gap_after + lp(48) + lp(84)
    hero_h = min(hero_needed, avail)
    if compact and not use_columns and next_day and hero_h + below_need > avail:
        # Kachel: Hero etwas verdichten, wenn dann der Streifen/die Liste noch hinpasst
        p = hero_plans[0]
        room = avail - below_need - banner_h
        hs_fit = room / max(1, p["needed"])
        if hs_fit >= 0.7:
            m = _HeroText(px, load_font, small, min(1.0, hs_fit))
            p["heights"], p["needed"] = _measure_hero(draw, p["groups"], p["text_w"], m, load_font, len(extra_lines))
            p["m"] = m
            hero_needed = max(px(150), p["needed"] + banner_h)
            hero_h = min(hero_needed, avail)
    if compact and hero_h + below_need > avail:
        # Streifen/Liste passt nicht: eine Textzeile darunter, Rest an den Hero
        oneliner = gap_after + lp(34) + px(6) if upcoming_days else 0
        hero_h = max(hero_h, min(avail, avail - oneliner))
    hero = (margin, y, rw - margin, y + hero_h)
    draw.rounded_rectangle(hero, radius=0 if flat else px(28), fill=pal["card_fill"],
                           outline=pal["card_outline"], width=4 if flat else 2)
    inner_top = y
    if urgent and hero_plans:
        first_color = pal["bins"][hero_plans[0]["groups"][0]["color"]] if hero_plans[0]["groups"] else pal["bins"]["black"]
        _draw_banner(draw, hero, data.get("reminder") or "Rausstellen", first_color, pal, banner_h, font_banner)
        inner_top = y + banner_h
    inner_h = hero_h - banner_h

    if next_day and use_columns:
        col_w = (rw - 2 * margin) // len(columns)
        font_col = load_font(px(24 if small else 28), True)
        tallest = max(p["needed"] for p in hero_plans)
        for i, p in enumerate(hero_plans):
            cx0 = margin + i * col_w
            if i > 0:
                draw.line([(cx0, inner_top + px(16)), (cx0, hero[3] - px(16))], fill=pal["row_line"], width=2 if flat else 1)
            # Alle Spalten auf derselben Höhe beginnen, sonst springt der Blick
            top = inner_top + max(0, (inner_h - tallest) // 2) + p["m"].pad_top
            draw.text((cx0 + px(24), top), _ellipsize(draw, p["title"], font_col, col_w - px(48)), font=font_col, fill=pal["muted"])
            top += px(34)
            icon_h = max(px(60), min(p["icon_max"], inner_h - px(60)))
            ix = cx0 + px(24)
            right = _draw_hero_icons(draw, ix, top + (p["needed"] - px(34) - p["m"].pad_top) // 2, icon_h, p["groups"], pal, px)
            _draw_hero_text(draw, right + px(16), top, p["text_w"], p["day"], p["groups"], p["heights"], p["m"], pal, load_font, [])
        if extra_lines:
            f = load_font(px(22), False)
            draw.text((margin + px(24), hero[3] - px(34)), " · ".join(extra_lines), font=f, fill=pal["muted"])
    elif next_day:
        p = hero_plans[0]
        icon_h = max(px(80), min(p["icon_max"], inner_h - px(50 if small else 60)))
        _draw_hero_icons(draw, hero[0] + px(48), inner_top + inner_h // 2 + px(6), icon_h, p["groups"], pal, px)
        top = inner_top + p["m"].pad_top + max(0, (inner_h - p["needed"]) // 2)
        _draw_hero_text(draw, p["text_x"], top, p["text_w"], p["day"], p["groups"], p["heights"], p["m"], pal, load_font, extra_lines)
    else:
        font_empty = load_font(px(34 if small else 40), True)
        draw.text((hero[0] + px(48), y + px(40 if small else 60)), "Keine Abfuhrtermine gefunden", font=font_empty, fill=pal["text"])
        if extra_lines:
            draw.text((hero[0] + px(48), y + px(40 if small else 60) + px(50)), " · ".join(extra_lines),
                      font=load_font(px(24), False), fill=pal["muted"])

    y = hero[3] + gap_after
    remaining = rh - margin - y

    # ── Darunter: Wochenstreifen oder Liste ──────────────────────────────────
    font_section  = load_font(lp(28), True)
    font_row_date = load_font(lp(30), True)
    font_row_rel  = load_font(lp(22), False)
    font_row_type = load_font(lp(28), False)
    row_h = lp(84)

    def _oneliner() -> None:
        if not upcoming_days or remaining < lp(34):
            return
        parts = [f"{_short_date(d['date'])} {' + '.join(g['summary'] for g in _group_events(d['events'], False))}"
                 for d in upcoming_days[:4]]
        draw.text((margin, y), _ellipsize(draw, "Danach: " + "  ·  ".join(parts), font_row_rel, rw - 2 * margin),
                  font=font_row_rel, fill=pal["muted"])

    heading = f"Nächste {data.get('days_ahead', 14)} Tage"
    if upcoming == "strip" and days:
        strip_h = _strip_height(px, strip_rows, compact)
        if remaining < lp(36) + strip_h:
            _oneliner()
            return img.convert("RGB")
        draw.text((margin, y), f"Nächste {n_strip} Tage", font=font_section, fill=pal["muted"])
        y += lp(36)
        y += _draw_strip(draw, margin, y, rw - 2 * margin, days, today, n_strip, strip_rows, pal, px, load_font, compact)
        y += gap_after
        remaining = rh - margin - y
        # Unter dem Streifen ist oft noch Platz – dann die Termine auch als Zeilen
        if not upcoming_days or remaining < lp(48) + 2 * row_h:
            return img.convert("RGB")
        heading = "Termine"

    if remaining < lp(48) + row_h or not next_day:
        _oneliner()
        return img.convert("RGB")

    draw.text((margin, y), heading, font=font_section, fill=pal["muted"])
    y += lp(48)

    if not upcoming_days and next_day and not data.get("next_outside_window"):
        draw.text((margin, y), "Danach keine weiteren Termine im Zeitraum.", font=font_row_type, fill=pal["muted"])
    chip_w, chip_h = lp(30), lp(40)
    text_left = margin + lp(230)
    text_right = rw - margin
    for day in upcoming_days:
        groups = _group_events(day["events"])[:4]
        labels = [g["summary"] + (f"  · {_group_note(g)}" if _group_note(g) else "") for g in groups]
        widths = [chip_w + lp(14) + int(draw.textlength(lb, font=font_row_type)) for lb in labels]
        # Passt alles in eine Zeile? Sonst jede Tonne in eine eigene Zeile (Zeile wird höher)
        one_line = text_left + sum(widths) + lp(34) * max(0, len(widths) - 1) <= text_right
        line_h = lp(40)
        this_row_h = row_h if one_line else max(row_h, lp(16) + len(groups) * line_h + lp(6))
        if y + this_row_h > rh - margin:
            break
        draw.line([(margin, y), (rw - margin, y)], fill=pal["row_line"], width=2 if flat else 1)
        draw.text((margin, y + lp(14)), _short_date(day["date"]), font=font_row_date, fill=pal["text"])
        draw.text((margin, y + lp(50)), day["relative"], font=font_row_rel, fill=pal["muted"])
        cx, cy = text_left, y + lp(22)
        for g, label, w in zip(groups, labels, widths):
            draw_icon(draw, cx, cy - lp(2), chip_w, chip_h, pal["bins"][g["color"]], pal, g["icon"])
            draw.text((cx + chip_w + lp(14), cy), _ellipsize(draw, label, font_row_type, text_right - cx - chip_w - lp(14)),
                      font=font_row_type, fill=pal["text"])
            if one_line:
                cx += w + lp(34)
            else:
                cy += line_h
        y += this_row_h

    return img.convert("RGB")
