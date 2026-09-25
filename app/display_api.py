"""
JSON-Schnittstelle für die Oberfläche.

- Anzeige:   was das Display zeigt, das Programm (Inhalte, Reihenfolge,
             Höhen, Schalter), Live-Inhalte, Nachtplan, Gerät
- Karten:    Felder eines Moduls lesen/schreiben mit Fehlern je Feld
- Prüfen:    eine Quelle einmal abrufen

Alle Schreibzugriffe landen in der bekannten Env-Datei; die Oberfläche
schreibt IDLE_MODULES, DASHBOARD_TILES, *_MODULE_ENABLED usw., damit
handgepflegte Dateien und alte Formulare weiter funktionieren.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from app.config import (
    SETTINGS_FIELDS as FRAMEWORK_FIELDS,
    SETTINGS_GROUPS as FRAMEWORK_GROUPS,
    get_cfg,
    get_settings_values,
)
from app.http_client import cache_only
from app.logger import get_logger
import app.module_registry as _registry

log = get_logger(__name__)

# Framework-Felder, die die Anzeige-Seite verwaltet (nicht mehr als Formularfeld)
DISPLAY_MANAGED_KEYS = {
    "IDLE_MODULES", "IDLE_LAYOUT", "DASHBOARD_TILES", "IDLE_MODULE_ROTATION_SECONDS", "SCHEDULE_WINDOWS",
    "NIGHT_MODE_ENABLED", "NIGHT_MODE_START", "NIGHT_MODE_END",
    "NIGHT_MODE_INTERVAL_MINUTES", "NIGHT_MODE_IDLE_BEHAVIOR", "NIGHT_MODE_FIXED_MODULE",
}
DEVICE_KEYS = {"RENDER_WIDTH", "RENDER_HEIGHT", "DISPLAY_ROTATION", "DISPLAY_THEME", "OUTPUT_FORMAT", "SHOW_RENDER_TIME",
               "PANEL_CLEAN_INTERVAL_DAYS", "PANEL_CLEAN_HOUR", "NOTIFY_URL", "NOTIFY_OFFLINE_MINUTES",
               "NOTIFY_EVENTS", "NOTIFY_DAILY_HOUR", "NOTIFY_BASE_URL", "NOTIFY_AVATAR_URL"}


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def _is_true(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return str(value).strip().lower() == "true"


def _module_enabled(mod, env: dict[str, str]) -> bool:
    if mod.ENABLED_KEY:
        default = mod.is_enabled({**env, mod.ENABLED_KEY: "true"}) and _is_true(env.get(mod.ENABLED_KEY), True)
        return _is_true(env.get(mod.ENABLED_KEY), default)
    idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
    return mod.MODULE_ID in idle


def _safe(callable_, default):
    try:
        return callable_()
    except Exception as exc:
        log.warning(f"display_api: {exc}")
        return default


def _module_entry(mod, env: dict[str, str], active_module_id: str, heights: dict[str, int]) -> dict[str, Any]:
    # Nur aus dem Cache: die Anzeige-Seite fragt alle 30 s, /metrics bei jedem
    # Scrape – das darf keine Quelle neu laden, schon gar keine abgeschaltete
    with cache_only():
        status = _safe(lambda: mod.describe_status(env), {"state": "ready", "reason": ""})
        summary = _safe(lambda: mod.summarize(env), "")
    return {
        "id":             mod.MODULE_ID,
        "name":           mod.MODULE_NAME,
        "description":    mod.MODULE_DESCRIPTION,
        "kind":           "live" if mod.MODULE_PRIORITY < 10 else "content",
        "enabled":        _module_enabled(mod, env),
        "status":         {"state": status.get("state", "ready"), "reason": status.get("reason", "")},
        "summary":        summary,
        "tile_supported": mod.supports_tile(),
        "height":         heights.get(mod.MODULE_ID),
        "active_now":     mod.MODULE_ID == active_module_id,
    }


# ---------------------------------------------------------------------------
# Anzeige
# ---------------------------------------------------------------------------

# Das 13,3″-Spectra-6-Panel der mitgelieferten Firmware nimmt nur Bilder genau dieser Größe
PANEL_SIZE = (1200, 1600)


def panel_setup_issues(cfg) -> list[str]:
    """Einstellungen, mit denen die mitgelieferte Firmware kein Bild bekommt oder es ablehnt."""
    issues: list[str] = []
    if cfg.output_format != "bmp":
        issues.append("Ausgabeformat ist PNG – die Firmware lädt das Panel-Format, das es nur mit „BMP“ gibt.")
    if (cfg.render_width, cfg.render_height) != PANEL_SIZE:
        issues.append(f"Das Bild hat {cfg.render_width} × {cfg.render_height} Pixel, das Panel braucht 1200 × 1600 "
                      "(Breite 1600, Höhe 1200, Ausrichtung 90°).")
    return issues


def build_display_state(esp32_state: dict, last_ack: dict, next_wake: tuple[int, str]) -> dict[str, Any]:
    env = get_settings_values()
    cfg = get_cfg()
    tiles = dict(cfg.dashboard_tiles)
    order = [mid for mid, _ in cfg.dashboard_tiles]
    active_id = str(esp32_state.get("media_type", ""))

    content_mods = _registry.get_idle_modules()
    live_mods = _registry.get_priority_modules()
    # Reihenfolge: erst wie in DASHBOARD_TILES, dann der Rest nach Priorität
    ordered = [m for mid in order for m in content_mods if m.MODULE_ID == mid]
    ordered += [m for m in content_mods if m not in ordered]

    active_mod = _registry.get_module_by_id(active_id)
    if active_id == "dashboard":
        active_name = "Dashboard"
    elif active_mod is not None:
        active_name = active_mod.MODULE_NAME
    elif active_id in ("none", "idle", ""):
        active_name = "Kein Inhalt"
    else:
        active_name = active_id

    ack_at = last_ack.get("ack_at", "")
    seconds_since_ack = None
    if ack_at:
        try:
            ack_dt = datetime.strptime(ack_at, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            seconds_since_ack = int((datetime.now(timezone.utc) - ack_dt).total_seconds())
        except ValueError:
            seconds_since_ack = None

    night_fixed = _registry.get_module_by_id(cfg.night_mode_fixed_module_id)

    from app.config import now_local
    from app.schedule import active_window, describe_window, effective_windows
    windows = effective_windows(cfg)
    module_names = {m.MODULE_ID: m.MODULE_NAME for m in _registry.get_modules()}
    from app.holidays import school_holiday_checker
    active, seconds_until_change, upcoming = active_window(windows, now_local(), school_holiday_checker(windows)) if windows else (None, 0, None)

    from app.device import RESET_LABELS, firmware_info, firmware_update_expected, last_clean_at, rssi_quality, test_banner_pending
    fw = firmware_info()
    last_clean = last_clean_at()
    device_fw = str(last_ack.get("fw_version", "") or "")
    rssi = last_ack.get("rssi")
    rssi_label, rssi_state = rssi_quality(rssi if isinstance(rssi, int) else None)
    return {
        "firmware": {
            "hosted":         bool(fw),
            "version":        fw["version"] if fw else "",
            "size":           fw["size"] if fw else 0,
            "md5":            fw["md5"] if fw else "",
            "uploaded_at":    fw.get("uploaded_at", "") if fw else "",
            "device_version": device_fw,
            # Update steht an, wenn das Gerät die bereitgestellte Datei auch einspielt
            # (ab Firmware 1.3.0 nur neuere Versionen, ausser beim Hochladen erzwungen)
            "update_pending": firmware_update_expected(fw, device_fw),
            "not_newer":      bool(fw) and bool(device_fw) and fw["version"] != device_fw and not firmware_update_expected(fw, device_fw),
            "force":          bool(fw.get("force")) if fw else False,
            "device_supports_ota": bool(device_fw),
        },
        "panel": {
            "clean_interval_days": cfg.panel_clean_interval_days,
            "clean_hour":          cfg.panel_clean_hour,
            "last_clean_at":       last_clean.isoformat() if last_clean else "",
            "test_banner_pending": test_banner_pending(),
            "setup_issues":        panel_setup_issues(cfg),
        },
        "layout":           cfg.idle_layout,
        "rotation_seconds": cfg.idle_module_rotation_seconds,
        "theme":            cfg.display_theme,
        "schedule": {
            "windows": [{**w.to_dict(), "summary": describe_window(w, module_names), "active": w is active} for w in windows],
            # "night": Fenster stammt aus dem alten Nachtmodus, "schedule": gespeicherter Zeitplan
            "source":               "schedule" if cfg.schedule_windows else ("night" if windows else ""),
            "active_name":          active.name if active else "",
            "seconds_until_change": seconds_until_change,
            "next_name":            upcoming.name if upcoming else "",
        },
        "night": {
            "enabled":          cfg.night_mode_enabled,
            "start":            cfg.night_mode_start,
            "end":              cfg.night_mode_end,
            "interval_minutes": cfg.night_mode_interval_minutes,
            "behavior":         cfg.night_mode_idle_behavior,
            "fixed_module":     cfg.night_mode_fixed_module_id,
            "fixed_module_name": night_fixed.MODULE_NAME if night_fixed else "",
        },
        "current": {
            "module_id":   active_id,
            "module_name": active_name,
            "rendered_at": esp32_state.get("rendered_at", ""),
            "hash":        esp32_state.get("hash", ""),
            "state":       esp32_state.get("state", ""),
            "format":      esp32_state.get("format", ""),
        },
        "next": {"seconds": int(next_wake[0]), "reason": str(next_wake[1])},
        "notify": {
            "url_set":         bool(cfg.notify_url),
            "offline_minutes": cfg.notify_offline_minutes,
            "events":          list(cfg.notify_events),
            "daily_hour":      cfg.notify_daily_hour,
            "base_url_set":    bool(cfg.notify_base_url),
        },
        "device": {
            "device_id":        last_ack.get("device_id", ""),
            "ack_at":           ack_at,
            "remote":           last_ack.get("remote", ""),
            "seconds_since_ack": seconds_since_ack,
            "hash_matches":     bool(ack_at) and last_ack.get("hash") == esp32_state.get("hash"),
            "result":           str(last_ack.get("result", "") or ""),
            "error":            str(last_ack.get("error", "") or ""),
            "fw_version":       device_fw,
            "rssi":             rssi if isinstance(rssi, int) else None,
            "rssi_label":       rssi_label,
            "rssi_state":       rssi_state,
            "boot_count":       last_ack.get("boot_count"),
            "free_psram_kb":    last_ack.get("free_psram_kb"),
            "cycle_ms":         last_ack.get("cycle_ms"),
            "download_ms":      last_ack.get("download_ms"),
            "refresh_ms":       last_ack.get("refresh_ms"),
            "image_format":     str(last_ack.get("image_format", "") or ""),
            "wake_reason":      str(last_ack.get("wake_reason", "") or ""),
            "reset_reason":     str(last_ack.get("reset_reason", "") or ""),
            "reset_label":      RESET_LABELS.get(str(last_ack.get("reset_reason", "") or ""), ""),
            "ip":               str(last_ack.get("ip", "") or ""),
        },
        "content": [_module_entry(m, env, active_id, tiles) for m in ordered],
        "live":    [_module_entry(m, env, active_id, tiles) for m in live_mods],
    }


def normalize_tile_heights(items: list[dict]) -> tuple[list[dict], str | None]:
    """
    Prozentangaben über 100 anteilig verkleinern, damit gespeichert wird, was
    gerendert wird. Rückgabe (items mit angepassten Höhen, Hinweis oder None).
    """
    sized = [it for it in items if it.get("enabled") and isinstance(it.get("height"), int) and it["height"] > 0]
    total = sum(it["height"] for it in sized)
    if total <= 100:
        return items, None
    scaled: list[dict] = []
    for it in items:
        if it in sized:
            it = {**it, "height": max(1, int(it["height"] * 100 // total))}
        scaled.append(it)
    new_total = sum(it["height"] for it in scaled if it.get("enabled") and isinstance(it.get("height"), int) and it["height"] > 0)
    return scaled, f"Die Höhen ergaben {total} %. Sie wurden anteilig auf {new_total} % verkleinert."


def display_updates_from_payload(payload: dict) -> dict[str, str]:
    updates, _ = display_updates_and_notices(payload)
    return updates


def display_updates_and_notices(payload: dict) -> tuple[dict[str, str], list[str]]:
    """
    Übersetzt die Anzeige-Änderung in Settings-Keys. Fehlende Teile bleiben
    unverändert (es werden nur Keys geliefert, die im Payload vorkommen).
    Hinweise beschreiben stille Anpassungen, z. B. verkleinerte Höhen.
    """
    updates: dict[str, str] = {}
    notices: list[str] = []
    known = {m.MODULE_ID: m for m in _registry.get_modules()}

    if "layout" in payload:
        layout = str(payload["layout"]).strip().lower()
        updates["IDLE_LAYOUT"] = layout if layout in {"rotation", "dashboard"} else "rotation"
    if "rotation_seconds" in payload:
        updates["IDLE_MODULE_ROTATION_SECONDS"] = str(payload["rotation_seconds"]).strip()

    if isinstance(payload.get("content"), list):
        items: list[dict] = []
        for item in payload["content"]:
            mid = str(item.get("id", "")).strip()
            mod = known.get(mid)
            if mod is None or mod.MODULE_PRIORITY < 10:
                continue
            height = item.get("height")
            try:
                pct = int(height) if height not in (None, "", "auto") else 0
            except (TypeError, ValueError):
                pct = 0
            items.append({"id": mid, "enabled": bool(item.get("enabled")), "height": max(0, min(100, pct))})
        items, notice = normalize_tile_heights(items)
        if notice:
            notices.append(notice)
        enabled = [it for it in items if it["enabled"]]
        updates["IDLE_MODULES"] = ",".join(it["id"] for it in enabled)
        updates["DASHBOARD_TILES"] = ", ".join(f"{it['id']}:{it['height']}" if it["height"] > 0 else it["id"] for it in enabled)

    if isinstance(payload.get("live"), list):
        for item in payload["live"]:
            mod = known.get(str(item.get("id", "")).strip())
            if mod is not None and mod.ENABLED_KEY:
                updates[mod.ENABLED_KEY] = "true" if item.get("enabled") else "false"

    schedule_payload = payload.get("schedule")
    if isinstance(schedule_payload, dict) and isinstance(schedule_payload.get("windows"), list):
        from app.schedule import serialize_windows, window_from_dict
        windows = [window_from_dict(w, i) for i, w in enumerate(schedule_payload["windows"]) if isinstance(w, dict)]
        updates["SCHEDULE_WINDOWS"] = serialize_windows(windows)
        # Der Zeitplan löst den alten Nachtmodus ab (die Oberfläche hat ihn als Fenster übernommen)
        updates["NIGHT_MODE_ENABLED"] = "false"

    night = payload.get("night")
    if isinstance(night, dict):
        mapping = {
            "enabled": "NIGHT_MODE_ENABLED", "start": "NIGHT_MODE_START", "end": "NIGHT_MODE_END",
            "interval_minutes": "NIGHT_MODE_INTERVAL_MINUTES", "behavior": "NIGHT_MODE_IDLE_BEHAVIOR",
            "fixed_module": "NIGHT_MODE_FIXED_MODULE",
        }
        for key, env_key in mapping.items():
            if key in night:
                value = night[key]
                updates[env_key] = ("true" if value else "false") if isinstance(value, bool) else str(value).strip()
    return updates, notices


def schedule_errors(payload: dict, programme_layout: str = "rotation") -> list[str]:
    """
    Prüft die Zeitfenster eines Anzeige-Payloads, bevor sie gespeichert werden:
    Wochentage, Zeiten, Takt, bekannte Inhalte, und dass Dashboard-Fenster nur
    Inhalte mit Kachel enthalten (ererbtes Layout zählt mit).
    """
    from app.schedule import validate_windows, window_from_dict
    schedule_payload = payload.get("schedule")
    if not isinstance(schedule_payload, dict) or not isinstance(schedule_payload.get("windows"), list):
        return []
    windows = [window_from_dict(w, i) for i, w in enumerate(schedule_payload["windows"]) if isinstance(w, dict)]
    idle = {m.MODULE_ID: m for m in _registry.get_idle_modules()}
    errors = validate_windows(windows, set(idle))
    from app.holidays import configured_region
    if any(w.school for w in windows) and not configured_region():
        name = next(w.name for w in windows if w.school)
        errors.append(f"Zeitplan: Fenster „{name}“ hängt an den Schulferien – dafür unter System das Bundesland "
                      "für Feiertage und Ferien einstellen.")
    for w in windows:
        layout = w.layout or programme_layout
        if layout != "dashboard":
            continue
        for mid in w.module_ids:
            mod = idle.get(mid)
            if mod is not None and not mod.supports_tile():
                errors.append(f"Zeitplan: Fenster „{w.name}“: {mod.MODULE_NAME} hat keine Kachel-Darstellung und wird im Dashboard nicht gezeigt.")
    return errors


# ---------------------------------------------------------------------------
# Karten (Modul-Einstellungen)
# ---------------------------------------------------------------------------

def _split_list_value(raw: str, field: dict) -> list[dict[str, str]]:
    """'Label|URL; URL' → [{"label": "Label", "url": "URL"}, {"label": "", "url": "URL"}]."""
    item_fields = [f["name"] for f in field.get("item_fields", [])] or ["value"]
    separator = field.get("separator", ";")
    joiner = field.get("joiner", "|")
    items: list[dict[str, str]] = []
    for chunk in str(raw or "").replace("\n", separator).split(separator):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split(joiner)] if len(item_fields) > 1 else [chunk]
        if len(item_fields) > 1 and len(parts) == 1:
            # Nur der letzte Teil (z. B. die URL) ist Pflicht – Label fehlt
            parts = [""] * (len(item_fields) - 1) + parts
        items.append({name: (parts[i] if i < len(parts) else "") for i, name in enumerate(item_fields)})
    return items


def _join_list_value(items: list, field: dict) -> str:
    item_fields = [f["name"] for f in field.get("item_fields", [])] or ["value"]
    separator = field.get("separator", ";")
    joiner = field.get("joiner", "|")
    chunks: list[str] = []
    for item in items or []:
        if isinstance(item, dict):
            parts = [str(item.get(name, "") or "").strip() for name in item_fields]
        else:
            parts = [str(item or "").strip()]
        if not any(parts):
            continue
        # Leere führende Teile (Label) weglassen, damit "URL" statt "|URL" entsteht
        while len(parts) > 1 and not parts[0]:
            parts = parts[1:]
        chunks.append(joiner.join(parts))
    return f"{separator} ".join(chunks)


def _split_mapping_value(raw: str) -> list[dict[str, str]]:
    pairs: list[dict[str, str]] = []
    for chunk in str(raw or "").split(","):
        if "=" not in chunk:
            continue
        key, value = chunk.split("=", 1)
        if key.strip():
            pairs.append({"key": key.strip(), "value": value.strip()})
    return pairs


def _join_mapping_value(pairs: list) -> str:
    return ", ".join(f"{p.get('key', '').strip()}={p.get('value', '').strip()}"
                     for p in (pairs or []) if isinstance(p, dict) and p.get("key", "").strip())


# Zahlenfelder mit Sekunden werden in der Oberfläche als Dauer gezeigt (s/min/h)
def _effective_type(field: dict) -> str:
    ftype = field.get("type", "text")
    if ftype == "number" and field["name"].endswith("_SECONDS"):
        return "duration"
    return ftype


def _field_view(field: dict, values: dict[str, str]) -> dict[str, Any]:
    name = field["name"]
    raw = values.get(name)
    if raw is None or (raw == "" and field.get("type") != "checkbox_group"):
        raw = field.get("default", "")
    if name == "NOTIFY_EVENTS" and not field.get("options"):
        from app.notifications import EVENT_OPTIONS
        field = {**field, "options": [list(o) for o in EVENT_OPTIONS]}
    ftype = _effective_type(field)
    view = {
        "name":        name,
        "label":       field.get("label", name),
        "type":        ftype,
        "help":        field.get("help", ""),
        "placeholder": field.get("placeholder", ""),
        "wide":        bool(field.get("wide", False)),
        "options":     [list(o) if isinstance(o, (list, tuple)) else [o, o] for o in field.get("options", [])],
        "show_when":   field.get("show_when"),
        "link_href":   field.get("link_href", ""),
        "link_label":  field.get("link_label", ""),
        "link_note":   field.get("link_note", ""),
        "min":         field.get("min"),
        "max":         field.get("max"),
        "datalist_url": field.get("datalist_url", ""),
        "item_fields": [dict(f) for f in field.get("item_fields", [])],
        "value_options": [list(o) for o in field.get("value_options", [])],
        "managed_by_display": name in DISPLAY_MANAGED_KEYS,
        "device": name in DEVICE_KEYS,
    }
    if ftype == "password":
        view["value"] = ""
        view["is_set"] = bool(values.get(name, "").strip())
    elif ftype in ("checkbox_group", "priority_list"):
        view["value"] = [x.strip() for x in str(raw).split(",") if x.strip()]
    elif ftype == "list":
        view["value"] = _split_list_value(str(raw), field)
    elif ftype == "mapping":
        view["value"] = _split_mapping_value(str(raw))
    else:
        view["value"] = str(raw)
    return view


def _fields_for(module_id: str) -> tuple[list[dict], list[dict], Any]:
    if module_id == "framework":
        return list(FRAMEWORK_FIELDS), list(FRAMEWORK_GROUPS), None
    mod = _registry.get_module_by_id(module_id)
    if mod is None:
        raise LookupError(module_id)
    return list(mod.SETTINGS_FIELDS), list(mod.SETTINGS_GROUPS), mod


def build_module_settings(module_id: str) -> dict[str, Any]:
    fields, groups, mod = _fields_for(module_id)
    values = get_settings_values()
    env = values
    payload: dict[str, Any] = {
        "id":     module_id,
        "name":   mod.MODULE_NAME if mod else "Gerät & System",
        "fields": [_field_view(f, values) for f in fields],
        "groups": [{"title": g.get("title", ""), "desc": g.get("desc", ""), "fields": list(g.get("fields", []))} for g in groups],
    }
    if mod is not None:
        payload["status"]  = _safe(lambda: mod.describe_status(env), {"state": "ready", "reason": ""})
        payload["summary"] = _safe(lambda: mod.summarize(env), "")
        payload["enabled"] = _module_enabled(mod, env)
        payload["kind"]    = "live" if mod.MODULE_PRIORITY < 10 else "content"
        payload["enabled_key"] = mod.ENABLED_KEY or ""
        # Der Ein/Aus-Schalter eines Live-Inhalts gehört auf die Anzeige-Seite
        for view in payload["fields"]:
            if mod.ENABLED_KEY and view["name"] == mod.ENABLED_KEY:
                view["managed_by_display"] = True
    return payload


def module_updates_from_values(module_id: str, incoming: dict) -> dict[str, str]:
    """Formwerte → Settings-Keys. Leere Passwörter behalten den gespeicherten Wert."""
    fields, _, _ = _fields_for(module_id)
    current = get_settings_values()
    updates: dict[str, str] = {}
    for field in fields:
        name = field["name"]
        if name not in incoming:
            continue
        value = incoming[name]
        ftype = field.get("type", "text")
        if ftype == "password":
            text = str(value or "").strip()
            updates[name] = text if text else current.get(name, "")
        elif ftype in ("checkbox_group", "priority_list"):
            if isinstance(value, (list, tuple)):
                updates[name] = ",".join(str(v).strip() for v in value if str(v).strip())
            else:
                updates[name] = str(value or "").strip()
        elif ftype == "list":
            updates[name] = _join_list_value(value, field) if isinstance(value, list) else str(value or "").strip()
        elif ftype == "mapping":
            updates[name] = _join_mapping_value(value) if isinstance(value, list) else str(value or "").strip()
        elif isinstance(value, bool):
            updates[name] = "true" if value else "false"
        else:
            updates[name] = str(value if value is not None else "").strip()
    return updates


def list_value_errors(module_id: str, incoming: dict) -> list[str]:
    """
    Listen und Zuordnungen landen als eine Zeile in settings.env ('Name|URL; URL',
    'bio=blue, papier=blue'). Ein Trennzeichen in einem Eintrag würde ihn beim
    nächsten Laden still in falsche Spalten zerlegen – deshalb vorher ablehnen.
    """
    fields, _, _ = _fields_for(module_id)
    errors: list[str] = []
    for field in fields:
        value = incoming.get(field["name"])
        ftype = field.get("type", "text")
        if ftype not in ("list", "mapping") or not isinstance(value, list):
            continue
        if ftype == "list":
            cols = field.get("item_fields") or [{"name": "value", "label": field.get("label", "")}]
            forbidden = [field.get("separator", ";")] + ([field.get("joiner", "|")] if len(cols) > 1 else [])
        else:
            cols = field.get("item_fields") or [{"name": "key", "label": "Stichwort"}, {"name": "value", "label": "Wert"}]
            forbidden = [",", "="]
        for row, item in enumerate(value, start=1):
            if not isinstance(item, dict):
                continue
            for col in cols:
                text = str(item.get(col["name"], "") or "")
                bad = [c for c in forbidden if c in text] + (["Zeilenumbruch"] if "\n" in text else [])
                if bad:
                    what = " und ".join(f"„{c}“" if len(c) == 1 else c for c in bad)
                    errors.append(f"{field.get('label', field['name'])}: Zeile {row}, {col.get('label') or col['name']}: "
                                  f"{what} ist hier nicht möglich – das Zeichen trennt die Einträge beim Speichern.")
                    break
    return errors


def map_errors_to_fields(errors: list[str], fields: list[dict]) -> dict[str, Any]:
    """'Label: Nachricht' → {"fields": {name: Nachricht}, "general": [...]}."""
    by_label = {str(f.get("label", "")).strip(): f["name"] for f in fields if f.get("label")}
    result: dict[str, Any] = {"fields": {}, "general": []}
    for error in errors:
        label, sep, message = error.partition(":")
        name = by_label.get(label.strip()) if sep else None
        if name and name not in result["fields"]:
            result["fields"][name] = message.strip()
        else:
            result["general"].append(error)
    return result


# ---------------------------------------------------------------------------
# Export / Import
# ---------------------------------------------------------------------------

def _all_known_fields() -> list[dict]:
    fields = list(FRAMEWORK_FIELDS)
    for mod in _registry.get_modules():
        fields.extend(mod.SETTINGS_FIELDS)
    return fields


def secret_field_names() -> set[str]:
    """Passwort-Felder und Felder mit "secret": True (Webhook-Adresse, private Kalender-Links …)."""
    return {f["name"] for f in _all_known_fields() if f.get("type") == "password" or f.get("secret")}


def export_settings(include_secrets: bool = False) -> dict[str, Any]:
    """Alle bekannten Settings als {key: value}. Passwörter, Webhook- und Kalender-Links nur auf Wunsch."""
    values = get_settings_values()
    fields = _all_known_fields()
    secret_keys = secret_field_names()
    exported = {
        f["name"]: values.get(f["name"], "")
        for f in fields
        if include_secrets or f["name"] not in secret_keys
    }
    return {
        "format": "inkwall-settings",
        "version": 1,
        "includes_secrets": include_secrets,
        "values": exported,
    }


def import_updates(payload: dict) -> tuple[dict[str, str], list[str]]:
    """Import-Datei → (Updates nur für bekannte Keys, ignorierte Keys)."""
    values = payload.get("values", payload) if isinstance(payload, dict) else {}
    if not isinstance(values, dict):
        return {}, []
    known = {f["name"]: f for f in _all_known_fields()}
    updates: dict[str, str] = {}
    ignored: list[str] = []
    for key, value in values.items():
        field = known.get(str(key))
        if field is None:
            ignored.append(str(key))
            continue
        if field.get("type") == "password" and not str(value or "").strip():
            continue      # leere Passwörter im Import überschreiben nichts
        updates[str(key)] = str(value if value is not None else "").strip()
    return updates, ignored


def probe_module(module_id: str, values: dict | None = None) -> dict[str, Any]:
    """
    Verbindung prüfen. values: Formularwerte der Karte, noch nicht gespeichert –
    sie gelten nur für diese Prüfung (leere Passwörter behalten den gespeicherten Wert).
    """
    mod = _registry.get_module_by_id(module_id)
    if mod is None:
        raise LookupError(module_id)
    from app.config import override_runtime_config
    env = get_settings_values()
    unsaved = module_updates_from_values(module_id, values) if isinstance(values, dict) else {}
    env = {**env, **unsaved}
    # Die Datenquellen lesen ihre Einstellungen über get_setting – dort müssen die Formularwerte auch gelten
    with override_runtime_config(settings_values=unsaved):
        result = _safe(lambda: mod.probe(env), {"ok": False, "message": "Prüfung fehlgeschlagen"})
    details = result.get("details") or []
    return {
        "ok": bool(result.get("ok")),
        "message": str(result.get("message", "")),
        "details": [str(d) for d in details if str(d).strip()][:40],
    }
