"""
Generische Text-Utilities für alle Renderer: Zeichenfläche, Maßstab,
Zeilenumbruch, Kürzen mit Ellipse, Einpassen in eine Box mit fallender
Schriftgröße, mehrzeiliges Zeichnen.
"""

from __future__ import annotations

from typing import Callable

from PIL import Image, ImageDraw

FontLoader = Callable[[int, bool], object]

ELLIPSIS = "…"


def new_draw(img: Image.Image, mode: str | None = None, flat: bool | None = None) -> ImageDraw.ImageDraw:
    """
    ImageDraw für Renderer. Im flachen E-Ink-Theme ohne Kantenglättung der
    Schrift (fontmode "1"): graue Kantenpixel liegen zwischen den sechs
    Panelfarben, und das Dithering macht aus ihnen blaue und grüne Punkte
    rund um jeden Buchstaben. Das Panel kann ohnehin nur harte Kanten zeigen.
    flat=None: aus dem eingestellten Theme (in Vorschauen das Vorschau-Theme).
    """
    draw = ImageDraw.Draw(img, mode)
    if flat is None:
        from app.config import get_cfg, is_flat_theme
        flat = is_flat_theme(get_cfg().display_theme)
    if flat:
        draw.fontmode = "1"
    return draw


def page_scale(width: int, height: int) -> float:
    """Maßstab einer Vollbild-Seite, entworfen für 1200 × 1600: kleiner wird verkleinert, größer nicht vergrößert."""
    return max(0.35, min(1.0, width / 1200.0, height / 1200.0))


def tile_scale(width: int) -> float:
    """Maßstab einer Dashboard-Kachel nach ihrer Breite."""
    return max(0.5, min(width / 1200.0, 1.4))


def get_text_size(draw: ImageDraw.ImageDraw, text: str, font) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_width: int) -> str:
    """Text auf max_width kürzen, mit „…“ am Ende. Passt er, bleibt er, wie er ist."""
    text = text or ""
    if max_width <= 0:
        return ""
    if draw.textlength(text, font=font) <= max_width:
        return text
    # Binärsuche über die Länge statt Zeichen für Zeichen
    lo, hi = 0, len(text)
    while lo < hi:
        mid = (lo + hi + 1) // 2
        if draw.textlength(text[:mid].rstrip() + ELLIPSIS, font=font) <= max_width:
            lo = mid
        else:
            hi = mid - 1
    return (text[:lo].rstrip() + ELLIPSIS) if lo > 0 else ELLIPSIS


def wrap_text(draw: ImageDraw.ImageDraw, text: str, font, max_width: int, max_lines: int | None = None) -> list[str]:
    """
    Zeilenumbruch an Wortgrenzen. Jedes Wort wird einmal vermessen (nicht jede
    wachsende Zeile neu – das war bei langen Texten quadratisch). Ein einzelnes
    Wort, das breiter ist als die Zeile, wird mit „…“ gekürzt statt über den
    Rand zu laufen; bei max_lines endet die letzte Zeile mit „…“.
    """
    if not text:
        return []

    space_w = draw.textlength(" ", font=font)
    widths: dict[str, float] = {}

    def width_of(word: str) -> float:
        if word not in widths:
            widths[word] = draw.textlength(word, font=font)
        return widths[word]

    lines: list[str] = []
    truncated = False
    paragraphs = text.splitlines() or [text]

    for paragraph in paragraphs:
        words = paragraph.split()
        if not words:
            if lines and lines[-1] != "":
                lines.append("")
                if max_lines and len(lines) >= max_lines:
                    truncated = True
                    break
            continue

        current, current_w = words[0], width_of(words[0])
        for word in words[1:]:
            word_w = width_of(word)
            if current_w + space_w + word_w <= max_width:
                current = f"{current} {word}"
                current_w += space_w + word_w
            else:
                lines.append(current)
                current, current_w = word, word_w

                if max_lines and len(lines) >= max_lines:
                    truncated = True
                    break

        if truncated:
            break

        if not max_lines or len(lines) < max_lines:
            lines.append(current)
        else:
            truncated = True
            break

    if max_lines and len(lines) > max_lines:
        lines = lines[:max_lines]
        truncated = True

    # Zu breite Einzelwörter (lange Zielhalte, URLs) kürzen
    lines = [ellipsize(draw, line, font, max_width) if line and draw.textlength(line, font=font) > max_width else line
             for line in lines]

    if truncated and lines and not lines[-1].endswith(ELLIPSIS):
        # passt „Zeile…“ nicht, kürzt ellipsize die Zeile selbst und hängt „…“ an
        lines[-1] = ellipsize(draw, lines[-1] + ELLIPSIS, font, max_width)

    return lines


def fit_wrapped_text(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
    load_font: FontLoader,
    is_bold: bool = False,
    max_lines: int | None = None,
    line_spacing: float = 0.2,
):
    if max_width <= 0 or max_height <= 0:
        font = load_font(min_size, is_bold)
        return font, [], 0, 0, 0

    for size in range(start_size, min_size - 1, -2):
        font = load_font(size, is_bold)
        lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)

        if not lines:
            return font, [], 0, 0, 0

        _, line_h = get_text_size(draw, "Ag", font)
        spacing_px = int(line_h * line_spacing)
        total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing_px
        max_line_w = max(get_text_size(draw, line, font)[0] for line in lines)

        if max_line_w <= max_width and total_h <= max_height:
            return font, lines, line_h, spacing_px, total_h

    font = load_font(min_size, is_bold)
    lines = wrap_text(draw, text, font, max_width, max_lines=max_lines)
    _, line_h = get_text_size(draw, "Ag", font)
    spacing_px = int(line_h * line_spacing)
    total_h = len(lines) * line_h + max(0, len(lines) - 1) * spacing_px
    return font, lines, line_h, spacing_px, total_h


def fit_optional_text_block(
    draw: ImageDraw.ImageDraw,
    text: str,
    max_width: int,
    max_height: int,
    start_size: int,
    min_size: int,
    fallback_font,
    load_font: FontLoader,
    is_bold: bool = False,
    max_lines: int | None = None,
    line_spacing: float = 0.2,
):
    if not text:
        return fallback_font, [], 0, 0, 0

    return fit_wrapped_text(
        draw,
        text,
        max_width,
        max_height,
        start_size,
        min_size,
        load_font,
        is_bold=is_bold,
        max_lines=max_lines,
        line_spacing=line_spacing,
    )


def draw_lines(draw: ImageDraw.ImageDraw, x: int, y: int, lines: list[str], font, fill, line_h: int, spacing_px: int):
    current_y = y
    for line in lines:
        if line:
            draw.text((x, current_y), line, font=font, fill=fill)
        current_y += line_h + spacing_px
    return current_y
