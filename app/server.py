"""
Inkwall Webservice – Framework-Orchestrator.

Bindet Module-Registry, Config, Bild-Rendering und Flask-Routes zusammen.
Module werden beim Start automatisch aus dem modules/-Verzeichnis geladen.
"""

from __future__ import annotations

import copy
import dataclasses
import hashlib
import io
import json as _json
import logging
import os
import sys

# `python app/server.py` legt nur app/ auf den Import-Pfad – das Projekt-Root
# muss dazu, sonst schlägt `import app` fehl. `python -m app.server` und
# Gunicorn (wsgi.py) sind davon nicht betroffen.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)
import time
import threading
from datetime import datetime, timedelta, timezone

from flask import Flask, redirect, render_template, request, send_file, jsonify, url_for
from PIL import Image, ImageDraw

from app.logger import get_logger, log_event, redact_secrets, LOGS_DIR

log = get_logger(__name__)

# ── Framework-Module ─────────────────────────────────────────────────────────
from app.config import (
    get_cfg,
    PROJECT_DIR,
    CURRENT_IMAGE_PATH,
    CURRENT_BMP_PATH,
    CURRENT_EPD_PATH,
    STATE_PATH,
    SETTINGS_FIELDS        as FRAMEWORK_SETTINGS_FIELDS,
    SETTINGS_GROUPS        as FRAMEWORK_SETTINGS_GROUPS,
    apply_runtime_config,
    get_settings_values,
    validate_settings,
    write_env_settings,
    should_flip_output,
)
from app.image_rendering import convert_to_spectra6, quantize_spectra6, spectra6_images
from app.epd_format import encode_epd4
import app.module_registry as _registry

# ── Flask-App ────────────────────────────────────────────────────────────────
app = Flask(__name__,
            template_folder=str(PROJECT_DIR / "templates"),
            static_folder=str(PROJECT_DIR / "static"),
            static_url_path="/static")

# Module beim Import laden (deckt sowohl 'python app/server.py' als auch
# 'flask run' und WSGI-Server ab – reload_modules() ist idempotent)
_registry.reload_modules()


# ---------------------------------------------------------------------------
# Optionaler UI-Schutz per Basic Auth (INKWALL_UI_PASSWORD)
# ---------------------------------------------------------------------------

# Endpunkte, die der ESP32 ohne Auth erreichen muss. Der Rest (Dashboard,
# Settings, Logs, alle /api/*) wird geschützt, sobald ein Passwort gesetzt ist.
_PUBLIC_PATHS = ("/hash", "/meta.json", "/current.png", "/current.bmp", "/current.epd", "/ack", "/health",
                 "/firmware.json", "/firmware.bin")
_PUBLIC_PREFIXES = ("/static/",)


def _ui_password() -> str:
    from app.config import process_env
    return process_env("UI_PASSWORD")


@app.before_request
def _require_ui_password():
    password = _ui_password()
    if not password:
        return None
    path = request.path
    if path in _PUBLIC_PATHS or path.startswith(_PUBLIC_PREFIXES):
        return None
    auth = request.authorization
    if auth is not None and auth.type == "basic" and auth.password == password:
        return None
    return (
        "Authentifizierung erforderlich.",
        401,
        {"WWW-Authenticate": 'Basic realm="Inkwall", charset="UTF-8"'},
    )


# ---------------------------------------------------------------------------
# ESP32 API – globaler Render-Zustand
# ---------------------------------------------------------------------------

_esp32_state: dict = {
    "hash":        "",
    "format":      "png",
    "state":       "idle",
    "media_type":  "idle",
    "rendered_at": "",
}
_last_ack: dict = {}
_runtime_lock = threading.Lock()
_runtime_started = False
_worker_thread: threading.Thread | None = None

# Render-Serialisierung: genau ein Render gleichzeitig (Worker ODER Request).
# Requests rendern nicht selbst, sondern wecken den Worker per Event.
_render_lock = threading.Lock()
_wake_event = threading.Event()
_render_cond = threading.Condition()
_render_generation = 0          # zählt abgeschlossene Render-Zyklen
_render_in_progress = False
_force_render_requested = False


def _get_local_now() -> datetime:
    from app.config import now_local
    return now_local()


def _get_schedule_state(now_local: datetime | None = None) -> dict:
    """
    Zeitplan-Zustand: aktives Fenster (gespeicherter Zeitplan oder alter
    Nachtmodus), Sekunden bis zur nächsten Änderung (Ende des aktiven
    Fensters oder Beginn des nächsten) und das nächste beginnende Fenster.
    """
    from app import schedule
    cfg = get_cfg()
    windows = schedule.effective_windows(cfg)
    if not windows:
        return {"active": False, "window": None, "seconds_until_end": 0, "seconds_until_change": 0,
                "label": "", "name": "", "next": None}
    now_local = now_local or _get_local_now()
    active, seconds, upcoming = schedule.active_window(windows, now_local)
    return {
        "active":               active is not None,
        "window":               active,
        "seconds_until_end":    seconds if active is not None else 0,
        "seconds_until_change": seconds,
        "label":                active.label if active is not None else "",
        "name":                 active.name if active is not None else "",
        "next":                 upcoming,
    }


def _with_config(cfg, **changes):
    """Kopie der Config mit geänderten Feldern (RuntimeConfig ist eingefroren)."""
    try:
        return dataclasses.replace(cfg, **changes)
    except TypeError:
        clone = copy.copy(cfg)
        for key, value in changes.items():
            setattr(clone, key, value)
        return clone


def _effective_programme(env: dict[str, str], state: dict | None = None) -> tuple[dict[str, str], object, dict]:
    """
    Programm unter Berücksichtigung des Zeitplans: (env, cfg, Zustand). Ist
    ein Fenster aktiv, ersetzen seine Inhalte, seine Darstellung und sein
    Takt die Werte des Programms – in env für die Module (is_enabled) und in
    cfg für Layout und Dashboard-Kacheln. Leere Fensterfelder erben.
    """
    state = state or _get_schedule_state()
    cfg = get_cfg()
    window = state.get("window")
    if window is None:
        return env, cfg, state
    env = dict(env)
    changes: dict = {}
    if window.content:
        env["IDLE_MODULES"] = ",".join(window.module_ids)
        env["DASHBOARD_TILES"] = ", ".join(f"{m}:{p}" if p else m for m, p in window.content)
        changes["dashboard_tiles"] = tuple(window.content)
    if window.layout:
        env["IDLE_LAYOUT"] = window.layout
        changes["idle_layout"] = window.layout
    if window.interval_seconds:
        env["IDLE_MODULE_ROTATION_SECONDS"] = str(window.interval_seconds)
        changes["idle_module_rotation_seconds"] = int(window.interval_seconds)
    return env, (_with_config(cfg, **changes) if changes else cfg), state


def _format_interval(seconds: int) -> str:
    if seconds % 3600 == 0:
        return f"{seconds // 3600} h"
    if seconds % 60 == 0:
        return f"{seconds // 60} min"
    return f"{seconds} s"


def _apply_schedule_interval(base_seconds: int, base_reason: str) -> tuple[int, str]:
    """
    Wake-Intervall eines Idle-Bilds mit Zeitplan: im Fenster gilt dessen Takt
    (nie schneller als das Modul verlangt), an der Fenstergrenze wird
    pünktlich geweckt – beim Ende des aktiven wie beim Beginn des nächsten.
    """
    state = _get_schedule_state()
    window = state.get("window")
    base = max(10, int(base_seconds))
    if window is not None:
        effective = max(base, int(window.interval_seconds or base))
        until = int(state.get("seconds_until_end", 0) or 0)
        if 0 < until < effective:
            return max(10, until), f"Zeitfenster „{window.name}“ endet um {window.end_text} – pünktlicher Wechsel"
        return effective, f"Zeitfenster „{window.name}“ ({window.label}) – alle {_format_interval(effective)}"
    upcoming = state.get("next")
    until = int(state.get("seconds_until_change", 0) or 0)
    if upcoming is not None and 0 < until < base:
        return max(10, until), f"Zeitfenster „{upcoming.name}“ beginnt um {upcoming.start_text} – pünktlicher Wechsel"
    return base, base_reason


def _get_effective_idle_modules(env: dict[str, str]) -> tuple[list, int]:
    """Idle-Module und Takt, die jetzt gelten: das Programm oder das aktive Zeitfenster."""
    raw_env = env
    env, cfg, state = _effective_programme(env)
    enabled_idle = [m for m in _registry.get_idle_modules() if m.is_enabled(env)]
    rotation = max(int(getattr(cfg, "idle_module_rotation_seconds", 120)), 1)
    window = state.get("window")
    if window is None or not window.content:
        return enabled_idle, rotation
    ids = list(window.module_ids)
    chosen = sorted((m for m in enabled_idle if m.MODULE_ID in ids), key=lambda m: ids.index(m.MODULE_ID))
    if chosen:
        return chosen, rotation
    log.warning(f"Zeitfenster „{window.name}“: keiner seiner Inhalte ist bereit – das Programm läuft weiter")
    return [m for m in _registry.get_idle_modules() if m.is_enabled(raw_env)], rotation


def _compute_image_hash() -> str:
    cfg  = get_cfg()
    path = CURRENT_BMP_PATH if cfg.output_format == "bmp" else CURRENT_IMAGE_PATH
    if not path.exists():
        return ""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _is_priority_media_type(media_type: str) -> bool:
    mod = _registry.get_module_by_id(media_type)
    return bool(mod is not None and mod.MODULE_PRIORITY < 10)


ROTATION_WAKE_MARGIN_S = 15     # so viele Sekunden nach der Slot-Grenze soll das Gerät das neue Bild holen
DEFAULT_DEVICE_CYCLE_S = 40     # Aufwachen → WLAN → Download → Anzeige → ACK, solange keine Messung da ist
MIN_WAKE_S = 10


def _device_cycle_seconds() -> int:
    """Dauer eines Gerätezyklus aus der letzten Rückmeldung (cycle_ms), sonst der Standardwert."""
    try:
        seconds = int(_last_ack.get("cycle_ms")) / 1000.0
    except (TypeError, ValueError):
        return DEFAULT_DEVICE_CYCLE_S
    return int(min(180, max(5, round(seconds))))


def _aligned_rotation_wake(rotation_seconds: int, now: float | None = None, cycle_seconds: int | None = None) -> tuple[int, float]:
    """
    Schlafzeit, mit der das Gerät ROTATION_WAKE_MARGIN_S nach der nächsten Slot-Grenze
    das Bild holt – statt „Rotationstakt“ zu schlafen und mit jedem Zyklus um die eigene
    Zyklusdauer zu driften (dann verpasst es Bilder oder holt das alte kurz vor dem Wechsel).
    Gibt (Sekunden, Zeitstempel der Grenze) zurück.
    """
    now = time.time() if now is None else now
    cycle = _device_cycle_seconds() if cycle_seconds is None else cycle_seconds
    rotation = max(int(rotation_seconds), 1)
    boundary = (int(now // rotation) + 1) * rotation
    seconds = boundary + ROTATION_WAKE_MARGIN_S - cycle - now
    if seconds < MIN_WAKE_S:
        # Die nächste Grenze ist mit dem laufenden Zyklus nicht mehr zu schaffen – die übernächste
        boundary += rotation
        seconds += rotation
    return int(round(seconds)), float(boundary)


def _rotation_wake() -> tuple[int, str]:
    """Wake-Intervall eines Idle-Bilds: Slot-Grenze der Rotation, Zeitplan-Grenzen gehen vor."""
    from app.config import local_tz
    _, rotation = _get_effective_idle_modules(get_settings_values())
    seconds, boundary = _aligned_rotation_wake(rotation)
    at = datetime.fromtimestamp(boundary, local_tz()).strftime("%H:%M")
    state = _get_schedule_state()
    window = state.get("window")
    if window is not None:
        until = int(state.get("seconds_until_end", 0) or 0)
        if 0 < until < seconds:
            return max(MIN_WAKE_S, until), f"Zeitfenster „{window.name}“ endet um {window.end_text} – pünktlicher Wechsel"
        return seconds, f"Zeitfenster „{window.name}“ – nächstes Bild um {at}"
    upcoming = state.get("next")
    until = int(state.get("seconds_until_change", 0) or 0)
    if upcoming is not None and 0 < until < seconds:
        return max(MIN_WAKE_S, until), f"Zeitfenster „{upcoming.name}“ beginnt um {upcoming.start_text} – pünktlicher Wechsel"
    return seconds, f"Idle-Rotation – nächstes Bild um {at}"


def _suggest_next_wake(state: str, media_type: str) -> tuple[int, str]:
    cfg = get_cfg()
    if media_type == "plex":
        state_parts = state.split(":")
        player_state = state_parts[2] if len(state_parts) >= 3 else (state_parts[1] if len(state_parts) >= 2 else "unknown")
        if player_state in ("playing", "buffering"):
            return cfg.refresh_interval, f"Plex {player_state} – Refresh-Intervall"
        if player_state == "paused":
            return cfg.refresh_interval * 3, "Plex pausiert – verlangsamter Refresh"
        return cfg.refresh_interval * 5, "Plex unbekannt/inaktiv – konservativer Fallback"

    if state in ("idle", "__no_content__"):
        return _apply_schedule_interval(cfg.idle_module_rotation_seconds, "Kein aktiver Inhalt – Idle-Rotation")

    mod = _registry.get_module_by_id(media_type)
    if mod is not None:
        env = get_settings_values()
        info = mod.get_next_wake_info(env, state)
        if info is not None:
            seconds = max(10, int(info.get("seconds", cfg.idle_module_rotation_seconds)))
            reason = str(info.get("reason", f"{mod.MODULE_NAME} bestimmt das Wake-Intervall")).strip()
            if mod.MODULE_PRIORITY < 10:
                return seconds, reason
            return _apply_schedule_interval(seconds, reason)
        custom = mod.get_next_wake_seconds(env, state)
        if custom is not None:
            if mod.MODULE_PRIORITY < 10:
                return max(10, int(custom)), f"{mod.MODULE_NAME} bestimmt das Wake-Intervall"
            return _apply_schedule_interval(max(10, int(custom)), f"{mod.MODULE_NAME} bestimmt das Wake-Intervall")
        if mod.MODULE_PRIORITY < 10:
            return cfg.refresh_interval, f"{mod.MODULE_NAME} aktiv – Refresh-Intervall"

    return _rotation_wake()


# ---------------------------------------------------------------------------
# Placeholder-Bild (kein Modul aktiv)
# ---------------------------------------------------------------------------

def render_no_content_image() -> Image.Image:
    """Platzhalterbild wenn kein einziges Modul aktiven Inhalt liefert."""
    from app.config import load_font
    cfg = get_cfg()
    w, h = cfg.render_width, cfg.render_height

    if cfg.display_theme == "eink":
        from app.image_rendering import SPECTRA6_COLORS
        bg_col     = SPECTRA6_COLORS["white"]
        text_col   = SPECTRA6_COLORS["black"]
        muted_col  = SPECTRA6_COLORS["blue"]
        border_col = SPECTRA6_COLORS["black"]
    elif cfg.display_theme == "light":
        bg_col     = (238, 234, 228)
        text_col   = (24, 20, 14)
        muted_col  = (110, 101, 92)
        border_col = (200, 195, 188)
    else:
        bg_col     = (15, 15, 14)
        text_col   = (240, 237, 232)
        muted_col  = (125, 117, 108)
        border_col = (42, 42, 40)

    img  = Image.new("RGB", (w, h), bg_col)
    draw = ImageDraw.Draw(img)

    box_w, box_h = min(680, w - 80), 280
    bx = (w - box_w) // 2
    by = (h - box_h) // 2
    draw.rounded_rectangle((bx, by, bx + box_w, by + box_h),
                            radius=24, outline=border_col, width=1)

    cx        = w // 2
    font_head = load_font(36, True)
    font_sub  = load_font(22, False)
    font_hint = load_font(18, False)

    draw.text((cx, by + 72),  "Noch kein Inhalt eingeschaltet",
              font=font_head, fill=text_col, anchor="mm")
    draw.text((cx, by + 130), "Öffne die Weboberfläche im Browser",
              font=font_sub, fill=muted_col, anchor="mm")
    draw.text((cx, by + 160), "und schalte unter „Anzeige“ einen Inhalt ein.",
              font=font_sub, fill=muted_col, anchor="mm")
    draw.text((cx, by + 220), "Quellen wie Wetter, Kalender oder Müllabfuhr richtest du unter „Inhalte“ ein.",
              font=font_hint, fill=muted_col, anchor="mm")
    return img


# ---------------------------------------------------------------------------
# Bild speichern + ESP32-State aktualisieren
# ---------------------------------------------------------------------------

def _atomic_write_bytes(path, data: bytes) -> None:
    """Schreibt in eine tmp-Datei und tauscht atomar. Leser sehen nie halbe Dateien."""
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _encode_image(image: Image.Image, fmt: str) -> bytes:
    buf = io.BytesIO()
    image.save(buf, fmt)
    return buf.getvalue()


def _save_image(image: Image.Image, state_key: str, module_id: str) -> None:
    global _esp32_state
    cfg = get_cfg()

    # Optional: Uhrzeit auf jeder Seite (das Dashboard hat sie selbst)
    if getattr(cfg, "show_render_time", False) and module_id != "dashboard":
        from app.image_rendering import stamp_render_time
        image = stamp_render_time(image, cfg.display_theme, f"Stand {_get_local_now():%H:%M}")

    if should_flip_output(cfg.display_rotation):
        image = image.transpose(Image.Transpose.ROTATE_180)

    # Alle Bytes zuerst im Speicher erzeugen, Hash daraus berechnen,
    # dann atomar schreiben. Erst danach den ESP32-State umschalten,
    # damit /meta.json nie einen neuen Hash zu einer alten Datei liefert.
    if cfg.output_format == "bmp":
        quantized = quantize_spectra6(image)
        device_img, preview_img = spectra6_images(quantized)
        device_bytes  = _encode_image(device_img, "BMP")
        preview_bytes = _encode_image(preview_img, "PNG")
        epd_bytes     = encode_epd4(quantized)      # kompaktes 4-bpp-Format, gleicher Inhalt
        image_hash = hashlib.md5(device_bytes).hexdigest()
        files = [(CURRENT_IMAGE_PATH, preview_bytes), (CURRENT_BMP_PATH, device_bytes), (CURRENT_EPD_PATH, epd_bytes)]
    else:
        png_bytes = _encode_image(image, "PNG")
        image_hash = hashlib.md5(png_bytes).hexdigest()
        files = [(CURRENT_IMAGE_PATH, png_bytes)]

    # Gleiches Bild wie zuletzt (typisch: Rotations-Slot wechselt, Inhalt nicht):
    # Dateien nicht neu schreiben, nur den State-Key nachziehen. Der ESP32
    # sieht denselben Hash und lädt nichts.
    unchanged = image_hash == _esp32_state.get("hash") and all(p.exists() for p, _ in files)
    if not unchanged:
        for path, data in files:
            _atomic_write_bytes(path, data)

    _atomic_write_bytes(STATE_PATH, state_key.encode("utf-8"))

    previous_module = str(_esp32_state.get("media_type", ""))
    _esp32_state = {
        "hash":        image_hash,
        "format":      cfg.output_format,
        "state":       state_key,
        "media_type":  module_id,
        "rendered_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    log.log(
        logging.DEBUG if unchanged else logging.INFO,
        f"Rendered [{module_id}] state={state_key[:40]} "
        f"theme={cfg.display_theme} fmt={cfg.output_format} "
        f"hash={_esp32_state['hash'][:8]}…{' (unverändert)' if unchanged else ''}"
    )
    mod = _registry.get_module_by_id(module_id)
    shown = "Dashboard" if module_id == "dashboard" else (mod.MODULE_NAME if mod else ("Kein Inhalt" if module_id == "none" else module_id))
    if module_id != previous_module and previous_module not in ("", "idle"):
        log_event("switch", f"Display zeigt jetzt: {shown}")
    if not unchanged:
        # Verlauf: nur echte neue Bilder, nicht die „unverändert“-Durchläufe
        from app import history, monitoring
        history.record(image, module_id, shown, image_hash)
        monitoring.increment("inkwall_renders_total", module=module_id)


# ---------------------------------------------------------------------------
# Kern-Rendering-Logik (wird von periodic_worker + /refresh genutzt)
# ---------------------------------------------------------------------------

def render_if_changed(last_state_key: str | None) -> str | None:
    """
    Iteriert Module in Prioritätsreihenfolge, rendert wenn Inhalt vorhanden
    und der State-Key sich geändert hat (oder should_refresh() True ist).
    Gibt den aktuellen State-Key zurück. Schlägt das Rendern fehl, wird der
    alte State-Key zurückgegeben, damit der nächste Tick es erneut versucht.

    Läuft vollständig unter _render_lock: Worker und Requests können nie
    gleichzeitig rendern oder schreiben.
    """
    with _render_lock:
        return _render_if_changed_locked(last_state_key)


def _render_if_changed_locked(last_state_key: str | None) -> str | None:
    env = get_settings_values()

    # ── 1. Prioritätsmodule (MODULE_PRIORITY < 10, z. B. Plex) ───────────────
    for mod in _registry.get_priority_modules():
        if not mod.is_enabled(env):
            continue
        try:
            content = mod.fetch_content(env)
        except Exception as exc:
            log.error(f"fetch_content [{mod.MODULE_ID}]: {exc}", exc_info=True)
            continue

        if content is None:
            continue

        state_key = f"{mod.MODULE_ID}:{mod.get_state_key(content)}"
        if state_key != last_state_key or mod.should_refresh(env):
            try:
                image = mod.render(env, content)
                _save_image(image, state_key, mod.MODULE_ID)
            except Exception as exc:
                log.error(f"render [{mod.MODULE_ID}]: {exc}", exc_info=True)
                _count_render_error(mod.MODULE_ID)
                return last_state_key
        return state_key

    # ── 2a. Dashboard: alle aktiven Idle-Module in einem Bild ─────────────────
    # Zeitplan: env und cfg tragen die Werte des aktiven Fensters (Inhalte, Layout, Takt)
    enabled_idle, rotation_seconds = _get_effective_idle_modules(env)
    env, cfg, _ = _effective_programme(env)
    if enabled_idle and cfg.idle_layout == "dashboard":
        from app.dashboard import compose_dashboard
        try:
            result = compose_dashboard(env, cfg, _dashboard_modules(enabled_idle, cfg))
        except Exception as exc:
            log.error(f"dashboard: {exc}", exc_info=True)
            result = None
        if result is not None:
            image, state_key = result
            needs_refresh = any(m.should_refresh(env) for m in enabled_idle)
            if state_key != last_state_key or needs_refresh:
                try:
                    _save_image(image, state_key, "dashboard")
                except Exception as exc:
                    log.error(f"render [dashboard]: {exc}", exc_info=True)
                    _count_render_error("dashboard")
                    return last_state_key
            return state_key
        # kein Inhalt in keiner Kachel → normale Rotation als Fallback

    # ── 2b. Idle-Module in Rotation (MODULE_PRIORITY >= 10) ──────────────────
    if enabled_idle:
        slot = int(time.time() // rotation_seconds)
        sequence = _rotation_sequence(enabled_idle, env)

        for offset in range(len(sequence)):
            mod = sequence[(slot + offset) % len(sequence)]
            try:
                content = mod.fetch_content(env)
            except Exception as exc:
                log.error(f"fetch_content [{mod.MODULE_ID}]: {exc}", exc_info=True)
                continue

            if content is None:
                continue

            state_key = f"{mod.MODULE_ID}:{mod.get_state_key(content)}:{slot}"
            if state_key != last_state_key or mod.should_refresh(env):
                try:
                    image = mod.render(env, content)
                    _save_image(image, state_key, mod.MODULE_ID)
                except Exception as exc:
                    log.error(f"render [{mod.MODULE_ID}]: {exc}", exc_info=True)
                    _count_render_error(mod.MODULE_ID)
                    return last_state_key
            return state_key

    # ── 3. Kein Modul hat Inhalt → Placeholder ───────────────────────────────
    if last_state_key != "__no_content__":
        try:
            _save_image(render_no_content_image(), "__no_content__", "none")
        except Exception as exc:
            log.error(f"render placeholder: {exc}", exc_info=True)
            return last_state_key
    return "__no_content__"


def _count_render_error(module_id: str) -> None:
    from app import monitoring
    monitoring.increment("inkwall_render_errors_total", module=module_id)


def _rotation_sequence(enabled_idle: list, env: dict[str, str]) -> list:
    """
    Reihenfolge der Idle-Rotation. Dringende Module (is_urgent) kommen vor
    jedes andere Modul: [Müll, Wetter, Müll, News] – so ist die Erinnerung
    jedes zweite Bild da, ohne den Rest ganz zu verdrängen.
    """
    urgent: list = []
    for mod in enabled_idle:
        try:
            if mod.is_urgent(env):
                urgent.append(mod)
        except Exception as exc:
            log.warning(f"is_urgent [{mod.MODULE_ID}]: {exc}")
    if not urgent:
        return list(enabled_idle)
    rest = [m for m in enabled_idle if m not in urgent]
    if not rest:
        return urgent
    sequence: list = []
    for mod in rest:
        sequence.extend(urgent)
        sequence.append(mod)
    return sequence


def _dashboard_modules(enabled_idle: list, cfg) -> list:
    """Aktive Idle-Module in der Reihenfolge der Kachel-Konfiguration (Rest hinten dran)."""
    by_id = {m.MODULE_ID: m for m in enabled_idle}
    ordered = [by_id[mid] for mid, _ in cfg.dashboard_tiles if mid in by_id]
    if cfg.dashboard_tiles:
        return ordered
    return list(enabled_idle)


def render_image() -> str | None:
    """Erzwingt einen synchronen Neu-Render (ignoriert last_state_key). Gibt State-Key zurück."""
    return render_if_changed(None)   # None != irgendein str → immer neu


def request_render(wait_seconds: float = 0.0, reason: str = "") -> bool:
    """
    Fordert vom Worker einen erzwungenen Render an, ohne selbst zu rendern.
    Mit wait_seconds > 0 wird auf den Abschluss gewartet. Gibt True zurück,
    wenn der Render innerhalb der Wartezeit abgeschlossen wurde.
    """
    global _force_render_requested
    log.info(f"Render angefordert: {reason or 'ohne Angabe'}")
    with _render_cond:
        _force_render_requested = True
        # Läuft gerade ein Render, enthält er unsere Anforderung noch nicht:
        # dann erst der übernächste Abschluss zählt.
        target = _render_generation + (2 if _render_in_progress else 1)
        _wake_event.set()
        if wait_seconds <= 0:
            return False
        return _render_cond.wait_for(lambda: _render_generation >= target, timeout=wait_seconds)


def _get_background_poll_seconds() -> int:
    cfg = get_cfg()
    env = get_settings_values()
    candidates = [max(1, int(cfg.refresh_interval))]

    for mod in _registry.get_modules():
        if not mod.is_enabled(env):
            continue
        custom = mod.get_background_poll_seconds(env)
        if custom is not None:
            candidates.append(max(1, int(custom)))

    base_poll = min(candidates)
    if _is_priority_media_type(_esp32_state.get("media_type", "")):
        return base_poll

    # Zeitplan: an der nächsten Fenstergrenze (Ende oder Beginn) pünktlich neu rendern
    until = int(_get_schedule_state().get("seconds_until_change", 0) or 0)
    if until > 0:
        base_poll = min(base_poll, max(1, until))
    # Idle-Rotation: das nächste Bild entsteht direkt an der Slot-Grenze, nicht erst
    # beim nächsten Poll – das Gerät holt es ROTATION_WAKE_MARGIN_S danach ab
    enabled_idle, rotation = _get_effective_idle_modules(env)
    if enabled_idle and rotation > 1:
        until_slot = rotation - (time.time() % rotation)
        base_poll = min(base_poll, max(1, int(until_slot) + 1))
    return base_poll


# ---------------------------------------------------------------------------
# Background-Worker
# ---------------------------------------------------------------------------

def _run_worker_cycle(last_state_key: str | None) -> str | None:
    """Ein Worker-Durchlauf: erzwungen (wenn angefordert) oder normal."""
    global _render_generation, _render_in_progress, _force_render_requested

    with _render_cond:
        forced = _force_render_requested
        _force_render_requested = False
        _render_in_progress = True

    try:
        try:
            last_state_key = render_if_changed(None if forced else last_state_key)
        except Exception as exc:
            log.error(f"periodic_worker: {exc}", exc_info=True)
    finally:
        with _render_cond:
            _render_in_progress = False
            _render_generation += 1
            _render_cond.notify_all()

    return last_state_key


def _notifier():
    from app.notifications import Notifier
    cfg = get_cfg()
    return Notifier(cfg.notify_url, cfg.notify_events, cfg.notify_offline_minutes, cfg.notify_daily_hour,
                    cfg.notify_base_url, cfg.notify_avatar_url)


def _stale_sources(env: dict[str, str]) -> list:
    """[(modul, Name, seit wann aus dem Cache), …] – Inhalte, die gerade nur den gespeicherten Stand zeigen."""
    stale: list = []
    for mod in _registry.get_idle_modules():
        try:
            if not mod.is_enabled(env):
                continue
            content = mod.fetch_content(env)
            raw = (content or {}).get("stale_since") if isinstance(content, dict) else ""
            if raw:
                stale.append((mod.MODULE_ID, mod.MODULE_NAME, datetime.fromisoformat(str(raw))))
        except Exception as exc:
            log.debug(f"stale check [{mod.MODULE_ID}]: {exc}")
    return stale


def _current_summary() -> dict:
    mod = _registry.get_module_by_id(str(_esp32_state.get("media_type", "")))
    media = str(_esp32_state.get("media_type", ""))
    name = "Dashboard" if media == "dashboard" else (mod.MODULE_NAME if mod else ("Kein Inhalt" if media in ("none", "") else media))
    return {"module_name": name, "rendered_at": _esp32_state.get("rendered_at", "")}


_NOTIFY_LOG = {
    "offline": ("meldet sich nicht mehr – Benachrichtigung verschickt", logging.WARNING),
    "online": ("ist wieder da – Entwarnung verschickt", logging.INFO),
    "firmware": ("läuft mit neuer Firmware – Nachricht verschickt", logging.INFO),
    "rollback": ("hat die Firmware zurückgerollt – Nachricht verschickt", logging.WARNING),
    "errors": ("meldet wiederholt Fehler – Nachricht verschickt", logging.WARNING),
    "source_down": ("Quelle nicht erreichbar – Nachricht verschickt", logging.WARNING),
    "source_up": ("Quelle wieder erreichbar – Nachricht verschickt", logging.INFO),
    "daily": ("Tagesbild verschickt", logging.INFO),
    "weekly": ("Wochenbericht verschickt", logging.INFO),
}


def _log_notifications(kinds: list) -> None:
    from app import monitoring
    device = _last_ack.get("device_id") or "Gerät"
    for kind in kinds:
        monitoring.increment("inkwall_notifications_total", kind=kind)
        text, level = _NOTIFY_LOG.get(kind, ("Nachricht verschickt", logging.INFO))
        log_event("device", f"{device}: {text}" if kind in ("offline", "online", "firmware", "rollback", "errors") else text, level)


def _check_device_offline() -> None:
    """Nach jedem Worker-Durchlauf: Ausfall, Quellen, Tagesbild, Wochenbericht."""
    try:
        from app import monitoring
        notifier = _notifier()
        if not notifier.url:
            return
        env = get_settings_values()
        sent = notifier.on_cycle(
            _last_ack, _get_local_now(),
            stale_sources=_stale_sources(env) if "sources" in notifier.events else [],
            current=_current_summary(),
            weekly_stats=lambda: monitoring.ack_stats(monitoring.read_ack_history(limit=5000, hours=24 * 7),
                                                      _suggest_next_wake(_esp32_state.get("state", "idle"), _esp32_state.get("media_type", "idle"))[0]),
        )
        _log_notifications(sent)
    except Exception as exc:
        log.warning(f"Benachrichtigungen: {exc}")


def periodic_worker() -> None:
    last_state_key: str | None = None

    while True:
        last_state_key = _run_worker_cycle(last_state_key)
        _check_device_offline()
        # Warten bis zum nächsten Poll oder bis ein Request den Worker weckt
        _wake_event.wait(timeout=_get_background_poll_seconds())
        _wake_event.clear()


# ---------------------------------------------------------------------------
# Settings-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _build_settings_sections() -> list[dict]:
    """
    Gibt eine geordnete Liste von Section-Dicts zurück:
    [{key, title, eyebrow, desc, fields, groups}, …]
    Framework-Section zuerst, dann alle Module in Prioritätsreihenfolge.
    """
    # Idle-Modul-Optionen für IDLE_MODULES-Checkbox dynamisch befüllen
    idle_opts = [
        (m.MODULE_ID, m.MODULE_NAME)
        for m in _registry.get_idle_modules()
    ]
    framework_fields = []
    for f in FRAMEWORK_SETTINGS_FIELDS:
        field = dict(f)
        if field["name"] == "IDLE_MODULES" and not field.get("options"):
            field = dict(field, options=idle_opts)
        if field["name"] == "NIGHT_MODE_FIXED_MODULE" and not field.get("options"):
            field = dict(field, options=[("", "– Bitte wählen –"), *idle_opts])
        framework_fields.append(field)

    sections = [
        {
            "key":     "framework",
            "title":   "Kern-Einstellungen",
            "eyebrow": "Framework",
            "desc":    (
                "Render-Grundkonfiguration: Bildgröße, Rotation, Theme, Ausgabeformat "
                "und die Verwaltung der Idle-Module."
            ),
            "fields":  framework_fields,
            "groups":  FRAMEWORK_SETTINGS_GROUPS,
        }
    ]

    for mod in _registry.get_modules():
        mod_fields = [dict(f, section=mod.MODULE_ID) for f in mod.SETTINGS_FIELDS]
        sections.append({
            "key":      mod.MODULE_ID,
            "title":    mod.MODULE_NAME,
            "eyebrow":  "Modul" if mod.MODULE_PRIORITY >= 10 else "Prioritätsmodul",
            "desc":     mod.MODULE_DESCRIPTION,
            "fields":   mod_fields,
            "groups":   list(mod.SETTINGS_GROUPS),
            "priority": mod.MODULE_PRIORITY,
        })

    return sections


def _all_fields_from_sections(sections: list[dict]) -> list[dict]:
    """Flache Liste aller Felder aus allen Sections."""
    fields: list[dict] = []
    seen: set[str] = set()
    for sec in sections:
        for f in sec["fields"]:
            if f["name"] not in seen:
                fields.append(f)
                seen.add(f["name"])
    return fields


def _build_module_health() -> dict[str, dict]:
    env = get_settings_values()
    health: dict[str, dict] = {}
    for mod in _registry.get_modules():
        try:
            payload = mod.get_health_status(env)
        except Exception as exc:
            log.warning(f"module_health [{mod.MODULE_ID}]: {exc}")
            payload = {"ok": False, "error": str(exc)}
        if payload is not None:
            health[mod.MODULE_ID] = payload
    return health


def _build_effective_settings(updates: dict[str, str]) -> dict[str, str]:
    env = get_settings_values()
    env.update(updates)
    return env


def _validate_all_settings(updates: dict[str, str], all_fields: list[dict]) -> list[str]:
    errors = list(validate_settings(updates, all_fields))
    effective_env = _build_effective_settings(updates)
    for mod in _registry.get_modules():
        try:
            errors.extend(mod.validate_settings(updates, effective_env))
        except Exception as exc:
            log.error(f"validate_settings [{mod.MODULE_ID}]: {exc}", exc_info=True)
            errors.append(f"{mod.MODULE_NAME}: Fehler in der Modul-Validierung.")
    return errors


# ---------------------------------------------------------------------------
# Flask-Routes
# ---------------------------------------------------------------------------

@app.route("/health", methods=["GET"])
def health():
    from app.config import APP_VERSION
    return jsonify({
        "ok": True,
        "version": APP_VERSION,
        "ui_password": bool(_ui_password()),
        "modules": _build_module_health(),
    })


@app.route("/logo.png", methods=["GET"])
def logo_png():
    return send_file(str(PROJECT_DIR / "logo.png"), mimetype="image/png", max_age=3600)


@app.route("/favicon.png", methods=["GET"])
def favicon_png():
    return send_file(str(PROJECT_DIR / "logo.png"), mimetype="image/png", max_age=3600)


@app.route("/current.png", methods=["GET"])
def current_png():
    if not CURRENT_IMAGE_PATH.exists():
        try:
            render_image()
        except Exception as exc:
            log.error(f"current_png render: {exc}", exc_info=True)
            return "Bild konnte nicht gerendert werden.", 500
    return send_file(str(CURRENT_IMAGE_PATH), mimetype="image/png", max_age=0)


@app.route("/current.bmp", methods=["GET"])
def current_bmp():
    cfg = get_cfg()
    if cfg.output_format != "bmp":
        return "BMP-Ausgabe ist nicht aktiv. Bitte OUTPUT_FORMAT=bmp setzen.", 404
    if not CURRENT_BMP_PATH.exists():
        try:
            render_image()
        except Exception as exc:
            log.error(f"current_bmp render: {exc}", exc_info=True)
            return "BMP konnte nicht gerendert werden.", 500
    return send_file(str(CURRENT_BMP_PATH), mimetype="image/bmp", max_age=0)


@app.route("/current.epd", methods=["GET"])
def current_epd():
    """Kompaktes 4-bpp-Bild (siehe app/epd_format.py). Nur bei OUTPUT_FORMAT=bmp."""
    cfg = get_cfg()
    if cfg.output_format != "bmp":
        return "Das kompakte Format gibt es nur mit OUTPUT_FORMAT=bmp.", 404
    if not CURRENT_EPD_PATH.exists():
        try:
            render_image()
        except Exception as exc:
            log.error(f"current_epd render: {exc}", exc_info=True)
            return "Bild konnte nicht gerendert werden.", 500
        if not CURRENT_EPD_PATH.exists():
            return "Noch kein Bild.", 404
    return send_file(str(CURRENT_EPD_PATH), mimetype="application/octet-stream", max_age=0)


# ── Render-Historie ──────────────────────────────────────────────────────────

@app.route("/api/history", methods=["GET"])
def api_history():
    from app import history
    return jsonify({"entries": history.list_entries(), "keep": history.HISTORY_KEEP})


@app.route("/api/history", methods=["DELETE"])
def api_history_delete():
    from app import history
    removed = history.clear()
    log_event("history", f"Verlauf geleert ({removed} Bilder)")
    return jsonify({"ok": True, "removed": removed})


@app.route("/history/<entry_id>.png", methods=["GET"])
def history_png(entry_id: str):
    from app import history
    path = history.image_path(entry_id)
    if path is None:
        return "Unbekannter Verlaufseintrag.", 404
    return send_file(str(path), mimetype="image/png", max_age=86400)


# ── Modul-Vorschau ───────────────────────────────────────────────────────────

def render_module_preview(module_id: str, theme: str | None = None, device: bool = False) -> Image.Image | None:
    """
    Rendert ein einzelnes Modul on-demand, ohne das Display-Bild anzufassen.
    theme überschreibt DISPLAY_THEME nur für diesen Render. device=True
    liefert die 6-Farben-Vorschau, wie sie das Spectra-6-Display zeigt.
    Gibt None zurück, wenn das Modul gerade keinen Inhalt hat.
    """
    from app.config import override_runtime_config

    mod = _registry.get_module_by_id(module_id)
    if mod is None and module_id != "dashboard":
        raise LookupError(module_id)

    with _render_lock:
        changes = {"display_theme": theme} if theme else {}
        with override_runtime_config(**changes):
            env = get_settings_values()
            if module_id == "dashboard":
                from app.dashboard import compose_dashboard
                env, cfg, _ = _effective_programme(env)
                enabled_idle = [m for m in _registry.get_idle_modules() if m.is_enabled(env)]
                result = compose_dashboard(env, cfg, _dashboard_modules(enabled_idle, cfg))
                if result is None:
                    return None
                image = result[0]
            else:
                content = mod.fetch_content(env)
                if content is None:
                    return None
                image = mod.render(env, content)

    if device:
        _, image = convert_to_spectra6(image)
    return image


@app.route("/api/preview/<module_id>.png", methods=["GET"])
def api_preview(module_id: str):
    from app.config import AVAILABLE_THEMES
    theme = (request.args.get("theme") or "").strip().lower() or None
    if theme and theme not in AVAILABLE_THEMES:
        return jsonify({"ok": False, "error": f"Unbekanntes Theme: {theme}"}), 400
    device = request.args.get("device", "").strip().lower() in ("1", "true", "yes")

    try:
        image = render_module_preview(module_id, theme=theme, device=device)
    except LookupError:
        return jsonify({"ok": False, "error": "Unbekanntes Modul"}), 404
    except Exception as exc:
        log.error(f"preview [{module_id}]: {exc}", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500

    if image is None:
        return jsonify({"ok": False, "error": "Modul liefert gerade keinen Inhalt"}), 404

    buf = io.BytesIO()
    image.save(buf, "PNG")
    buf.seek(0)
    response = send_file(buf, mimetype="image/png", max_age=0)
    response.headers["Cache-Control"] = "no-store"
    return response


# ── ESP32-Endpunkte ──────────────────────────────────────────────────────────

@app.route("/hash", methods=["GET"])
def image_hash():
    h = _esp32_state.get("hash") or _compute_image_hash()
    return h, 200, {"Content-Type": "text/plain; charset=utf-8"}


@app.route("/meta.json", methods=["GET"])
def meta_json():
    state = _esp32_state.get("state", "idle")
    fmt   = _esp32_state.get("format", get_cfg().output_format)
    media_type = _esp32_state.get("media_type", "idle")
    next_wake_sec, next_wake_reason = _suggest_next_wake(state, media_type)
    from app.device import firmware_info
    payload = {
        "hash":          _esp32_state.get("hash", ""),
        "format":        fmt,
        "state":         state,
        "media_type":    media_type,
        "rendered_at":   _esp32_state.get("rendered_at", ""),
        "next_wake_sec": next_wake_sec,
        "next_wake_reason": next_wake_reason,
        "image_url":     f"/current.{fmt}",
    }
    # Kompaktes Format: das Gerät bevorzugt es, wenn es angeboten wird
    if fmt == "bmp" and CURRENT_EPD_PATH.exists():
        payload["epd_url"] = "/current.epd"
        payload["epd_size"] = CURRENT_EPD_PATH.stat().st_size
    fw = firmware_info()
    if fw:
        payload["firmware_version"] = fw["version"]
        payload["firmware_md5"] = fw["md5"]
        payload["firmware_size"] = fw["size"]
        payload["firmware_url"] = fw["url"]
    # Uhrzeit fürs Gerät (Offline-Balken "seit HH:MM"), Panelreinigung, Test des Hinweises
    from app.device import clean_due, consume_test_banner
    now = _get_local_now()
    cfg = get_cfg()
    payload["epoch"] = int(now.timestamp())
    payload["tz_offset_sec"] = int((now.utcoffset() or timedelta(0)).total_seconds())
    payload["clean_due"] = clean_due(now, cfg.panel_clean_interval_days, cfg.panel_clean_hour)
    # Einmalig: mit der Auslieferung verbraucht, damit ein Geraet, das dabei haengen
    # bleibt, den Auftrag nicht bei jedem Neustart wieder bekommt
    if consume_test_banner():
        payload["show_offline_test"] = True
    return jsonify(payload)


@app.route("/ack", methods=["POST"])
def ack():
    """
    Rückmeldung des Geräts am Ende jedes Zyklus: Hash, Ergebnis (updated,
    unchanged, error, ota), Gesundheitsdaten (RSSI, Firmware, Zeiten) und die
    seriellen Logzeilen des Zyklus.
    """
    global _last_ack
    from app import monitoring
    from app.device import append_device_log, firmware_info, load_device_state, normalize_ack, save_device_state
    body = request.get_json(silent=True) or {}
    ack_data = normalize_ack(body, request.remote_addr)
    previous = dict(_last_ack)
    _last_ack = ack_data

    device = ack_data.get("device_id") or request.remote_addr
    result = ack_data.get("result", "updated")
    hash_short = ack_data.get("hash", "")[:8] or "–"
    matches = ack_data.get("hash", "") == _esp32_state.get("hash", "")

    # Beobachtung: Historie, Zähler, Ereignisse (Entwarnung, Firmware, Fehlerserie), letzte Rückmeldung überlebt einen Neustart
    monitoring.record_ack(ack_data, matches)
    monitoring.increment("inkwall_acks_total", result=result)
    try:
        _log_notifications(_notifier().on_ack(ack_data, previous))
        state = load_device_state()
        state["last_ack"] = ack_data
        save_device_state(state)
    except Exception as exc:
        log.warning(f"ACK-Beobachtung: {exc}")

    if isinstance(body.get("log"), list):
        append_device_log(device, body["log"], ack_data["ack_at"])

    if result == "updated":
        log_event("device", f"Gerät {device} zeigt jetzt das {'aktuelle' if matches else 'ältere'} Bild ({hash_short}…)")
    elif result == "ota":
        log_event("device", f"Gerät {device} installiert Firmware {ack_data.get('fw_target', '')} und startet neu".replace("  ", " "))
    elif result == "error":
        log_event("device", f"Gerät {device} meldet einen Fehler: {ack_data.get('error', 'unbekannt')}", logging.WARNING)
    else:
        log.debug(f"ACK {device}: {result} ({hash_short}…)")

    fw_now = ack_data.get("fw_version", "")
    if fw_now and previous.get("fw_version") and previous.get("fw_version") != fw_now:
        log_event("device", f"Gerät {device} läuft jetzt Firmware {fw_now}")
    from app.device import consume_test_banner, mark_cleaned
    if ack_data.get("cleaned"):
        mark_cleaned(_get_local_now())
        log_event("device", f"Gerät {device} hat das Panel gereinigt")
    offline_s = ack_data.get("offline_s")
    if isinstance(offline_s, int) and offline_s > 0:
        log_event("device", f"Gerät {device} ist wieder erreichbar – war {max(1, offline_s // 60)} min ohne Verbindung", logging.WARNING)
    if result == "test":
        log_event("device", f"Gerät {device} zeigt den Offline-Hinweis zur Probe")
    consume_test_banner()
    rssi = ack_data.get("rssi")
    if isinstance(rssi, int) and rssi < -82:
        log_event("device", f"Gerät {device}: WLAN sehr schwach ({rssi} dBm)", logging.WARNING)

    fw = firmware_info()
    return jsonify({
        "ok": True,
        "ack_at": ack_data["ack_at"],
        "firmware": {"version": fw["version"], "md5": fw["md5"], "url": fw["url"]} if fw else None,
    })


# ── Beobachtung: Metrics, ACK-Historie, Benachrichtigung ─────────────────────

@app.route("/metrics", methods=["GET"])
def metrics():
    """Prometheus-Textformat: Renders, Rückmeldungen, Gerätezustand, Inhalte, Zeitplan."""
    from app import monitoring
    from app.display_api import build_display_state
    cfg = get_cfg()
    state = build_display_state(_esp32_state, _last_ack, _suggest_next_wake(_esp32_state.get("state", "idle"), _esp32_state.get("media_type", "idle")))
    modules = [{"id": m["id"], "enabled": m["enabled"], "state": m["status"]["state"]} for m in state["content"] + state["live"]]
    text = monitoring.prometheus_text(
        _esp32_state, _last_ack, (state["next"]["seconds"], state["next"]["reason"]),
        _get_background_poll_seconds(), modules, _get_schedule_state(), cfg.notify_offline_minutes,
    )
    return text, 200, {"Content-Type": "text/plain; version=0.0.4; charset=utf-8"}


@app.route("/api/device/history", methods=["GET"])
def api_device_history():
    from app import monitoring
    try:
        hours = max(1.0, min(24.0 * 14, float(request.args.get("hours", "24"))))
        limit = max(10, min(5000, int(request.args.get("limit", "600"))))
    except ValueError:
        hours, limit = 24.0, 600
    entries = monitoring.read_ack_history(limit=limit, hours=hours)
    expected, _ = _suggest_next_wake(_esp32_state.get("state", "idle"), _esp32_state.get("media_type", "idle"))
    return jsonify({
        "hours": hours,
        "expected_seconds": int(expected),
        "entries": entries,
        "stats": monitoring.ack_stats(entries, int(expected)),
    })


@app.route("/api/device/history", methods=["DELETE"])
def api_device_history_delete():
    from app import monitoring
    removed = monitoring.clear_ack_history()
    log_event("device", f"ACK-Historie geleert ({removed} Einträge)")
    return jsonify({"ok": True, "removed": removed})


@app.route("/api/notify/test", methods=["POST"])
def api_notify_test():
    """Testnachricht an die gespeicherte oder die im Body übergebene Adresse."""
    from app import monitoring
    body = request.get_json(silent=True) or {}
    url = str(body.get("url") or get_cfg().notify_url or "").strip()
    if not url:
        return jsonify({"ok": False, "error": "Keine Adresse für Benachrichtigungen eingetragen."}), 400
    if not url.lower().startswith(("http://", "https://")):
        return jsonify({"ok": False, "error": "Die Adresse muss mit http:// oder https:// beginnen."}), 400
    from app.notifications import Notifier
    cfg = get_cfg()
    events = body.get("events") if isinstance(body.get("events"), list) else cfg.notify_events
    notifier = Notifier(url, events, cfg.notify_offline_minutes, cfg.notify_daily_hour,
                        str(body.get("base_url") or cfg.notify_base_url), str(body.get("avatar_url") or cfg.notify_avatar_url))
    ok = notifier.send_test(_last_ack, _current_summary())
    if not ok:
        return jsonify({"ok": False, "error": "Die Nachricht kam nicht an – Adresse prüfen (Ereignisse zeigen die Antwort)."}), 502
    monitoring.increment("inkwall_notifications_total", kind="test")
    log_event("device", "Testnachricht verschickt")
    return jsonify({"ok": True})


# ── Firmware und Gerätelog ───────────────────────────────────────────────────

@app.route("/firmware.json", methods=["GET"])
def firmware_json():
    from app.device import firmware_info
    fw = firmware_info()
    if not fw:
        return jsonify({"hosted": False}), 404
    return jsonify({"hosted": True, **fw})


@app.route("/firmware.bin", methods=["GET"])
def firmware_bin():
    from app.device import FIRMWARE_BIN, firmware_info
    fw = firmware_info()
    if not fw:
        return "Keine Firmware bereitgestellt.", 404
    response = send_file(str(FIRMWARE_BIN), mimetype="application/octet-stream", max_age=0,
                         as_attachment=True, download_name=f"Inkwall-{fw['version']}.bin")
    # HTTPUpdate auf dem ESP32 prüft die Datei gegen diesen Header
    response.headers["x-MD5"] = fw["md5"]
    response.headers["X-Firmware-Version"] = fw["version"]
    return response


@app.route("/api/device/firmware", methods=["GET"])
def api_device_firmware_get():
    from app.device import firmware_info
    return jsonify({"hosted": bool(firmware_info()), "firmware": firmware_info()})


@app.route("/api/device/firmware", methods=["POST"])
def api_device_firmware_post():
    from app.device import store_firmware
    upload = request.files.get("file")
    if upload is None:
        return jsonify({"ok": False, "error": "Bitte eine .bin-Datei auswählen."}), 400
    data = upload.read()
    try:
        info = store_firmware(data)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    log_event("device", f"Firmware {info['version']} bereitgestellt – das Gerät holt sie beim nächsten Aufwachen")
    return jsonify({"ok": True, "firmware": {**info, "url": "/firmware.bin"}})


@app.route("/api/device/firmware", methods=["DELETE"])
def api_device_firmware_delete():
    from app.device import delete_firmware
    delete_firmware()
    log_event("device", "Bereitgestellte Firmware entfernt")
    return jsonify({"ok": True})


@app.route("/api/device/test-banner", methods=["POST"])
def api_device_test_banner():
    """Einmalig: das Gerät zeigt beim nächsten Aufwachen den Offline-Hinweis zur Probe."""
    from app.device import request_test_banner
    request_test_banner()
    log_event("device", "Offline-Hinweis zur Probe angefordert – das Gerät zeigt ihn beim nächsten Aufwachen")
    return jsonify({"ok": True})


@app.route("/api/device/log", methods=["GET"])
def api_device_log():
    from app.device import read_device_log
    try:
        limit = min(int(request.args.get("limit", "300")), 2000)
    except ValueError:
        limit = 300
    return jsonify(read_device_log(limit))


@app.route("/api/device/log", methods=["DELETE"])
def api_device_log_delete():
    from app.device import clear_device_log
    clear_device_log()
    return jsonify({"ok": True})


# ── Settings ─────────────────────────────────────────────────────────────────

@app.route("/inhalte", methods=["GET"])
def content_page():
    return render_template("inhalte.html")


@app.route("/geraet", methods=["GET"])
def device_page():
    return render_template("geraet.html")


@app.route("/system", methods=["GET"])
def system_page():
    return render_template("system.html")


@app.route("/settings", methods=["GET"])
def settings_page():
    # Alte Adresse: die Einstellungen sind jetzt auf Inhalte, Gerät und System verteilt
    return redirect(url_for("content_page"))


@app.route("/api/settings/export", methods=["GET"])
def api_settings_export():
    from app.display_api import export_settings
    include_secrets = request.args.get("secrets", "").strip().lower() in ("1", "true", "yes")
    return jsonify(export_settings(include_secrets))


@app.route("/api/settings/import", methods=["POST"])
def api_settings_import():
    from app.display_api import import_updates, map_errors_to_fields
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jsonify({"ok": False, "error": "Keine gültige Einstellungsdatei."}), 400
    updates, ignored = import_updates(payload)
    if not updates:
        return jsonify({"ok": False, "error": "Die Datei enthält keine bekannten Einstellungen."}), 400
    sections = _build_settings_sections()
    all_fields = _all_fields_from_sections(sections)
    errors = _validate_all_settings(updates, all_fields)
    if errors:
        return jsonify({"ok": False, "errors": map_errors_to_fields(errors, all_fields)}), 400
    _apply_updates_and_render(updates)
    log_event("settings", f"Einstellungen aus Datei wiederhergestellt ({len(updates)} Werte)")
    return jsonify({"ok": True, "applied": len(updates), "ignored": ignored})


@app.route("/refresh", methods=["GET", "POST"])
def refresh():
    completed = request_render(wait_seconds=20, reason="/refresh")
    return jsonify({
        "ok":      True,
        "message": "refreshed" if completed else "queued",
        "completed": completed,
    })


@app.route("/api/rescan-modules", methods=["POST"])
def api_rescan_modules():
    """Scannt das modules/-Verzeichnis neu ohne Server-Neustart."""
    try:
        modules = _registry.reload_modules()
        log.info(f"Modul-Rescan abgeschlossen: {[m.MODULE_ID for m in modules]}")
        return jsonify({
            "ok":      True,
            "modules": _registry.get_module_info_list(),
            "count":   len(modules),
        })
    except Exception as exc:
        log.error(f"api_rescan_modules: {exc}", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


@app.route("/api/modules", methods=["GET"])
def api_modules():
    """Gibt alle aktuell geladenen Module zurück."""
    return jsonify(_registry.get_module_info_list())


@app.route("/api/module-field-options/<module_id>/<field_name>", methods=["GET"])
def api_module_field_options(module_id: str, field_name: str):
    try:
        options = _registry.get_module_field_options(module_id, field_name, get_settings_values())
        if options is None:
            return jsonify([]), 404
        return jsonify(options)
    except Exception as exc:
        log.error(f"api_module_field_options [{module_id}.{field_name}]: {exc}", exc_info=True)
        return jsonify([]), 500


@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        payload = request.form.get("payload")
        log.info(f"Webhook: {(payload or '')[:300] or '(kein Payload)'}")
        request_render(reason="Webhook")
        return jsonify({"ok": True, "queued": True})
    except Exception as exc:
        log.error(f"webhook: {exc}", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


# ── JSON-Schnittstelle der Oberfläche (Anzeige, Karten, Prüfen) ─────────────

def _apply_updates_and_render(updates: dict[str, str], wait_seconds: float = 0.0) -> None:
    """
    Teil-Änderung (eine Karte, die Anzeige, ein Import) übernehmen: in die
    Datei mergen und die Laufzeit-Konfiguration aus ALLEN bisherigen Werten
    plus den Änderungen neu bauen. apply_runtime_config() kennt nur, was man
    ihr gibt – nur die Änderungen zu übergeben würde alle anderen Werte aus
    dem Speicher werfen.
    """
    write_env_settings(updates)
    with _render_lock:
        apply_runtime_config({**get_settings_values(), **updates})
    log_event("settings", "Einstellungen gespeichert")
    request_render(wait_seconds=wait_seconds, reason="Einstellungen gespeichert")


def _display_state_payload() -> dict:
    from app.display_api import build_display_state
    state = _esp32_state.get("state", "idle")
    media_type = _esp32_state.get("media_type", "idle")
    return build_display_state(_esp32_state, _last_ack, _suggest_next_wake(state, media_type))


@app.route("/api/display", methods=["GET"])
def api_display_get():
    return jsonify(_display_state_payload())


@app.route("/api/display", methods=["PUT", "POST"])
def api_display_put():
    from app.display_api import display_updates_and_notices, map_errors_to_fields, schedule_errors
    payload = request.get_json(silent=True) or {}
    updates, notices = display_updates_and_notices(payload)
    if not updates:
        return jsonify({"ok": False, "errors": {"fields": {}, "general": ["Keine Änderungen übermittelt."]}}), 400

    sections = _build_settings_sections()
    all_fields = _all_fields_from_sections(sections)
    errors = _validate_all_settings(updates, all_fields)
    errors.extend(schedule_errors(payload, updates.get("IDLE_LAYOUT", get_cfg().idle_layout)))
    # Zusätzliche Anzeige-Regel: Dashboard-Kacheln müssen Kacheln liefern können
    cfg_layout = updates.get("IDLE_LAYOUT", get_cfg().idle_layout)
    if cfg_layout == "dashboard":
        for mid in [x.strip() for x in updates.get("IDLE_MODULES", "").split(",") if x.strip()]:
            mod = _registry.get_module_by_id(mid)
            if mod is not None and not mod.supports_tile():
                errors.append(f"Dashboard: {mod.MODULE_NAME} hat keine Kachel-Darstellung und wird im Dashboard nicht gezeigt.")
    if errors:
        return jsonify({"ok": False, "errors": map_errors_to_fields(errors, all_fields)}), 400

    _apply_updates_and_render(updates)
    return jsonify({"ok": True, "notices": notices, "display": _display_state_payload()})


@app.route("/api/settings/<module_id>", methods=["GET"])
def api_module_settings_get(module_id: str):
    from app.display_api import build_module_settings
    try:
        return jsonify(build_module_settings(module_id))
    except LookupError:
        return jsonify({"ok": False, "error": "Unbekanntes Modul"}), 404


@app.route("/api/settings/<module_id>", methods=["PUT", "POST"])
def api_module_settings_put(module_id: str):
    from app.display_api import build_module_settings, map_errors_to_fields, module_updates_from_values
    payload = request.get_json(silent=True) or {}
    values = payload.get("values", payload)
    if not isinstance(values, dict):
        return jsonify({"ok": False, "errors": {"fields": {}, "general": ["Ungültiges Format."]}}), 400
    try:
        updates = module_updates_from_values(module_id, values)
    except LookupError:
        return jsonify({"ok": False, "error": "Unbekanntes Modul"}), 404
    if not updates:
        return jsonify({"ok": False, "errors": {"fields": {}, "general": ["Keine bekannten Felder übermittelt."]}}), 400

    sections = _build_settings_sections()
    all_fields = _all_fields_from_sections(sections)
    # Nur diese Karte prüfen: eine Lücke in einem anderen Modul darf das
    # Speichern hier nicht blockieren (das prüft die Anzeige beim Einschalten)
    errors = list(validate_settings(updates, all_fields))
    target = _registry.get_module_by_id(module_id)
    if target is not None:
        try:
            errors.extend(target.validate_settings(updates, _build_effective_settings(updates)))
        except Exception as exc:
            log.error(f"validate_settings [{module_id}]: {exc}", exc_info=True)
            errors.append(f"{target.MODULE_NAME}: Fehler in der Modul-Validierung.")
    if errors:
        return jsonify({"ok": False, "errors": map_errors_to_fields(errors, all_fields)}), 400

    _apply_updates_and_render(updates)
    return jsonify({"ok": True, "settings": build_module_settings(module_id)})


@app.route("/api/probe/<module_id>", methods=["POST"])
def api_probe(module_id: str):
    from app.display_api import probe_module
    try:
        return jsonify(probe_module(module_id))
    except LookupError:
        return jsonify({"ok": False, "message": "Unbekanntes Modul"}), 404


# ── Dashboard ────────────────────────────────────────────────────────────────

@app.route("/", methods=["GET"])
def display_page():
    """Startseite: was das Display zeigt und das Programm."""
    return render_template("anzeige.html")


@app.route("/dashboard", methods=["GET"])
def dashboard():
    # Alte Adresse der Statusseite – die Anzeige ist jetzt die Startseite
    return redirect(url_for("display_page"))


# ── Log-Viewer ───────────────────────────────────────────────────────────────

@app.route("/logs", methods=["GET"])
def logs_page():
    # Alte Adresse: Ereignisse und Konsole liegen jetzt unter System
    return redirect(url_for("system_page"))


@app.route("/api/logs", methods=["GET"])
def api_logs():
    level_filter = request.args.get("level", "ALL").upper()
    try:
        limit = min(int(request.args.get("limit", "500")), 5000)
    except ValueError:
        limit = 500
    search = request.args.get("search", "").lower()
    # events=1: nur Ereignisse (log_event) plus Warnungen und Fehler – ohne "Rendered …"-Rauschen
    events_only = request.args.get("events", "").strip().lower() in ("1", "true", "yes")

    level_order = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "WARN": 30, "ERROR": 40, "CRITICAL": 50}
    min_level   = level_order.get(level_filter, 0) if level_filter != "ALL" else 0

    # Neueste Datei zuerst, Zeilen rückwärts, Abbruch sobald limit erreicht.
    # So wird nicht bei jedem Poll die gesamte 7-Tage-Historie geparst.
    entries: list[dict] = []
    for lf in sorted(LOGS_DIR.glob("app.jsonl*"), key=lambda f: f.stat().st_mtime, reverse=True):
        if len(entries) >= limit:
            break
        try:
            lines = lf.read_text(encoding="utf-8", errors="replace").splitlines()
        except Exception as exc:
            log.warning(f"api_logs: Fehler beim Lesen von {lf.name}: {exc}")
            continue
        for line in reversed(lines):
            if len(entries) >= limit:
                break
            line = line.strip()
            if not line:
                continue
            # Billige Vorfilter vor dem JSON-Parse
            if search and search not in line.lower():
                continue
            try:
                entry = _json.loads(line)
            except _json.JSONDecodeError:
                continue
            if level_order.get(entry.get("level", "DEBUG"), 0) < min_level:
                continue
            if events_only and not entry.get("event") and level_order.get(entry.get("level", "DEBUG"), 0) < 30:
                continue
            if search and search not in (entry.get("msg") or "").lower() \
                       and search not in (entry.get("name") or "").lower():
                continue
            # Auch beim Lesen maskieren: Zeilen aus der Zeit vor der Log-Maskierung
            # liegen bis zur Rotation noch unverändert in der Datei
            entry["msg"] = redact_secrets(entry.get("msg") or "")
            entries.append(entry)

    entries.reverse()   # chronologisch, wie bisher
    return jsonify(entries)


@app.route("/api/status", methods=["GET"])
def api_status():
    return jsonify({
        "esp32_state": dict(_esp32_state),
        "last_ack":    dict(_last_ack),
        "modules":     _registry.get_module_info_list(),
        "background_poll_sec": _get_background_poll_seconds(),
        "module_health": _build_module_health(),
    })


@app.route("/api/module-action/<module_id>/<action>", methods=["GET"])
def api_module_action(module_id: str, action: str):
    mod = _registry.get_module_by_id(module_id)
    if mod is None:
        return jsonify({"ok": False, "error": "unknown module"}), 404
    try:
        result = mod.handle_api_action(action, get_settings_values())
        if result is None:
            return jsonify({"ok": False, "error": "unknown action"}), 404
        payload, status = result
        return jsonify(payload), status
    except Exception as exc:
        log.error(f"api_module_action [{module_id}.{action}]: {exc}", exc_info=True)
        return jsonify({"ok": False, "error": str(exc)}), 500


# ---------------------------------------------------------------------------
# Startup
# ---------------------------------------------------------------------------

def log_startup_config() -> None:
    cfg = get_cfg()
    log.info(f"RENDER_SIZE          = {cfg.render_width}x{cfg.render_height}")
    log.info(f"DISPLAY_ROTATION     = {cfg.display_rotation}")
    log.info(f"DISPLAY_THEME        = {cfg.display_theme}")
    log.info(f"OUTPUT_FORMAT        = {cfg.output_format}")
    log.info(f"REFRESH_INTERVAL     = {cfg.refresh_interval}s")
    log.info(f"IDLE_MODULES         = {', '.join(cfg.idle_module_ids) or 'keine'}")
    log.info(f"IDLE_ROTATION        = {cfg.idle_module_rotation_seconds}s")
    from app.schedule import describe_window, effective_windows
    windows = effective_windows(cfg)
    if windows:
        source = "Zeitplan" if cfg.schedule_windows else "alter Nachtmodus"
        for w in windows:
            log.info(f"ZEITPLAN ({source})   = {w.name}: {describe_window(w)}")
    else:
        log.info("ZEITPLAN             = keine Zeitfenster, das Programm gilt rund um die Uhr")
    # Modul-Status generisch: jedes Modul beschreibt sich selbst
    env = get_settings_values()
    for mod in _registry.get_modules():
        try:
            summary = mod.get_runtime_summary(env) or {}
        except Exception as exc:
            summary = {"Fehler": str(exc)}
        parts = ", ".join(f"{k}: {v}" for k, v in summary.items())
        log.info(f"[{mod.MODULE_ID}] {'aktiv' if mod.is_enabled(env) else 'inaktiv'} – {parts}")


def _restore_last_ack() -> None:
    """Letzte Rückmeldung aus device_state.json, damit Gerät-Seite und Ausfallprüfung nach einem Neustart weiterwissen."""
    global _last_ack
    if _last_ack:
        return
    try:
        from app.device import load_device_state
        saved = load_device_state().get("last_ack")
        if isinstance(saved, dict) and saved.get("ack_at"):
            _last_ack = dict(saved)
    except Exception as exc:
        log.warning(f"Letzte Rückmeldung nicht wiederherstellbar: {exc}")


def ensure_runtime_started() -> None:
    """
    Startet Initial-Render und Background-Worker genau einmal pro Prozess.
    Wichtig für WSGI-Server wie Gunicorn, bei denen __main__ nicht ausgeführt wird.
    """
    global _runtime_started, _worker_thread

    if _runtime_started:
        return

    with _runtime_lock:
        if _runtime_started:
            return

        log.info(f"Module geladen: {[m.MODULE_ID for m in _registry.get_modules()]}")
        log_startup_config()
        _restore_last_ack()

        if not CURRENT_IMAGE_PATH.exists():
            render_image()

        _worker_thread = threading.Thread(
            target=periodic_worker,
            name="inkwall-periodic-worker",
            daemon=True,
        )
        _worker_thread.start()

        _runtime_started = True
        log.info("Runtime initialisiert")


if __name__ == "__main__":
    import os as _os
    _port = int(_os.environ.get("PORT", 8787))

    ensure_runtime_started()
    log.info(f"Server startet auf Port {_port}")
    app.run(host="0.0.0.0", port=_port, debug=False)
