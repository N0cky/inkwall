"""
NINA-Renderer: eine Warnung groß – farbiger Kopf mit Schwere und Quelle,
Überschrift, Gebiet und Zeit, Beschreibung, „Was tun?“ –, weitere Warnungen
als Liste darunter. Vollbild und kompakte Kachel, drei Themes.
"""

from __future__ import annotations

from datetime import datetime

from PIL import Image

from app.config import format_date_long, now_local
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from app.text_rendering import ellipsize, new_draw, page_scale, tile_scale, wrap_text

Color = tuple[int, int, int, int]


def _c(name: str) -> Color:
    return (*SPECTRA6_COLORS[name], 255)


_PALETTES: dict[str, dict] = {
    "dark": {
        "flat": False, "bg": (26, 28, 34, 255), "title": (255, 255, 255, 255), "text": (232, 234, 240, 255),
        "muted": (160, 166, 178, 255), "line": (255, 255, 255, 40), "box": (40, 43, 52, 255),
        "severity": {"extreme": ((150, 30, 150, 255), (255, 255, 255, 255)), "severe": ((200, 40, 36, 255), (255, 255, 255, 255)),
                     "moderate": ((230, 140, 20, 255), (20, 16, 10, 255)), "minor": ((228, 196, 40, 255), (20, 16, 10, 255))},
    },
    "light": {
        "flat": False, "bg": (244, 241, 236, 255), "title": (24, 20, 16, 255), "text": (36, 30, 24, 255),
        "muted": (120, 112, 102, 255), "line": (0, 0, 0, 36), "box": (232, 226, 216, 255),
        "severity": {"extreme": ((130, 20, 130, 255), (255, 255, 255, 255)), "severe": ((190, 32, 28, 255), (255, 255, 255, 255)),
                     "moderate": ((214, 120, 10, 255), (255, 255, 255, 255)), "minor": ((222, 186, 30, 255), (24, 20, 16, 255))},
    },
    "eink": {
        "flat": True, "bg": _c("white"), "title": _c("black"), "text": _c("black"), "muted": _c("blue"),
        "line": _c("black"), "box": _c("white"),
        "severity": {"extreme": (_c("red"), _c("white")), "severe": (_c("red"), _c("white")),
                     "moderate": (_c("yellow"), _c("black")), "minor": (_c("yellow"), _c("black"))},
    },
}


def get_palette(theme: str) -> dict:
    return _PALETTES.get(theme, _PALETTES["dark"])


def _when(value: str) -> datetime | None:
    try:
        return datetime.fromisoformat(value) if value else None
    except ValueError:
        return None


def _meta_line(w: dict) -> str:
    parts = []
    if w.get("area"):
        parts.append(w["area"])
    sent = _when(w.get("sent", ""))
    if sent:
        today = now_local().date()
        parts.append(f"seit {sent:%H:%M}" if sent.date() == today else f"seit {sent:%d.%m. %H:%M}")
    expires = _when(w.get("expires", ""))
    if expires:
        parts.append(f"bis {expires:%d.%m. %H:%M}")
    return " · ".join(parts)


def _paragraph_lines(draw, text: str, font, width: int, max_lines: int) -> list[str]:
    """Absätze getrennt umbrechen; insgesamt höchstens max_lines Zeilen, die letzte mit „…“."""
    lines: list[str] = []
    for paragraph in (text or "").split("\n"):
        if len(lines) >= max_lines:
            break
        lines.extend(wrap_text(draw, paragraph, font, width, max_lines=max_lines - len(lines)))
    if len(lines) > max_lines:
        lines = lines[:max_lines]
    return lines


def render_nina_module(services: ModuleRenderServices, content: object, compact: bool = False) -> Image.Image:
    data = content if isinstance(content, dict) else {}
    warnings = data.get("warnings") or []
    rw, rh = services.render_width, services.render_height
    pal = get_palette(services.display_theme)
    flat = pal["flat"]
    load_font = services.load_font
    scale = tile_scale(rw) if compact else page_scale(rw, rh)

    def px(v: float) -> int:
        return max(1, int(v * scale))

    img = Image.new("RGBA", (rw, rh), pal["bg"])
    draw = new_draw(img, "RGBA", flat)
    margin = max(24, rw // 30) if compact else max(40, rw // 20)
    width = rw - 2 * margin
    if not warnings:
        draw.text((margin, margin), "Keine Warnungen", font=load_font(px(40), True), fill=pal["title"])
        return img.convert("RGB")

    first = warnings[0]
    band_bg, band_fg = pal["severity"].get(first.get("severity", "minor"), pal["severity"]["minor"])

    # ── Kopf: farbiges Band mit Schwere und Quelle ──────────────────────────
    band_h = px(70) if compact else px(120)
    draw.rectangle([(0, 0), (rw, band_h)], fill=band_bg)
    label = f"{first.get('severity_label', 'Warnung')}  ·  {first.get('provider_label', 'NINA')}"
    font_band = load_font(px(30) if compact else px(40), True)
    draw.text((margin, band_h // 2), ellipsize(draw, label.upper() if not compact else label, font_band, width), font=font_band,
              fill=band_fg, anchor="lm")
    if not compact:
        font_region = load_font(px(26), False)
        region = data.get("region") or ""
        if region:
            rtext = ellipsize(draw, region, font_region, width // 3)
            draw.text((rw - margin, band_h // 2), rtext, font=font_region, fill=band_fg, anchor="rm")
    y = band_h + (px(18) if compact else px(34))

    # ── Überschrift und Gebiet/Zeit ─────────────────────────────────────────
    font_head = load_font(px(38) if compact else px(58), True)
    head_lines = wrap_text(draw, first.get("headline", ""), font_head, width, max_lines=2 if compact else 4)
    line_h = int(font_head.size * 1.18)
    for line in head_lines:
        draw.text((margin, y), line, font=font_head, fill=pal["title"])
        y += line_h
    font_meta = load_font(px(24) if compact else px(28), False)
    meta = _meta_line(first)
    if meta:
        y += px(6)
        draw.text((margin, y), ellipsize(draw, meta, font_meta, width), font=font_meta, fill=pal["muted"])
        y += int(font_meta.size * 1.3)

    others = warnings[1:]
    if compact:
        if others:
            more = f"+ {len(others)} weitere Warnung{'en' if len(others) > 1 else ''}"
            draw.text((margin, rh - margin // 2), more, font=font_meta, fill=pal["muted"], anchor="ls")
        return img.convert("RGB")

    # Platz unten für weitere Warnungen und die Fußzeile freihalten
    font_small = load_font(px(24), False)
    footer_h = px(56)
    list_h = min(len(others), 4) * px(52) + (px(56) if others else 0)
    bottom = rh - margin // 2 - footer_h - list_h
    y += px(20)
    draw.line([(margin, y), (rw - margin, y)], fill=pal["line"], width=2 if flat else 1)
    y += px(24)

    # ── Beschreibung ─────────────────────────────────────────────────────────
    font_text = load_font(px(32), False)
    text_lh = int(font_text.size * 1.32)
    instruction = first.get("instruction", "")
    # „Was tun?“ bekommt Vorrang: bis zu 6 Zeilen reservieren, wenn es Hinweise gibt
    font_instr = load_font(px(30), False)
    instr_lh = int(font_instr.size * 1.32)
    instr_lines_max = 6 if instruction else 0
    instr_block = (px(56) + instr_lines_max * instr_lh + px(30)) if instruction else 0
    desc_lines_max = max(0, (bottom - y - instr_block) // text_lh)
    description = first.get("description", "")
    for line in _paragraph_lines(draw, description, font_text, width, desc_lines_max):
        draw.text((margin, y), line, font=font_text, fill=pal["text"])
        y += text_lh

    # ── Was tun? ─────────────────────────────────────────────────────────────
    if instruction and bottom - y > px(56) + instr_lh:
        y += px(20)
        room = max(1, min(instr_lines_max, (bottom - y - px(56) - px(20)) // instr_lh))
        lines = _paragraph_lines(draw, instruction, font_instr, width - px(40), room)
        box_h = px(56) + len(lines) * instr_lh + px(20)
        draw.rectangle([(margin, y), (rw - margin, y + box_h)], fill=pal["box"], outline=band_bg, width=px(4))
        draw.text((margin + px(20), y + px(14)), "Was tun?", font=load_font(px(30), True), fill=pal["title"])
        ty = y + px(56)
        for line in lines:
            draw.text((margin + px(20), ty), line, font=font_instr, fill=pal["text"])
            ty += instr_lh
        y += box_h

    # ── Weitere Warnungen ────────────────────────────────────────────────────
    if others:
        y = rh - margin // 2 - footer_h - list_h + px(10)
        draw.text((margin, y), "Weitere Warnungen", font=load_font(px(28), True), fill=pal["title"])
        y += px(46)
        font_item = load_font(px(28), False)
        for w in others[:4]:
            bg, _ = pal["severity"].get(w.get("severity", "minor"), pal["severity"]["minor"])
            draw.rectangle([(margin, y + px(6)), (margin + px(14), y + px(38))], fill=bg)
            draw.text((margin + px(30), y + px(4)), ellipsize(draw, w.get("headline", ""), font_item, width - px(30)),
                      font=font_item, fill=pal["text"])
            y += px(52)

    # ── Fußzeile: Absender, Quelle, Datum ────────────────────────────────────
    footer_y = rh - margin // 2 - px(20)
    sender = first.get("sender") or ""
    left = f"{sender} · NINA / warnung.bund.de" if sender else "NINA / warnung.bund.de"
    right = format_date_long()
    draw.text((rw - margin, footer_y), right, font=font_small, fill=pal["muted"], anchor="rs")
    draw.text((margin, footer_y), ellipsize(draw, left, font_small, width - int(draw.textlength(right, font=font_small)) - px(30)),
              font=font_small, fill=pal["muted"], anchor="ls")
    return img.convert("RGB")
