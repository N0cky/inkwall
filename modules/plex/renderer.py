"""
Plex-Renderer: Now-Playing-Overlays für Video und Musik in Dark und Light.
"""

from __future__ import annotations

from PIL import Image, ImageDraw

from app.config import get_bool_setting, get_cfg, load_font, now_local
from app.image_rendering import SPECTRA6_COLORS, draw_bottom_gradient
from app.text_rendering import draw_lines, fit_wrapped_text, new_draw

from .plex import get_playback_label

# ---------------------------------------------------------------------------
# Layout-Konstanten
# ---------------------------------------------------------------------------

OVERLAY_X_MARGIN      = 80    # px linker/rechter Rand für Text im Overlay
PROGRESS_BAR_Y_OFFSET = 55    # px Abstand Progress-Bar vom unteren Bildrand
PROGRESS_BAR_HEIGHT   = 18    # px Höhe des Fortschrittsbalkens
TIMESTAMP_Y           = 40    # px Abstand Timestamp vom oberen Rand
PROGRESS_BOTTOM_RESERVE = 60  # px Puffer für Progress-Bar-Block
STATUS_BOTTOM_RESERVE   = 45  # px Puffer für Status-Label-Block
OVERLAY_TEXT_TOP_OFFSET = 35  # px Abstand Text-Anfang von Overlay-Oberkante
MIN_VIDEO_TEXT_HEIGHT   = 120 # px Mindesthöhe Textbereich Video
MIN_MUSIC_TEXT_HEIGHT   = 160 # px Mindesthöhe Textbereich Musik
LIGHT_TEXT_PRIMARY          = (25, 20, 15, 255)
LIGHT_TEXT_SECONDARY        = (90, 85, 78, 255)
LIGHT_TEXT_META             = (140, 133, 124, 255)
LIGHT_PROGRESS_TRACK        = (205, 200, 192, 255)
LIGHT_PROGRESS_FILL         = (30, 25, 20, 255)
LIGHT_PANEL_PADDING         = 80    # px seitlicher Textabstand im Light-Panel


# ---------------------------------------------------------------------------
# Fortschritt und Zeitstempel
# ---------------------------------------------------------------------------

def calc_progress(session: dict | None) -> float:
    if not session:
        return 0.0
    try:
        duration_i = max(int(session.get("duration") or 0), 1)
        offset_i   = max(int(session.get("viewOffset") or 0), 0)
        return min(offset_i / duration_i, 1.0)
    except Exception:
        return 0.0


def draw_progress_bar(
    img: Image.Image,
    progress: float,
    x: int, y: int, width: int, height: int,
    show: bool,
) -> None:
    """Zeichnet den Fortschrittsbalken korrekt via RGBA-Overlay-Kompositing."""
    if not show:
        return
    # Separate RGBA-Schicht, damit alpha korrekt auf dem RGB-Bild landet.
    overlay = Image.new("RGBA", img.size, (0, 0, 0, 0))
    d = new_draw(overlay)
    d.rounded_rectangle([(x, y), (x + width, y + height)], radius=8, fill=(255, 255, 255, 70))
    d.rounded_rectangle([(x, y), (x + int(width * progress), y + height)], radius=8, fill=(255, 255, 255, 210))
    img.paste(Image.alpha_composite(img.convert("RGBA"), overlay).convert("RGB"))


def draw_updated_timestamp(
    draw: ImageDraw.ImageDraw, font, right_x: int, text_y: int, show: bool
) -> None:
    """Stempel rechtsbündig bis right_x – die Breite hängt von Schrift und Datum ab, nicht von einer festen Spalte."""
    if not show:
        return
    stamp = now_local().strftime("Aktualisiert: %d.%m.%Y %H:%M")
    draw.text((right_x - draw.textlength(stamp, font=font), text_y), stamp, font=font, fill=(255, 255, 255, 230))


def get_bottom_reserved_space(show_progress_bar: bool) -> int:
    return (PROGRESS_BOTTOM_RESERVE if show_progress_bar else 0) + STATUS_BOTTOM_RESERVE


# ---------------------------------------------------------------------------
# Gemeinsame Overlay-Basis
# ---------------------------------------------------------------------------

def _setup_overlay_base(cfg, base: Image.Image, overlay_h_fraction: float, alpha_max: int):
    """Bereitet die gemeinsame Grundstruktur beider Overlays vor."""
    img  = base.copy()
    draw = new_draw(img, "RGBA")
    font_small  = load_font(28, is_bold=False)
    overlay_h   = int(cfg.render_height * overlay_h_fraction)
    draw_bottom_gradient(img, overlay_h, alpha_max, cfg.render_width, cfg.render_height)

    x           = OVERLAY_X_MARGIN
    text_width  = cfg.render_width - 2 * x
    progress_y  = cfg.render_height - PROGRESS_BAR_Y_OFFSET
    reserved_bottom = get_bottom_reserved_space(get_bool_setting("SHOW_PROGRESS_BAR", True))
    text_top    = cfg.render_height - overlay_h + OVERLAY_TEXT_TOP_OFFSET
    text_bottom_limit = progress_y - reserved_bottom

    layout = {
        "x": x,
        "text_width": text_width,
        "progress_y": progress_y,
        "text_top": text_top,
        "text_bottom_limit": text_bottom_limit,
    }
    return img, draw, font_small, layout


# ---------------------------------------------------------------------------
# Overlay-Rendering
# ---------------------------------------------------------------------------

def draw_video_overlay(base: Image.Image, session: dict) -> Image.Image:
    cfg = get_cfg()
    img, draw, font_small, layout = _setup_overlay_base(cfg, base, 0.42, 240)

    title       = session.get("title", "Unbekannt")
    grandparent = session.get("grandparentTitle", "")
    media_type  = session.get("type", "video")
    year        = session.get("year", "")
    parent_index = session.get("parentIndex", "")
    index       = session.get("index", "")
    player_state = session.get("playerState", "unknown")

    if media_type == "episode" and grandparent:
        subtitle  = grandparent
        meta_line = (f"Staffel {parent_index} · Folge {index}"
                     if parent_index and index else get_playback_label(player_state))
    else:
        subtitle  = year if year else ""
        meta_line = get_playback_label(player_state)

    x   = layout["x"]
    tw  = layout["text_width"]
    py  = layout["progress_y"]
    ttop = layout["text_top"]
    tbl  = layout["text_bottom_limit"]
    text_max_h = max(MIN_VIDEO_TEXT_HEIGHT, tbl - ttop)

    title_font, title_lines, title_lh, title_sp, title_th = fit_wrapped_text(
        draw, title, tw, int(text_max_h * 0.52), 72, 34, load_font, is_bold=True, max_lines=3, line_spacing=0.15)
    sub_font, sub_lines, sub_lh, sub_sp, sub_th = (
        fit_wrapped_text(draw, subtitle, tw, int(text_max_h * 0.22), 42, 24, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.15)
        if subtitle else (None, [], 0, 0, 0)
    )
    meta_font, meta_lines, meta_lh, meta_sp, meta_th = fit_wrapped_text(
        draw, meta_line, tw, int(text_max_h * 0.20), 42, 24, load_font, is_bold=False, max_lines=2, line_spacing=0.15)

    block_h = title_th + (20 if sub_lines else 0) + sub_th + 18 + meta_th
    cur_y = max(ttop, tbl - block_h)
    cur_y = draw_lines(draw, x, cur_y, title_lines, title_font, (255, 255, 255, 255), title_lh, title_sp)
    if sub_lines:
        cur_y += 20
        cur_y = draw_lines(draw, x, cur_y, sub_lines, sub_font, (235, 235, 235, 255), sub_lh, sub_sp)
    cur_y += 18
    draw_lines(draw, x, cur_y, meta_lines, meta_font, (235, 235, 235, 255), meta_lh, meta_sp)

    show_progress = get_bool_setting("SHOW_PROGRESS_BAR", True)
    show_updated = get_bool_setting("SHOW_UPDATED_TIMESTAMP", True)
    draw_progress_bar(img, calc_progress(session), x, py, cfg.render_width - 2 * x, PROGRESS_BAR_HEIGHT, show_progress)
    draw_updated_timestamp(draw, font_small, cfg.render_width - OVERLAY_X_MARGIN, TIMESTAMP_Y, show_updated)
    return img


def draw_music_overlay(base: Image.Image, session: dict) -> Image.Image:
    cfg = get_cfg()
    img, draw, font_small, layout = _setup_overlay_base(cfg, base, 0.46, 245)

    title        = session.get("title", "Unbekannt")
    artist       = session.get("grandparentTitle", "")
    album        = session.get("parentTitle", "")
    track_no     = session.get("index", "")
    player_state = session.get("playerState", "unknown")

    album_line = (f"{album} · Track {track_no}" if album else f"Track {track_no}") if track_no else album
    info_line  = get_playback_label(player_state, "Musik")

    x    = layout["x"]
    tw   = layout["text_width"]
    py   = layout["progress_y"]
    ttop = layout["text_top"]
    tbl  = layout["text_bottom_limit"]
    text_max_h = max(MIN_MUSIC_TEXT_HEIGHT, tbl - ttop)

    title_font,  title_lines,  title_lh,  title_sp,  title_th  = fit_wrapped_text(
        draw, title, tw, int(text_max_h * 0.38), 78, 34, load_font, is_bold=True, max_lines=3, line_spacing=0.15)
    artist_font, artist_lines, artist_lh, artist_sp, artist_th = (
        fit_wrapped_text(draw, artist, tw, int(text_max_h * 0.22), 48, 24, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.15)
        if artist else (None, [], 0, 0, 0)
    )
    album_font,  album_lines,  album_lh,  album_sp,  album_th  = (
        fit_wrapped_text(draw, album_line, tw, int(text_max_h * 0.18), 38, 22, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.15)
        if album_line else (None, [], 0, 0, 0)
    )
    info_font,   info_lines,   info_lh,   info_sp,   info_th   = fit_wrapped_text(
        draw, info_line, tw, int(text_max_h * 0.14), 38, 22, load_font, is_bold=False, max_lines=1, line_spacing=0.15)

    block_h = (title_th + (18 if artist_lines else 0) + artist_th
               + (16 if album_lines else 0) + album_th + 16 + info_th)
    cur_y = max(ttop, tbl - block_h)
    cur_y = draw_lines(draw, x, cur_y, title_lines, title_font, (255, 255, 255, 255), title_lh, title_sp)
    if artist_lines:
        cur_y += 18
        cur_y = draw_lines(draw, x, cur_y, artist_lines, artist_font, (235, 235, 235, 255), artist_lh, artist_sp)
    if album_lines:
        cur_y += 16
        cur_y = draw_lines(draw, x, cur_y, album_lines, album_font, (220, 220, 220, 255), album_lh, album_sp)
    cur_y += 16
    draw_lines(draw, x, cur_y, info_lines, info_font, (220, 220, 220, 255), info_lh, info_sp)

    show_progress = get_bool_setting("SHOW_PROGRESS_BAR", True)
    show_updated = get_bool_setting("SHOW_UPDATED_TIMESTAMP", True)
    draw_progress_bar(img, calc_progress(session), x, py, cfg.render_width - 2 * x, PROGRESS_BAR_HEIGHT, show_progress)
    draw_updated_timestamp(draw, font_small, cfg.render_width - OVERLAY_X_MARGIN, TIMESTAMP_Y, show_updated)
    return img


# ---------------------------------------------------------------------------
# Light-Theme Overlays
# ---------------------------------------------------------------------------

# Light- und E-Ink-Overlays teilen sich das Layout, unterscheiden sich nur in den Farben.
# E-Ink: nur Spectra-6-Farben, damit Text und Balken nicht gedithert werden.
_FLAT_PALETTES = {
    "light": {
        "text_primary":   LIGHT_TEXT_PRIMARY,
        "text_secondary": LIGHT_TEXT_SECONDARY,
        "text_meta":      LIGHT_TEXT_META,
        "progress_track": LIGHT_PROGRESS_TRACK,
        "progress_fill":  LIGHT_PROGRESS_FILL,
    },
    "eink": {
        "text_primary":   (*SPECTRA6_COLORS["black"], 255),
        "text_secondary": (*SPECTRA6_COLORS["black"], 255),
        "text_meta":      (*SPECTRA6_COLORS["blue"], 255),
        "progress_track": (*SPECTRA6_COLORS["white"], 255),
        "progress_fill":  (*SPECTRA6_COLORS["black"], 255),
    },
}


def _flat_palette(theme: str) -> dict:
    return _FLAT_PALETTES["eink" if theme == "eink" else "light"]


def _draw_light_progress_bar(
    draw: ImageDraw.ImageDraw,
    progress: float,
    x: int, y: int, width: int, height: int,
    show: bool,
    pal: dict,
) -> None:
    if not show:
        return
    draw.rounded_rectangle([(x, y), (x + width, y + height)], radius=8, fill=pal["progress_track"],
                           outline=pal["progress_fill"] if pal is _FLAT_PALETTES["eink"] else None, width=2)
    fill_w = max(height, int(width * progress))  # min width = height (fully round)
    draw.rounded_rectangle([(x, y), (x + fill_w, y + height)], radius=8, fill=pal["progress_fill"])


def draw_video_overlay_light(base: Image.Image, session: dict, cover_bottom: int) -> Image.Image:
    cfg = get_cfg()
    pal = _flat_palette(cfg.display_theme)
    img  = base.copy()
    draw = new_draw(img)

    title        = session.get("title", "Unbekannt")
    grandparent  = session.get("grandparentTitle", "")
    media_type   = session.get("type", "video")
    year         = session.get("year", "")
    parent_index = session.get("parentIndex", "")
    index        = session.get("index", "")
    player_state = session.get("playerState", "unknown")

    if media_type == "episode" and grandparent:
        subtitle  = grandparent
        meta_line = (f"Staffel {parent_index} · Folge {index}"
                     if parent_index and index else get_playback_label(player_state))
    else:
        subtitle  = year if year else ""
        meta_line = get_playback_label(player_state)

    # Text-Panel beginnt direkt unterhalb des Covers (dynamisch, kein fester Bruchteil).
    panel_top = cover_bottom + 24

    x  = LIGHT_PANEL_PADDING
    tw = cfg.render_width - 2 * x
    py = cfg.render_height - PROGRESS_BAR_Y_OFFSET

    show_progress = get_bool_setting("SHOW_PROGRESS_BAR", True)
    show_updated = get_bool_setting("SHOW_UPDATED_TIMESTAMP", True)
    reserved    = get_bottom_reserved_space(show_progress)
    text_top    = panel_top + 22
    text_bottom = py - reserved
    text_max_h  = max(MIN_VIDEO_TEXT_HEIGHT, text_bottom - text_top)

    font_small = load_font(26, is_bold=False)

    title_font, title_lines, title_lh, title_sp, title_th = fit_wrapped_text(
        draw, title, tw, int(text_max_h * 0.52), 68, 28, load_font, is_bold=True, max_lines=3, line_spacing=0.14)
    sub_font, sub_lines, sub_lh, sub_sp, sub_th = (
        fit_wrapped_text(draw, subtitle, tw, int(text_max_h * 0.24), 40, 22, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.14)
        if subtitle else (None, [], 0, 0, 0)
    )
    meta_font, meta_lines, meta_lh, meta_sp, meta_th = fit_wrapped_text(
        draw, meta_line, tw, int(text_max_h * 0.20), 36, 20, load_font,
        is_bold=False, max_lines=2, line_spacing=0.14)

    block_h = title_th + (16 if sub_lines else 0) + sub_th + 14 + meta_th
    cur_y   = max(text_top, text_bottom - block_h)
    cur_y   = draw_lines(draw, x, cur_y, title_lines, title_font, pal["text_primary"],   title_lh, title_sp)
    if sub_lines:
        cur_y += 16
        cur_y = draw_lines(draw, x, cur_y, sub_lines,   sub_font,   pal["text_secondary"], sub_lh,   sub_sp)
    cur_y += 14
    draw_lines(draw, x, cur_y, meta_lines, meta_font, pal["text_meta"], meta_lh, meta_sp)

    _draw_light_progress_bar(draw, calc_progress(session), x, py,
                             cfg.render_width - 2 * x, PROGRESS_BAR_HEIGHT, show_progress, pal)
    if show_updated:
        # Unterhalb des Textblocks statt oben rechts – dort läge der Stempel auf dem Cover
        stamp = now_local().strftime("Aktualisiert: %d.%m.%Y %H:%M")
        draw.text((cfg.render_width - x - draw.textlength(stamp, font=font_small), text_bottom + 14), stamp,
                  font=font_small, fill=pal["text_meta"])
    return img


def draw_music_overlay_light(base: Image.Image, session: dict, cover_bottom: int) -> Image.Image:
    cfg = get_cfg()
    pal = _flat_palette(cfg.display_theme)
    img  = base.copy()
    draw = new_draw(img)

    title        = session.get("title", "Unbekannt")
    artist       = session.get("grandparentTitle", "")
    album        = session.get("parentTitle", "")
    track_no     = session.get("index", "")
    player_state = session.get("playerState", "unknown")

    album_line = (f"{album} · Track {track_no}" if album else f"Track {track_no}") if track_no else album
    info_line  = get_playback_label(player_state, "Musik")

    # Text-Panel beginnt direkt unterhalb des Covers (dynamisch, kein fester Bruchteil).
    panel_top = cover_bottom + 24

    x  = LIGHT_PANEL_PADDING
    tw = cfg.render_width - 2 * x
    py = cfg.render_height - PROGRESS_BAR_Y_OFFSET

    show_progress = get_bool_setting("SHOW_PROGRESS_BAR", True)
    show_updated = get_bool_setting("SHOW_UPDATED_TIMESTAMP", True)
    reserved    = get_bottom_reserved_space(show_progress)
    text_top    = panel_top + 22
    text_bottom = py - reserved
    text_max_h  = max(MIN_MUSIC_TEXT_HEIGHT, text_bottom - text_top)

    font_small = load_font(26, is_bold=False)

    title_font,  title_lines,  title_lh,  title_sp,  title_th  = fit_wrapped_text(
        draw, title, tw, int(text_max_h * 0.38), 68, 28, load_font, is_bold=True, max_lines=3, line_spacing=0.14)
    artist_font, artist_lines, artist_lh, artist_sp, artist_th = (
        fit_wrapped_text(draw, artist, tw, int(text_max_h * 0.24), 44, 20, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.14)
        if artist else (None, [], 0, 0, 0)
    )
    album_font,  album_lines,  album_lh,  album_sp,  album_th  = (
        fit_wrapped_text(draw, album_line, tw, int(text_max_h * 0.20), 34, 18, load_font,
                         is_bold=False, max_lines=2, line_spacing=0.14)
        if album_line else (None, [], 0, 0, 0)
    )
    info_font,   info_lines,   info_lh,   info_sp,   info_th   = fit_wrapped_text(
        draw, info_line, tw, int(text_max_h * 0.14), 34, 18, load_font,
        is_bold=False, max_lines=1, line_spacing=0.14)

    block_h = (title_th + (14 if artist_lines else 0) + artist_th
               + (12 if album_lines else 0) + album_th + 12 + info_th)
    cur_y   = max(text_top, text_bottom - block_h)
    cur_y   = draw_lines(draw, x, cur_y, title_lines,  title_font,  pal["text_primary"],   title_lh,  title_sp)
    if artist_lines:
        cur_y += 14
        cur_y = draw_lines(draw, x, cur_y, artist_lines, artist_font, pal["text_secondary"], artist_lh, artist_sp)
    if album_lines:
        cur_y += 12
        cur_y = draw_lines(draw, x, cur_y, album_lines,  album_font,  pal["text_meta"],      album_lh,  album_sp)
    cur_y += 12
    draw_lines(draw, x, cur_y, info_lines, info_font, pal["text_meta"], info_lh, info_sp)

    _draw_light_progress_bar(draw, calc_progress(session), x, py,
                             cfg.render_width - 2 * x, PROGRESS_BAR_HEIGHT, show_progress, pal)
    if show_updated:
        # Unterhalb des Textblocks statt oben rechts – dort läge der Stempel auf dem Cover
        stamp = now_local().strftime("Aktualisiert: %d.%m.%Y %H:%M")
        draw.text((cfg.render_width - x - draw.textlength(stamp, font=font_small), text_bottom + 14), stamp,
                  font=font_small, fill=pal["text_meta"])
    return img
