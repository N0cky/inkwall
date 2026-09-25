"""
Tankpreise-Renderer: Stationsliste mit großem Preis (hochgestellte 9), darunter
abschaltbare Abschnitte – Tagesverlauf, 7 Tage, 30 Tage, Uhrzeit-Profil und
Kennzahlen. Drei Themes, Vollbild und kompakte Dashboard-Kachel.

Alle Diagramme sind flach (Linien, Balken, Punkte), damit sie auf dem
Spectra-6-Panel ohne Dithering-Rauschen auskommen.
"""

from __future__ import annotations

from datetime import date, datetime

from PIL import Image, ImageDraw

from app.config import WEEKDAYS_DE, format_date_long
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from app.text_rendering import new_draw

Color = tuple[int, int, int, int]

FUEL_LABELS = {"e5": "Super E5", "e10": "Super E10", "diesel": "Diesel"}
FUEL_SHORT = {"e5": "E5", "e10": "E10", "diesel": "Diesel"}
SECTION_ORDER = ("day", "week", "month", "hours", "stats")
SECTION_TITLES = {"day": "Tagesverlauf", "week": "Die letzten 7 Tage", "month": "Die letzten 30 Tage",
                  "hours": "Wann ist es günstig?", "stats": "Kennzahlen"}


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
        "price":     (245, 247, 250, 255),
        "cheap":     (110, 200, 140, 255),
        "pricey":    (245, 120, 110, 255),
        "closed":    (120, 125, 135, 255),
        "row_line":  (54, 58, 68, 255),
        "grid":      (58, 62, 72, 255),
        "axis":      (120, 126, 138, 255),
        "line":      (130, 185, 255, 255),
        "bar":       (80, 120, 180, 255),
        "bar_dim":   (52, 66, 92, 255),
        "box":       (36, 39, 47, 255),
        "box_line":  (60, 64, 74, 255),
        "alert_bg":  (72, 40, 42, 255),
        "alert":     (255, 170, 160, 255),
        "up":        (245, 120, 110, 255),
        "down":      (110, 200, 140, 255),
    },
    "light": {
        "flat": False,
        "bg":        (244, 241, 236, 255),
        "header":    (60, 54, 46, 255),
        "title":     (24, 20, 16, 255),
        "muted":     (120, 112, 102, 255),
        "text":      (30, 26, 22, 255),
        "price":     (24, 20, 16, 255),
        "cheap":     (36, 130, 70, 255),
        "pricey":    (198, 50, 46, 255),
        "closed":    (150, 142, 132, 255),
        "row_line":  (212, 207, 200, 255),
        "grid":      (218, 213, 206, 255),
        "axis":      (150, 142, 132, 255),
        "line":      (30, 90, 180, 255),
        "bar":       (80, 120, 180, 255),
        "bar_dim":   (178, 196, 220, 255),
        "box":       (236, 232, 226, 255),
        "box_line":  (208, 203, 195, 255),
        "alert_bg":  (250, 226, 222, 255),
        "alert":     (170, 40, 36, 255),
        "up":        (198, 50, 46, 255),
        "down":      (36, 130, 70, 255),
    },
    "eink": {
        "flat": True,
        "bg":        _c("white"),
        "header":    _c("black"),
        "title":     _c("black"),
        "muted":     _c("blue"),
        "text":      _c("black"),
        "price":     _c("black"),
        "cheap":     _c("green"),
        "pricey":    _c("red"),
        "closed":    _c("blue"),
        "row_line":  _c("black"),
        "grid":      _c("blue"),
        "axis":      _c("black"),
        "line":      _c("black"),
        "bar":       _c("blue"),
        "bar_dim":   _c("blue"),
        "box":       _c("white"),
        "box_line":  _c("black"),
        "alert_bg":  _c("yellow"),
        "alert":     _c("black"),
        "up":        _c("red"),
        "down":      _c("green"),
    },
}


def get_palette(theme: str) -> dict:
    return _PALETTES.get(theme, _PALETTES["dark"])


# ---------------------------------------------------------------------------
# Kleine Helfer
# ---------------------------------------------------------------------------

def split_price(price: float | None) -> tuple[str, str]:
    """1.729 → ('1,72', '9') – Tankstellen-Optik mit hochgestellter Zehntel-Cent-Stelle."""
    if price is None:
        return ("–", "")
    tenths = int(round(price * 1000))
    return (f"{tenths // 1000},{(tenths // 10) % 100:02d}", str(tenths % 10))


def format_price(price: float | None) -> str:
    main, sup = split_price(price)
    return f"{main}{sup}" if sup else main


def format_cents(delta_ct: float) -> str:
    text = f"{abs(delta_ct):.1f}".replace(".", ",")
    return text[:-2] if text.endswith(",0") else text


def _ellipsize(draw: ImageDraw.ImageDraw, text: str, font, max_w: int) -> str:
    if draw.textlength(text, font=font) <= max_w:
        return text
    while text and draw.textlength(text + "…", font=font) > max_w:
        text = text[:-1]
    return (text.rstrip() + "…") if text else ""


def draw_price(draw: ImageDraw.ImageDraw, right: int, top: int, price: float | None, font, sup_font, fill) -> int:
    """Preis rechtsbündig mit hochgestellter letzter Stelle. Gibt die gezeichnete Breite zurück."""
    main, sup = split_price(price)
    sup_w = draw.textlength(sup, font=sup_font) if sup else 0
    main_w = draw.textlength(main, font=font)
    x = right - sup_w - main_w
    draw.text((x, top), main, font=font, fill=fill)
    if sup:
        draw.text((x + main_w + 1, top), sup, font=sup_font, fill=fill)
    return int(main_w + sup_w)


def _triangle(draw: ImageDraw.ImageDraw, cx: int, cy: int, size: int, up: bool, fill) -> None:
    h = size
    if up:
        draw.polygon([(cx - h, cy + h // 2), (cx + h, cy + h // 2), (cx, cy - h // 2)], fill=fill)
    else:
        draw.polygon([(cx - h, cy - h // 2), (cx + h, cy - h // 2), (cx, cy + h // 2)], fill=fill)


def _stale_text(data: dict) -> str:
    raw = str(data.get("stale_since", "") or "")
    if not raw:
        return ""
    try:
        return f"Stand {datetime.fromisoformat(raw):%H:%M}"
    except ValueError:
        return ""


def _now(data: dict) -> datetime | None:
    try:
        return datetime.fromisoformat(data["now"]) if data.get("now") else None
    except ValueError:
        return None


def _axis_range(values: list[float], step: float = 0.02) -> tuple[float, float]:
    """Achse in 2-Cent-Schritten mit etwas Luft, mindestens zwei Schritte hoch."""
    lo, hi = min(values), max(values)
    lo = (int(lo / step) - 1) * step
    hi = (int(hi / step) + 2) * step
    while hi - lo < 2 * step - 1e-9:
        hi += step
    return (round(lo, 3), round(hi, 3))


def _fmt_day(d: date) -> str:
    return f"{d.day}.{d.month}."


# ---------------------------------------------------------------------------
# Diagramm-Bausteine
# ---------------------------------------------------------------------------

def _draw_grid(draw, box, lo, hi, font, pal, px, step=0.02, label_every=1):
    """Waagerechte Hilfslinien mit Preisbeschriftung links. Gibt die Datenfläche zurück."""
    left, top, right, bottom = box
    axis_w = px(66)
    plot = (left + axis_w, top, right, bottom)
    span = max(hi - lo, 1e-6)
    # Beschriftungen brauchen Platz: bei engen Linien nur jede zweite oder dritte
    tick_px = (bottom - top) / max(span / step, 1)
    label_every = max(label_every, int(px(26) / tick_px) + 1 if tick_px < px(26) else 1)
    ticks = []
    value = lo
    idx = 0
    while value <= hi + 1e-9:
        y = int(bottom - (value - lo) / span * (bottom - top))
        draw.line((plot[0], y, right, y), fill=pal["axis"] if idx == 0 else pal["grid"], width=1)
        if idx % label_every == 0:
            label = split_price(value)[0]
            lw = draw.textlength(label, font=font)
            draw.text((plot[0] - lw - px(8), y - px(11)), label, font=font, fill=pal["muted"])
        ticks.append(value)
        value = round(value + step, 3)
        idx += 1
    return plot


def _y_of(value, lo, hi, top, bottom) -> int:
    span = max(hi - lo, 1e-6)
    return int(bottom - (value - lo) / span * (bottom - top))


def draw_day_section(draw, box, stats: dict, pal, px, load_font, primary: str):
    left, top, right, bottom = box
    font_axis = load_font(px(20), False)
    font_note = load_font(px(22), False)
    points = stats.get("day_points") or []
    trend = stats.get("trend")
    # Kopfzeile rechts: jetzt + Trend
    if trend:
        font_val = load_font(px(26), True)
        font_sup = load_font(px(16), True)
        x_right = right
        delta = trend["delta_ct"]
        if abs(delta) >= 0.05:
            note = f"{format_cents(delta)} ct seit {trend['reference_at']}"
            nw = draw.textlength(note, font=font_note)
            draw.text((x_right - nw, top - px(30)), note, font=font_note, fill=pal["up"] if delta > 0 else pal["down"])
            x_right -= nw + px(30)
            _triangle(draw, int(x_right + px(8)), top - px(18), px(8), delta > 0, pal["up"] if delta > 0 else pal["down"])
            x_right -= px(26)
        else:
            note = f"unverändert seit {trend['reference_at']}"
            nw = draw.textlength(note, font=font_note)
            draw.text((x_right - nw, top - px(30)), note, font=font_note, fill=pal["muted"])
            x_right -= nw + px(20)
        w = draw_price(draw, int(x_right), top - px(34), trend["now"], font_val, font_sup, pal["price"])
        label = "jetzt "
        draw.text((x_right - w - draw.textlength(label, font=font_note) - px(4), top - px(30)), label, font=font_note, fill=pal["muted"])
    if len(points) < 2:
        draw.text((left, top + px(10)), "Noch keine Werte für heute – die Linie füllt sich mit jeder Abfrage.", font=font_note, fill=pal["muted"])
        return
    values = [p[1] for p in points]
    lo, hi = _axis_range(values)
    step = 0.02 if hi - lo <= 0.12 else 0.05
    lo, hi = _axis_range(values, step)
    plot = _draw_grid(draw, (left, top, right, bottom - px(28)), lo, hi, font_axis, pal, px, step)
    pl, pt, pr, pb = plot
    # Zeitachse 0–24 Uhr
    for hour in (0, 6, 12, 18, 24):
        x = int(pl + hour / 24 * (pr - pl))
        draw.line((x, pb, x, pb + px(6)), fill=pal["axis"], width=1)
        label = f"{hour} Uhr" if hour in (6, 12, 18) else str(hour)
        lw = draw.textlength(label, font=font_axis)
        draw.text((min(max(pl, x - lw / 2), pr - lw), pb + px(8)), label, font=font_axis, fill=pal["muted"])
    coords = []
    for when, price in points:
        frac = (when.hour * 60 + when.minute) / 1440
        coords.append((int(pl + frac * (pr - pl)), _y_of(price, lo, hi, pt, pb)))
    # Treppenlinie: der Preis gilt, bis der nächste kommt
    path = []
    for i, (x, y) in enumerate(coords):
        if i:
            path.append((x, coords[i - 1][1]))
        path.append((x, y))
    draw.line(path, fill=pal["line"], width=px(4) if pal["flat"] else px(3), joint="curve")
    lowest = min(points, key=lambda p: p[1])
    lx, ly = coords[points.index(lowest)]
    r = px(6)
    draw.ellipse((lx - r, ly - r, lx + r, ly + r), fill=pal["cheap"])
    cx, cy = coords[-1]
    draw.ellipse((cx - r, cy - r, cx + r, cy + r), fill=pal["line"], outline=pal["bg"], width=2)
    low_label = f"Tief {format_price(lowest[1])} um {lowest[0]:%H:%M}"
    lw = draw.textlength(low_label, font=font_axis)
    draw.text((min(max(pl, lx - lw / 2), pr - lw), min(ly + px(10), pb - px(24))), low_label, font=font_axis, fill=pal["cheap"])


def draw_bar_section(draw, box, bars: list[dict], pal, px, load_font, empty_text: str, value_labels: bool = True,
                     x_labels_every: int = 1, today_idx: int | None = None):
    """
    bars: [{label, value|None, highlight: bool, dim: bool}] – ein Balken je Eintrag, Skala aus den Werten.
    """
    left, top, right, bottom = box
    font_axis = load_font(px(20), False)
    font_val = load_font(px(19), True)
    font_sup = load_font(px(13), True)
    values = [b["value"] for b in bars if b["value"] is not None]
    if not values:
        draw.text((left, top + px(10)), empty_text, font=load_font(px(22), False), fill=pal["muted"])
        return
    lo, hi = _axis_range(values)
    step = 0.02 if hi - lo <= 0.12 else 0.05
    lo, hi = _axis_range(values, step)
    lo = max(0.0, lo)
    label_h = px(28)
    plot = _draw_grid(draw, (left, top + (px(26) if value_labels else 0), right, bottom - label_h), lo, hi, font_axis, pal, px, step)
    pl, pt, pr, pb = plot
    n = len(bars)
    slot = (pr - pl) / max(n, 1)
    bar_w = max(px(6), int(slot * 0.62))
    for i, bar in enumerate(bars):
        cx = int(pl + slot * (i + 0.5))
        label = bar["label"]
        if label and i % x_labels_every == 0:
            lw = draw.textlength(label, font=font_axis)
            draw.text((cx - lw / 2, pb + px(8)), label, font=font_axis, fill=pal["title"] if today_idx == i else pal["muted"])
        if bar["value"] is None:
            continue
        y = _y_of(bar["value"], lo, hi, pt, pb)
        fill = pal["cheap"] if bar.get("highlight") else (pal["bar_dim"] if bar.get("dim") else pal["bar"])
        label_y = y
        if bar.get("avg") is not None and bar["avg"] > bar["value"]:
            # heller Aufsatz vom Tiefstpreis bis zum Tagesdurchschnitt
            ay = _y_of(bar["avg"], lo, hi, pt, pb)
            draw.rectangle((cx - bar_w // 2, ay, cx + bar_w // 2, y), fill=pal["bar_dim"])
            label_y = ay
        draw.rectangle((cx - bar_w // 2, y, cx + bar_w // 2, pb), fill=fill)
        if value_labels:
            main, sup = split_price(bar["value"])
            tw = draw.textlength(main, font=font_val) + draw.textlength(sup, font=font_sup)
            draw_price(draw, int(cx + tw / 2), label_y - px(24), bar["value"], font_val, font_sup, pal["cheap"] if bar.get("highlight") else pal["text"])


def draw_month_section(draw, box, stats: dict, pal, px, load_font):
    left, top, right, bottom = box
    font_axis = load_font(px(20), False)
    days = stats.get("month") or []
    values = [d["min"] for d in days if d["min"] is not None]
    if len(values) < 2:
        draw.text((left, top + px(10)), "Der 30-Tage-Verlauf entsteht mit den nächsten Tagen.", font=load_font(px(22), False), fill=pal["muted"])
        return
    lo, hi = _axis_range(values)
    step = 0.02 if hi - lo <= 0.12 else 0.05
    lo, hi = _axis_range(values, step)
    plot = _draw_grid(draw, (left, top, right, bottom - px(28)), lo, hi, font_axis, pal, px, step)
    pl, pt, pr, pb = plot
    n = len(days)
    slot = (pr - pl) / max(n - 1, 1)
    segment: list[tuple[int, int]] = []
    low = None
    for i, d in enumerate(days):
        x = int(pl + i * slot)
        if (i % 7 == 0 and i < n - 3) or i == n - 1:
            label = _fmt_day(d["date"])
            lw = draw.textlength(label, font=font_axis)
            draw.text((min(max(pl, x - lw / 2), pr - lw), pb + px(8)), label, font=font_axis, fill=pal["muted"])
            draw.line((x, pb, x, pb + px(6)), fill=pal["axis"], width=1)
        if d["min"] is None:
            if len(segment) > 1:
                draw.line(segment, fill=pal["line"], width=px(3))
            elif segment:
                sx, sy = segment[0]
                draw.ellipse((sx - px(3), sy - px(3), sx + px(3), sy + px(3)), fill=pal["line"])
            segment = []
            continue
        y = _y_of(d["min"], lo, hi, pt, pb)
        segment.append((x, y))
        if low is None or d["min"] < low[2]:
            low = (x, y, d["min"], d)
    if len(segment) > 1:
        draw.line(segment, fill=pal["line"], width=px(3))
    elif segment:
        sx, sy = segment[0]
        draw.ellipse((sx - px(3), sy - px(3), sx + px(3), sy + px(3)), fill=pal["line"])
    if low:
        x, y, value, d = low
        r = px(6)
        draw.ellipse((x - r, y - r, x + r, y + r), fill=pal["cheap"])
        label = f"Tief {format_price(value)} am {_fmt_day(d['date'])}"
        lw = draw.textlength(label, font=font_axis)
        draw.text((min(max(pl, x - lw / 2), pr - lw), min(y + px(10), pb - px(24))), label, font=font_axis, fill=pal["cheap"])


def draw_hours_section(draw, box, stats: dict, pal, px, load_font, now: datetime | None):
    left, top, right, bottom = box
    if not stats.get("stats_ready"):
        ready_on = stats.get("stats_ready_on")
        text = f"Das Uhrzeit-Profil gibt es ab {_fmt_day(ready_on)}" if ready_on else "Das Uhrzeit-Profil entsteht ab dem ersten Tag"
        text += f" – bisher {stats.get('days_collected', 0)} Tage gesammelt."
        draw.text((left, top + px(10)), text, font=load_font(px(22), False), fill=pal["muted"])
        return
    profile = stats.get("hour_profile") or [None] * 24
    best = stats.get("best_time") or {}
    window = set()
    if best:
        window = {best["hour_start"], (best["hour_start"] + 1) % 24}
    bars = [{"label": f"{h}" if h % 6 == 0 else "", "value": v, "highlight": h in window,
             "dim": now is not None and h != now.hour and h not in window} for h, v in enumerate(profile)]
    draw_bar_section(draw, box, bars, pal, px, load_font, "Noch keine Stundenwerte.", value_labels=False)
    if now is not None:
        # Aktuelle Stunde markieren
        font_axis = load_font(px(20), False)
        pl = left + px(66)
        slot = (right - pl) / 24
        cx = int(pl + slot * (now.hour + 0.5))
        _triangle(draw, cx, bottom - px(34), px(6), True, pal["title"])
        draw.text((cx + px(10), bottom - px(44)), "jetzt", font=font_axis, fill=pal["title"])


def draw_stats_section(draw, box, data: dict, stats: dict, pal, px, load_font):
    left, top, right, bottom = box
    font_label = load_font(px(19), False)
    font_val = load_font(px(30), True)
    font_sup = load_font(px(18), True)
    font_sub = load_font(px(20), False)
    font_mini = load_font(px(18), True)
    font_mini_sup = load_font(px(12), True)
    primary = data.get("primary", "e5")
    gap = px(12)
    cols = 2 if right - left >= px(700) else 1
    boxes = ["weekday", "lows", "best", "saving"]
    rows = (len(boxes) + cols - 1) // cols
    bw = (right - left - gap * (cols - 1)) / cols
    bh = (bottom - top - gap * (rows - 1)) / rows
    ready_on = stats.get("stats_ready_on")
    not_ready = f"ab {_fmt_day(ready_on)}" if ready_on else "sobald Daten da sind"

    for i, kind in enumerate(boxes):
        col, row = i % cols, i // cols
        bx0 = int(left + col * (bw + gap))
        by0 = int(top + row * (bh + gap))
        bx1, by1 = int(bx0 + bw), int(by0 + bh)
        draw.rounded_rectangle((bx0, by0, bx1, by1), radius=0 if pal["flat"] else px(12), fill=pal["box"], outline=pal["box_line"], width=1)
        ix, iy = bx0 + px(14), by0 + px(10)
        if kind == "weekday":
            draw.text((ix, iy), "Ø je Wochentag", font=font_label, fill=pal["muted"])
            avgs = stats.get("weekday_avg") or [None] * 7
            if not stats.get("stats_ready") or all(v is None for v in avgs):
                draw.text((ix, iy + px(30)), f"Statistik {not_ready}", font=font_sub, fill=pal["muted"])
                continue
            known = [v for v in avgs if v is not None]
            lowest = min(known)
            slot = (bx1 - bx0 - px(28)) / 7
            for d, v in enumerate(avgs):
                cx = ix + slot * (d + 0.5)
                lw = draw.textlength(WEEKDAYS_DE[d], font=font_sub)
                cheap = v is not None and v - lowest <= 0.005
                draw.text((cx - lw / 2, iy + px(30)), WEEKDAYS_DE[d], font=font_sub, fill=pal["cheap"] if cheap else pal["muted"])
                if v is None:
                    tw = draw.textlength("–", font=font_mini)
                    draw.text((cx - tw / 2, iy + px(56)), "–", font=font_mini, fill=pal["muted"])
                    continue
                main, sup = split_price(v)
                tw = draw.textlength(main, font=font_mini) + draw.textlength(sup, font=font_mini_sup)
                draw_price(draw, int(cx + tw / 2), iy + px(56), v, font_mini, font_mini_sup, pal["cheap"] if cheap else pal["text"])
        elif kind == "lows":
            draw.text((ix, iy), "Tiefstpreis", font=font_label, fill=pal["muted"])
            lows = stats.get("lows") or {}
            y = iy + px(28)
            for key, label in (("week", "7 Tage"), ("month", "30 Tage")):
                entry = lows.get(key)
                draw.text((ix, y + px(4)), label, font=font_sub, fill=pal["muted"])
                if entry:
                    w = draw_price(draw, bx1 - px(14), y - px(2), entry["min"], font_val, font_sup, pal["cheap"])
                    detail = f"{WEEKDAYS_DE[entry['date'].weekday()]} {_fmt_day(entry['date'])}"
                    if entry.get("min_at"):
                        detail += f" {entry['min_at']}"
                    if entry.get("min_station"):
                        detail += f" · {entry['min_station']}"
                    detail = _ellipsize(draw, detail, font_sub, int(bx1 - px(14) - w - px(10) - (ix + px(80))))
                    dw = draw.textlength(detail, font=font_sub)
                    draw.text((bx1 - px(14) - w - px(10) - dw, y + px(4)), detail, font=font_sub, fill=pal["text"])
                else:
                    draw.text((ix + px(80), y + px(4)), "noch keine Daten", font=font_sub, fill=pal["muted"])
                y += px(38)
        elif kind == "best":
            draw.text((ix, iy), "Beste Tankzeit", font=font_label, fill=pal["muted"])
            best = stats.get("best_time")
            if not best:
                draw.text((ix, iy + px(30)), f"Statistik {not_ready}", font=font_sub, fill=pal["muted"])
                continue
            days = ", ".join(WEEKDAYS_DE[d] for d in best["weekdays"]) if len(best["weekdays"]) < 7 else "jeden Tag"
            draw.text((ix, iy + px(26)), _ellipsize(draw, f"{days} · {best['hour_start']}–{best['hour_end']} Uhr", font_val, int(bw - px(28))), font=font_val, fill=pal["text"])
            sub = f"Ø {format_price(best['hour_avg'])} in diesem Zeitfenster"
            draw.text((ix, iy + px(66)), _ellipsize(draw, sub, font_sub, int(bw - px(28))), font=font_sub, fill=pal["muted"])
        elif kind == "saving":
            draw.text((ix, iy), "Ersparnis heute", font=font_label, fill=pal["muted"])
            saving = data.get("saving_ct")
            if saving is None:
                draw.text((ix, iy + px(30)), "nur eine Station mit Preis", font=font_sub, fill=pal["muted"])
                continue
            draw.text((ix, iy + px(26)), f"{format_cents(saving)} ct/l", font=font_val, fill=pal["cheap"] if saving > 0 else pal["text"])
            euros = saving * 50 / 100
            cheap = data.get("cheapest", {}).get(primary, {}).get("station", "")
            pricey = next((s["name"] for s in data.get("stations", []) if s.get("is_priciest")), "")
            sub = f"{euros:.2f} € bei 50 l".replace(".", ",")
            if cheap and pricey:
                sub += f" · {cheap} statt {pricey}"
            draw.text((ix, iy + px(66)), _ellipsize(draw, sub, font_sub, int(bw - px(28))), font=font_sub, fill=pal["muted"])


# ---------------------------------------------------------------------------
# Seite
# ---------------------------------------------------------------------------

_SECTION_MIN = {"day": 200, "week": 180, "month": 180, "hours": 160, "stats": 280}
_SECTION_WEIGHT = {"day": 1.2, "week": 1.0, "month": 1.0, "hours": 0.8, "stats": 0.6}
_EMPTY_HEIGHT = 80      # Abschnitt ohne Daten: Titel plus ein Satz


def _has_data(key: str, stats: dict) -> bool:
    if key == "day":
        return len(stats.get("day_points") or []) >= 2
    if key == "week":
        return any(d.get("min") is not None for d in stats.get("week") or [])
    if key == "month":
        return sum(1 for d in stats.get("month") or [] if d.get("min") is not None) >= 2
    if key == "hours":
        return bool(stats.get("stats_ready"))
    return True


def _plan_sections(wanted: list[str], available: int, px, stats: dict) -> list[tuple[list[str], int]]:
    """
    Zeilen aus Abschnitten in fester Reihenfolge; 7 Tage und 30 Tage teilen sich eine
    Zeile, wenn beide Daten haben. Abschnitte ohne Daten bekommen nur Platz für einen Satz.
    Was nicht mehr passt, fällt hinten weg.
    """
    order = [s for s in SECTION_ORDER if s in wanted]
    rows: list[list[str]] = []
    for s in order:
        if s == "month" and rows and rows[-1] == ["week"] and _has_data("week", stats) and _has_data("month", stats):
            rows[-1].append("month")
        else:
            rows.append([s])

    def min_h(row: list[str]) -> int:
        return px(max(_SECTION_MIN[s] if _has_data(s, stats) else _EMPTY_HEIGHT for s in row))

    def weight(row: list[str]) -> float:
        return max(_SECTION_WEIGHT[s] if _has_data(s, stats) else 0.0 for s in row)

    while rows and sum(min_h(r) for r in rows) > available:
        rows.pop()
    if not rows:
        return []
    extra = max(0, available - sum(min_h(r) for r in rows))
    total_w = sum(weight(r) for r in rows) or 1.0
    return [(r, min_h(r) + int(extra * weight(r) / total_w)) for r in rows]


def render_fuel_module(services: ModuleRenderServices, content: object, compact: bool = False) -> Image.Image:
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

    fuels: list[str] = [f for f in data.get("fuels", []) if f in FUEL_LABELS] or ["e5"]
    primary = data.get("primary") if data.get("primary") in fuels else fuels[0]
    secondary = [f for f in fuels if f != primary]
    stations = data.get("stations") or []
    stale_text = _stale_text(data)
    now = _now(data)
    font_small = load_font(px(22), False)

    # ── Kopfzeile ────────────────────────────────────────────────────────────
    if compact:
        title = f"Tankpreise · {FUEL_SHORT[primary]}"
        draw.text((margin, px(14)), title, font=load_font(px(26), True), fill=pal["title"])
        right_text = stale_text
        if right_text:
            draw.text((rw - margin - draw.textlength(right_text, font=font_small), px(18)), right_text, font=font_small, fill=pal["muted"])
        y = px(56)
    else:
        draw.text((margin, px(46)), format_date_long(now) if now else format_date_long(), font=load_font(px(32), False), fill=pal["header"])
        draw.text((margin, px(86)), "Tankpreise", font=load_font(px(60), True), fill=pal["title"])
        right_text = stale_text or (f"Stand {now:%H:%M}" if now else "")
        if right_text:
            f = load_font(px(24), False)
            draw.text((rw - margin - draw.textlength(right_text, font=f), px(52)), right_text, font=f, fill=pal["muted"])
        scope = "deine Stationen" if data.get("fixed") else (f"Umkreis {data['radius_km']} km" if data.get("radius_km") else "Umkreis")
        shown = len(stations)
        total = data.get("total", shown)
        count = f"{shown} von {total} Stationen" if total > shown else f"{shown} Station{'en' if shown != 1 else ''}"
        draw.text((margin, px(158)), f"{FUEL_LABELS[primary]} · {scope} · {count}", font=font_small, fill=pal["muted"])
        y = px(190)

    # ── Preisalarm ───────────────────────────────────────────────────────────
    if data.get("alert") and not compact:
        cheapest = data.get("cheapest", {}).get(primary, {})
        text = f"Preisalarm: {FUEL_SHORT[primary]} unter {format_price(data.get('alert_price'))} – jetzt {format_price(cheapest.get('price'))}"
        if cheapest.get("station"):
            text += f" bei {cheapest['station']}"
        h = px(50)
        draw.rounded_rectangle((margin, y, rw - margin, y + h), radius=0 if flat else px(12), fill=pal["alert_bg"],
                               outline=pal["alert"] if flat else None, width=2)
        f = load_font(px(24), True)
        draw.text((margin + px(16), y + (h - px(28)) // 2), _ellipsize(draw, text, f, rw - 2 * margin - px(32)), font=f, fill=pal["alert"])
        y += h + px(14)

    # ── Liste ────────────────────────────────────────────────────────────────
    row_h = px(52) if compact else px(70)
    font_name = load_font(px(24) if compact else px(30), True)
    font_sub = load_font(px(18) if compact else px(22), False)
    font_price = load_font(px(32) if compact else px(44), True)
    font_price_sup = load_font(px(18) if compact else px(24), True)
    font_sec = load_font(px(22) if compact else px(28), True)
    font_sec_sup = load_font(px(13) if compact else px(16), True)
    font_head = load_font(px(18) if compact else px(20), False)
    primary_w = px(150) if compact else px(190)
    sec_w = px(110) if compact else px(140)
    if compact:
        secondary = secondary[:1]

    # Spaltenköpfe
    x = rw - margin
    if not compact or secondary:
        draw.text((x - draw.textlength(FUEL_SHORT[primary], font=font_head), y), FUEL_SHORT[primary], font=font_head, fill=pal["muted"])
        x -= primary_w
        for f in secondary:
            draw.text((x - draw.textlength(FUEL_SHORT[f], font=font_head), y), FUEL_SHORT[f], font=font_head, fill=pal["muted"])
            x -= sec_w
        y += px(28)
        draw.line([(margin, y), (rw - margin, y)], fill=pal["row_line"], width=2 if flat else 1)
        y += px(4)

    # Wie viele Zeilen passen? In der Kachel Platz für eine Trendzeile lassen
    reserve = px(40) if compact and data.get("stats", {}).get("trend") else px(10)
    max_rows = max(1, (rh - y - reserve - (0 if compact else px(60))) // row_h)
    wanted_sections = [s for s in data.get("sections", []) if s in SECTION_ORDER] if not compact else []
    if wanted_sections:
        max_rows = min(max_rows, len(stations))
    rows = stations[:max_rows]
    name_w = rw - 2 * margin - primary_w - sec_w * len(secondary) - px(12)

    if not rows:
        draw.text((margin, y + px(8)), data.get("error") or "Keine Stationen gefunden", font=font_sub, fill=pal["muted"])
        y += row_h
    for s in rows:
        mid = y + row_h // 2
        closed = not s.get("is_open", True)
        name_fill = pal["closed"] if closed else pal["text"]
        name = s.get("label") or s.get("name") or "Tankstelle"
        dist = f"{s['dist_km']:.1f} km".replace(".", ",") if s.get("dist_km") is not None else ""
        name_text = _ellipsize(draw, name, font_name, name_w - (draw.textlength("  " + dist, font=font_sub) if dist else 0))
        if compact:
            draw.text((margin, mid - px(14)), name_text, font=font_name, fill=name_fill)
            if dist:
                draw.text((margin + draw.textlength(name_text, font=font_name) + px(8), mid - px(9)), dist, font=font_sub, fill=pal["muted"])
        else:
            draw.text((margin, mid - px(30)), name_text, font=font_name, fill=name_fill)
            if dist:
                draw.text((margin + draw.textlength(name_text, font=font_name) + px(10), mid - px(24)), dist, font=font_sub, fill=pal["muted"])
            addr = " · ".join(p for p in (s.get("street", ""), s.get("place", "")) if p)
            if s.get("label") and s.get("full_name"):
                addr = s["full_name"] + (" · " + addr if addr else "")
            draw.text((margin, mid + px(2)), _ellipsize(draw, addr, font_sub, name_w), font=font_sub, fill=pal["muted"])
        # Preise
        x = rw - margin
        p = s.get("prices", {}).get(primary)
        if closed:
            t = "geschl."
            draw.text((x - draw.textlength(t, font=font_sec), mid - px(14)), t, font=font_sec, fill=pal["closed"])
        else:
            fill = pal["cheap"] if s.get("is_cheapest") else (pal["pricey"] if s.get("is_priciest") else pal["price"])
            draw_price(draw, x, mid - (px(20) if compact else px(28)), p, font_price, font_price_sup, fill)
        x -= primary_w
        for f in secondary:
            v = s.get("prices", {}).get(f)
            if closed or v is None:
                t = "–"
                draw.text((x - draw.textlength(t, font=font_sec), mid - px(14)), t, font=font_sec, fill=pal["closed"])
            else:
                draw_price(draw, x, mid - (px(14) if compact else px(18)), v, font_sec, font_sec_sup, pal["text"])
            x -= sec_w
        y += row_h
        draw.line([(margin, y), (rw - margin, y)], fill=pal["row_line"], width=1)

    # ── Kachel: Trendzeile ───────────────────────────────────────────────────
    if compact:
        trend = data.get("stats", {}).get("trend")
        if trend and y + px(34) <= rh:
            delta = trend["delta_ct"]
            if abs(delta) >= 0.05:
                _triangle(draw, margin + px(8), y + px(18), px(7), delta > 0, pal["up"] if delta > 0 else pal["down"])
                text = f"{format_cents(delta)} ct seit {trend['reference_at']}"
                draw.text((margin + px(24), y + px(6)), text, font=font_sub, fill=pal["up"] if delta > 0 else pal["down"])
            else:
                draw.text((margin, y + px(6)), f"unverändert seit {trend['reference_at']}", font=font_sub, fill=pal["muted"])
        return img.convert("RGB")

    # ── Abschnitte ───────────────────────────────────────────────────────────
    footer_h = px(44)
    y += px(16)
    stats = data.get("stats") or {}
    title_h = px(36)
    plan = _plan_sections(wanted_sections, rh - footer_h - y, px, stats)
    font_title = load_font(px(24), True)
    gap = px(24)
    for row, height in plan:
        top = y
        col_w = (rw - 2 * margin - gap * (len(row) - 1)) / len(row)
        for col, key in enumerate(row):
            left = int(margin + col * (col_w + gap))
            right = int(left + col_w)
            draw.text((left, top), SECTION_TITLES[key], font=font_title, fill=pal["title"])
            box = (left, top + title_h + px(6), right, top + height - px(8))
            if key == "day":
                draw_day_section(draw, box, stats, pal, px, load_font, primary)
            elif key == "week":
                week = stats.get("week") or []
                values = [d["min"] for d in week if d["min"] is not None]
                lowest = min(values) if values else None
                bars = [{"label": ("heute" if i == len(week) - 1 else WEEKDAYS_DE[d["date"].weekday()]), "value": d["min"],
                         "avg": d["avg"], "highlight": d["min"] is not None and lowest is not None and d["min"] - lowest <= 0.0005}
                        for i, d in enumerate(week)]
                draw_bar_section(draw, box, bars, pal, px, load_font, "Die Wochenübersicht entsteht mit den nächsten Tagen.",
                                 today_idx=len(week) - 1)
            elif key == "month":
                draw_month_section(draw, box, stats, pal, px, load_font)
            elif key == "hours":
                draw_hours_section(draw, box, stats, pal, px, load_font, now)
            elif key == "stats":
                draw_stats_section(draw, box, data, stats, pal, px, load_font)
        y += height

    # ── Fußzeile: Quellenangabe (CC BY 4.0) ──────────────────────────────────
    f = load_font(px(19), False)
    attribution = data.get("attribution") or "Daten: Tankerkönig.de / MTS-K · CC BY 4.0"
    draw.text((margin, rh - px(34)), attribution, font=f, fill=pal["muted"])
    return img.convert("RGB")
