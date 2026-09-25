from __future__ import annotations

from PIL import Image, ImageFilter, ImageOps

from app.text_rendering import draw_lines, fit_wrapped_text, new_draw
from app.image_rendering import SPECTRA6_COLORS, create_blurred_cover_background, fit_crop, resize_to_fit
from app.module_services import ModuleRenderServices


def _create_fit_canvas(img: Image.Image, target_w: int, target_h: int, theme: str) -> Image.Image:
    if theme == "eink":
        # Flach: weißer Grund, Bild zentriert, schwarzer Rahmen. Kein Blur, kein Schatten.
        canvas = Image.new("RGB", (target_w, target_h), SPECTRA6_COLORS["white"])
        fitted = resize_to_fit(img, target_w - 48, target_h - 48)
        fw, fh = fitted.size
        fx = (target_w - fw) // 2
        fy = (target_h - fh) // 2
        new_draw(canvas).rectangle([(fx - 6, fy - 6), (fx + fw + 5, fy + fh + 5)], fill=SPECTRA6_COLORS["black"])
        canvas.paste(fitted, (fx, fy))
        return canvas

    if theme == "light":
        blurred_bg = fit_crop(img, target_w, target_h).filter(ImageFilter.GaussianBlur(radius=48)).convert("RGBA")
        blurred_bg.alpha_composite(Image.new("RGBA", (target_w, target_h), (250, 248, 244, 175)))
        canvas = blurred_bg
        shadow_fill = (0, 0, 0, 70)
        border_fill = (160, 155, 148, 55)
    else:
        canvas = create_blurred_cover_background(img, target_w, target_h).convert("RGBA")
        shadow_fill = (0, 0, 0, 120)
        border_fill = (255, 255, 255, 26)

    fitted = resize_to_fit(img, target_w, target_h)
    fw, fh = fitted.size
    fx = (target_w - fw) // 2
    fy = (target_h - fh) // 2

    shadow = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    new_draw(shadow, "RGBA").rounded_rectangle(
        [(fx + 8, fy + 12), (fx + fw + 8, fy + fh + 12)],
        radius=24,
        fill=shadow_fill,
    )
    shadow = shadow.filter(ImageFilter.GaussianBlur(radius=16))
    canvas.alpha_composite(shadow)

    border_layer = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    new_draw(border_layer, "RGBA").rounded_rectangle(
        [(fx - 4, fy - 4), (fx + fw + 4, fy + fh + 4)],
        radius=26,
        fill=border_fill,
    )
    canvas.alpha_composite(border_layer)
    canvas.paste(fitted, (fx, fy))
    return canvas.convert("RGB")


def _resolve_caption(content: dict, overlay_mode: str) -> str:
    filename = str(content.get("caption_filename", "")).strip()
    folder = str(content.get("caption_folder", "")).strip()
    if overlay_mode == "filename":
        return filename
    if overlay_mode == "folder":
        return folder
    if overlay_mode == "filename_folder":
        if filename and folder:
            return f"{filename}  |  {folder}"
        return filename or folder
    return ""


def _draw_overlay(base: Image.Image, services: ModuleRenderServices, caption: str) -> Image.Image:
    if not caption:
        return base

    img = base.convert("RGBA")
    draw = new_draw(img)
    target_w, target_h = img.size
    panel_x = 56
    panel_w = target_w - 2 * panel_x
    panel_h = 118
    panel_y = target_h - panel_h - 52
    radius = 26

    flat = services.display_theme == "eink"
    if flat:
        panel_fill = (*SPECTRA6_COLORS["white"], 255)
        border_fill = (*SPECTRA6_COLORS["black"], 255)
        text_fill = (*SPECTRA6_COLORS["black"], 255)
    elif services.display_theme == "light":
        panel_fill = (248, 244, 238, 212)
        border_fill = (180, 172, 160, 95)
        text_fill = (28, 24, 19, 255)
    else:
        panel_fill = (12, 12, 12, 170)
        border_fill = (255, 255, 255, 34)
        text_fill = (255, 255, 255, 255)

    overlay = Image.new("RGBA", (target_w, target_h), (0, 0, 0, 0))
    overlay_draw = new_draw(overlay, "RGBA")
    overlay_draw.rounded_rectangle(
        [(panel_x, panel_y), (panel_x + panel_w, panel_y + panel_h)],
        radius=0 if flat else radius,
        fill=panel_fill,
        outline=border_fill,
        width=4 if flat else 1,
    )
    if not flat:
        overlay = overlay.filter(ImageFilter.GaussianBlur(radius=12))
    img.alpha_composite(overlay)

    font, lines, line_h, line_spacing, _ = fit_wrapped_text(
        draw,
        caption,
        panel_w - 44,
        panel_h - 30,
        38,
        22,
        services.load_font,
        is_bold=True,
        max_lines=2,
        line_spacing=0.14,
    )
    text_y = panel_y + max(14, (panel_h - ((len(lines) * line_h) + (max(0, len(lines) - 1) * line_spacing))) // 2)
    draw_lines(draw, panel_x + 22, text_y, lines, font, text_fill, line_h, line_spacing)
    return img.convert("RGB")


def render_gallery_image(services: ModuleRenderServices, content: dict, fit_mode: str, overlay_mode: str) -> Image.Image:
    # Nicht mehr Pixel dekodieren und aufbereiten als das Bild braucht: ein 24-MP-Foto
    # kostete sonst eine halbe Sekunde und rund 450 MB Speicher (Raspberry, arm64-NAS)
    side = max(services.render_width, services.render_height)
    with Image.open(content["image_path"]) as src:
        src.draft("RGB", (side, side))            # JPEG: gleich verkleinert dekodieren
        img = ImageOps.exif_transpose(src).convert("RGB")
    shortest = min(img.size)
    if shortest > side:
        factor = side / shortest                   # die kurze Seite bleibt ≥ der langen Bildseite – reicht fürs Füllen
        img = img.resize((max(1, round(img.width * factor)), max(1, round(img.height * factor))), Image.LANCZOS)
    if services.display_theme == "eink":
        from app.image_rendering import prepare_photo_for_eink
        img = prepare_photo_for_eink(img)

    if fit_mode == "cover":
        base = fit_crop(img, services.render_width, services.render_height)
    else:
        base = _create_fit_canvas(img, services.render_width, services.render_height, services.display_theme)

    caption = _resolve_caption(content, overlay_mode)
    return _draw_overlay(base, services, caption)
