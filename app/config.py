"""
Konfiguration, Konstanten, RuntimeConfig und Env-IO für Inkwall.

Dieses Modul enthält ausschließlich Framework-Einstellungen (Render-Größe,
Rotation, Theme, Ausgabeformat, Idle-Verwaltung, Zeitzone).
Modul-spezifische Einstellungen (Plex, DWD, Tagesschau …) sind in den
jeweiligen modules/*/SETTINGS_FIELDS definiert.

WICHTIG: Dieses Modul darf NICHT von modules/* oder app.image_rendering
importieren – es steht am Anfang der Dependency-Chain.
"""

from __future__ import annotations

import contextlib
import contextvars
import functools
import os
import re
import tempfile
import threading
from dataclasses import dataclass, field, replace as _dc_replace
from pathlib import Path

from dotenv import dotenv_values
from PIL import ImageFont


APP_VERSION = "0.2.0"

# ---------------------------------------------------------------------------
# Verzeichnisse
# ---------------------------------------------------------------------------

BASE_DIR    = Path(__file__).resolve().parent
PROJECT_DIR = BASE_DIR.parent
CONFIG_DIR = PROJECT_DIR / "config"
CONFIG_DIR.mkdir(parents=True, exist_ok=True)


def process_env(name: str, default: str = "") -> str:
    """Prozess-Umgebungsvariable INKWALL_<name>; der alte Präfix PLEXINK_ gilt weiter."""
    for prefix in ("INKWALL_", "PLEXINK_"):
        value = os.environ.get(prefix + name, "").strip()
        if value:
            return value
    return default


def resolve_env_file_path() -> Path:
    configured = process_env("CONFIG_FILE")
    if configured:
        return Path(configured).expanduser()
    return CONFIG_DIR / "settings.env"


ENV_FILE_PATH = resolve_env_file_path()
# Bewusst KEIN load_dotenv(): Settings leben ausschließlich in RuntimeConfig,
# nicht in os.environ. Sonst könnte ein Wert wie HTTPS_PROXY aus der
# Settings-Datei das Verhalten von requests beeinflussen.

# Ausgabepfad: per INKWALL_OUTPUT_DIR überschreibbar (Docker: /output)
_output_env = process_env("OUTPUT_DIR")
DATA_DIR = Path(_output_env) if _output_env else PROJECT_DIR / "data" / "output"
DATA_DIR.mkdir(parents=True, exist_ok=True)

CURRENT_IMAGE_PATH = DATA_DIR / "current.png"
CURRENT_BMP_PATH   = DATA_DIR / "current.bmp"
CURRENT_EPD_PATH   = DATA_DIR / "current.epd"    # kompaktes 4-bpp-Format für den ESP32
STATE_PATH         = DATA_DIR / "state.txt"


# ---------------------------------------------------------------------------
# Anwendungskonstanten
# ---------------------------------------------------------------------------

DISPLAY_ROTATION_ALIASES = {
    "0": 0, "90": 90, "180": 180, "270": 270,
    "portrait-right": 90, "portrait-left": 270,
    "landscape": 0, "landscape-flipped": 180,
}

# ---------------------------------------------------------------------------
# Framework-Settings-Felder
# Modulspezifische Felder liegen in modules/*/SETTINGS_FIELDS.
# ---------------------------------------------------------------------------

SETTINGS_FIELDS: list[dict] = [
    # ── Render-Grundeinstellungen ────────────────────────────────────────────
    {
        "name":        "REFRESH_INTERVAL",
        "label":       "Refresh-Intervall (s)",
        "type":        "number",
        "section":     "framework",
        "wide":        False,
        "placeholder": "60",
        "min":         10,
        "max":         3600,
        "help":        "Intervall in Sekunden für den Hintergrund-Prüfzyklus.",
    },
    {
        "name":        "RENDER_WIDTH",
        "label":       "Render-Breite (px)",
        "type":        "number",
        "section":     "framework",
        "wide":        False,
        "placeholder": "1600",
        "min":         400,
        "max":         3840,
        "help":        "Basisbreite vor optionaler Rotation.",
    },
    {
        "name":        "RENDER_HEIGHT",
        "label":       "Render-Höhe (px)",
        "type":        "number",
        "section":     "framework",
        "wide":        False,
        "placeholder": "1200",
        "min":         300,
        "max":         2160,
        "help":        "Basishöhe vor optionaler Rotation.",
    },
    {
        "name":    "DISPLAY_ROTATION",
        "label":   "Display-Ausrichtung",
        "type":    "select",
        "section": "framework",
        "wide":    False,
        "options": [("0", "0°"), ("90", "90°"), ("180", "180°"), ("270", "270°")],
        "help":    "90 und 270 rendern im Hochformat. 180 und 270 sind auf dem Kopf.",
    },
    {
        "name":    "DISPLAY_THEME",
        "label":   "Display-Theme",
        "type":    "select",
        "section": "framework",
        "wide":    True,
        "options": [
            ("dark",  "🌑 Dark – verschwommenes Artwork, weißer Text"),
            ("light", "☀️ Light – heller Hintergrund, weiche Verläufe"),
            ("eink",  "📄 E-Ink – flache Spectra-6-Farben, kein Blur, dicke Linien"),
        ],
        "help": (
            "Steuert das Aussehen aller generierten Bilder. "
            "Dark und Light sind für Bildschirme gedacht. "
            "E-Ink nutzt nur die sechs Displayfarben ohne Verläufe und Transparenzen, "
            "damit auf dem Spectra-6-Display nichts zu Dithering-Rauschen wird."
        ),
    },
    {
        "name":    "OUTPUT_FORMAT",
        "label":   "Ausgabeformat",
        "type":    "select",
        "section": "framework",
        "wide":    True,
        "options": [
            ("png", "PNG – Vollfarb, für HDMI- oder Web-Displays"),
            ("bmp", "BMP – Spectra 6 Dithering, für Waveshare 13,3″ E-Ink-Farbdisplay"),
        ],
        "help": (
            "PNG: Vollfarb-RGB. BMP: Floyd-Steinberg-Dithering auf 6 Spectra-Farben "
            "für das Waveshare Spectra 6 E-Ink-Display."
        ),
    },
    {
        "name":    "SHOW_RENDER_TIME",
        "label":   "Uhrzeit auf jeder Seite",
        "type":    "select",
        "section": "framework",
        "wide":    False,
        "default": "false",
        "options": [("false", "Aus"), ("true", "Ein – „Stand HH:MM“ oben rechts")],
        "help":    (
            "Zeigt auf jedem Bild, wann es erzeugt wurde. Fällt der Server oder das WLAN aus, "
            "bleibt das alte Bild stehen – mit Uhrzeit erkennt man das sofort. Das Dashboard hat die Angabe immer."
        ),
    },
    {
        "name":    "PANEL_CLEAN_INTERVAL_DAYS",
        "label":   "Panel reinigen alle (Tage)",
        "type":    "number",
        "section": "framework",
        "wide":    False,
        "default": "14",
        "min":     0,
        "max":     365,
        "help":    (
            "E-Ink-Panels bilden mit der Zeit Geisterbilder. Das Gerät fährt dann nachts einmal Weiß und Schwarz "
            "durch und lädt das Bild neu (rund 90 Sekunden). 0 schaltet die Reinigung aus."
        ),
    },
    {
        "name":    "PANEL_CLEAN_HOUR",
        "label":   "Reinigung um (Uhr)",
        "type":    "number",
        "section": "framework",
        "wide":    False,
        "default": "3",
        "min":     0,
        "max":     23,
        "help":    "Stunde, in der die Reinigung fällig wird – das Gerät macht sie beim ersten Aufwachen in dieser Stunde.",
    },
    # ── Benachrichtigung ─────────────────────────────────────────────────────
    {
        "name":        "NOTIFY_URL",
        "secret":      True,          # steckt ein Zugangsweg drin: nicht im Export „ohne Geheimnisse“
        "label":       "Benachrichtigung an",
        "type":        "text",
        "section":     "framework",
        "wide":        True,
        "default":     "",
        "placeholder": "https://ntfy.sh/mein-display",
        "help": (
            "ntfy-Topic (App auf dem Handy), Discord- oder Slack-Webhook. Der Server meldet, "
            "wenn sich das Display länger nicht gemeldet hat, und wenn es wieder da ist. Leer = aus."
        ),
    },
    {
        "name":    "NOTIFY_OFFLINE_MINUTES",
        "label":   "Ausfall melden nach (min)",
        "type":    "number",
        "section": "framework",
        "wide":    False,
        "default": "30",
        "min":     0,
        "max":     1440,
        "help":    "Minuten ohne Rückmeldung des Geräts, bis die Nachricht rausgeht. 0 = nie.",
    },
    {
        "name":    "NOTIFY_EVENTS",
        "label":   "Ereignisse",
        "type":    "checkbox_group",
        "section": "framework",
        "wide":    True,
        "default": "offline,firmware",
        "options": [],   # aus app.notifications.EVENT_OPTIONS (zur Laufzeit befüllt)
        "help":    "Was gemeldet wird. Tagesbild und Wochenbericht bringen das aktuelle Display-Bild mit (Discord und ntfy).",
    },
    {
        "name":    "NOTIFY_DAILY_HOUR",
        "label":   "Tagesbild und Wochenbericht um (Uhr)",
        "type":    "number",
        "section": "framework",
        "wide":    False,
        "default": "7",
        "min":     0,
        "max":     23,
        "help":    "Stunde, ab der das Tagesbild (täglich) und der Wochenbericht (montags) verschickt werden.",
    },
    {
        "name":        "NOTIFY_BASE_URL",
        "label":       "Adresse des Servers",
        "type":        "text",
        "section":     "framework",
        "wide":        True,
        "default":     "",
        "placeholder": "http://192.168.178.6:8787",
        "help":        "Wie du den Server im Browser aufrufst. Damit bekommen Nachrichten einen Link zur Gerät-Seite, und Slack kann das Bild einbinden.",
    },
    {
        "name":        "NOTIFY_AVATAR_URL",
        "label":       "Profilbild",
        "type":        "text",
        "section":     "framework",
        "wide":        True,
        "default":     "",
        "placeholder": "leer = Logo aus dem GitHub-Repo",
        "help":        "Öffentlich erreichbare Bildadresse für Discord (Profilbild) und ntfy (Icon). Leer nimmt das Projektlogo.",
    },
    # ── Lokalisierung ────────────────────────────────────────────────────────
    {
        "name":        "TIMEZONE",
        "label":       "Zeitzone",
        "type":        "text",
        "section":     "framework",
        "wide":        False,
        "placeholder": "Europe/Berlin",
        "help":        "IANA-Zeitzonenname (z. B. Europe/Berlin, Europe/Vienna).",
        "link_href":   "https://en.wikipedia.org/wiki/List_of_tz_database_time_zones",
        "link_label":  "Liste aller Zeitzonen",
    },
    # ── Idle-Verwaltung ──────────────────────────────────────────────────────
    {
        "name":    "IDLE_MODULES",
        "label":   "Aktive Idle-Module",
        "type":    "checkbox_group",
        "section": "framework",
        "wide":    True,
        "options": [],   # wird zur Laufzeit aus der Registry befüllt
        "help":    (
            "Welche Idle-Module aktiv sind. "
            "Wenn mehrere aktiv sind, wird zwischen ihnen rotiert."
        ),
    },
    {
        "name":    "IDLE_LAYOUT",
        "label":   "Idle-Darstellung",
        "type":    "select",
        "section": "framework",
        "wide":    True,
        "default": "rotation",
        "options": [
            ("rotation",  "Rotation – ein Modul pro Bild, im Wechsel"),
            ("dashboard", "Dashboard – mehrere Module gleichzeitig als Kacheln"),
        ],
        "help": (
            "Rotation zeigt die aktiven Idle-Module nacheinander als Vollbild. "
            "Dashboard setzt sie untereinander in ein Bild – weniger Display-Refreshes, "
            "mehr Information auf einen Blick."
        ),
    },
    {
        "name":        "DASHBOARD_TILES",
        "label":       "Dashboard-Kacheln",
        "type":        "text",
        "section":     "framework",
        "wide":        True,
        "default":     "",
        "placeholder": "dwd_weather:45, calendar:30, garbage:25",
        "help": (
            "Reihenfolge von oben nach unten als 'modul:höhe' in Prozent, kommagetrennt. "
            "Nur Module mit Kachel-Unterstützung (Wetter, Kalender, Müllabfuhr, Tagesschau, Gallery). "
            "Leer: alle aktiven Idle-Module gleich hoch."
        ),
        "show_when":   {"IDLE_LAYOUT": "dashboard"},
    },
    {
        "name":        "IDLE_MODULE_ROTATION_SECONDS",
        "label":       "Idle-Rotation (s)",
        "type":        "number",
        "section":     "framework",
        "wide":        False,
        "placeholder": "120",
        "min":         30,
        "max":         3600,
        "help":        "Intervall in Sekunden für die Rotation zwischen mehreren Idle-Modulen.",
    },
    {
        "name":        "SCHEDULE_WINDOWS",
        "label":       "Zeitplan",
        "type":        "text",
        "section":     "framework",
        "wide":        True,
        "default":     "",
        "placeholder": "Morgens|Mo-Fr|06:00-09:00|rotation|120|dwd_weather,garbage; Nachts|*|23:00-07:00||900|",
        "help": (
            "Zeitfenster mit eigenen Inhalten, Darstellung und Takt: 'Name|Tage|HH:MM-HH:MM|layout|sekunden|inhalte', "
            "mehrere per Semikolon. Leere Teile erben vom Programm. Wird auf der Anzeige-Seite gepflegt."
        ),
    },
    {
        "name":    "NIGHT_MODE_ENABLED",
        "label":   "Nachtmodus",
        "type":    "select",
        "section": "framework",
        "wide":    False,
        "default": "false",
        "options": [("false", "Deaktiviert"), ("true", "Aktiviert")],
        "help":    "Reduziert nachts das Update-Intervall und kann die Idle-Auswahl begrenzen.",
    },
    {
        "name":        "NIGHT_MODE_START",
        "label":       "Nachtmodus ab",
        "type":        "text",
        "section":     "framework",
        "wide":        False,
        "default":     "23:00",
        "placeholder": "23:00",
        "help":        "Lokale Uhrzeit im Format HH:MM, ab der der Nachtmodus aktiv wird.",
        "show_when":   {"NIGHT_MODE_ENABLED": "true"},
    },
    {
        "name":        "NIGHT_MODE_END",
        "label":       "Nachtmodus bis",
        "type":        "text",
        "section":     "framework",
        "wide":        False,
        "default":     "07:00",
        "placeholder": "07:00",
        "help":        "Lokale Uhrzeit im Format HH:MM, bis zu der der Nachtmodus aktiv bleibt.",
        "show_when":   {"NIGHT_MODE_ENABLED": "true"},
    },
    {
        "name":        "NIGHT_MODE_INTERVAL_MINUTES",
        "label":       "Update-Intervall nachts (min)",
        "type":        "number",
        "section":     "framework",
        "wide":        False,
        "default":     "15",
        "placeholder": "15",
        "min":         1,
        "max":         720,
        "help":        "Wie oft Idle-Inhalte nachts aktualisiert werden sollen.",
        "show_when":   {"NIGHT_MODE_ENABLED": "true"},
    },
    {
        "name":    "NIGHT_MODE_IDLE_BEHAVIOR",
        "label":   "Idle-Verhalten nachts",
        "type":    "select",
        "section": "framework",
        "wide":    True,
        "default": "rotate",
        "options": [
            ("rotate", "Weiter zwischen aktiven Idle-Modulen rotieren"),
            ("fixed", "Nur ein festes Idle-Modul verwenden"),
        ],
        "help":    "Legt fest, ob nachts weiter rotiert wird oder nur ein einzelnes Idle-Modul aktiv ist.",
        "show_when": {"NIGHT_MODE_ENABLED": "true"},
    },
    {
        "name":    "NIGHT_MODE_FIXED_MODULE",
        "label":   "Festes Nachtmodul",
        "type":    "select",
        "section": "framework",
        "wide":    False,
        "default": "",
        "options": [],
        "help":    "Dieses Idle-Modul bleibt nachts dauerhaft aktiv.",
        "show_when": {"NIGHT_MODE_ENABLED": "true", "NIGHT_MODE_IDLE_BEHAVIOR": "fixed"},
    },
]

SETTINGS_FIELD_ORDER = [f["name"] for f in SETTINGS_FIELDS]

# Framework-Untergruppen für die Settings-Seite
SETTINGS_GROUPS: list[dict] = [
    {
        "title":  "Render-Ausgabe",
        "desc":   "Größe, Rotation, Theme und Dateiformat des erzeugten Bildes.",
        "fields": [
            "REFRESH_INTERVAL",
            "RENDER_WIDTH", "RENDER_HEIGHT",
            "DISPLAY_ROTATION", "DISPLAY_THEME", "OUTPUT_FORMAT", "SHOW_RENDER_TIME",
        ],
    },
    {
        "title":  "Panelpflege",
        "desc":   "Regelmäßiger Reinigungslauf gegen Geisterbilder auf dem E-Ink-Panel.",
        "fields": ["PANEL_CLEAN_INTERVAL_DAYS", "PANEL_CLEAN_HOUR"],
    },
    {
        "title":  "Benachrichtigung",
        "desc":   "Nachricht aufs Handy, wenn das Display ausfällt oder wieder da ist.",
        "fields": ["NOTIFY_URL", "NOTIFY_OFFLINE_MINUTES", "NOTIFY_EVENTS", "NOTIFY_DAILY_HOUR", "NOTIFY_BASE_URL", "NOTIFY_AVATAR_URL"],
    },
    {
        "title":  "Lokalisierung",
        "desc":   "Zeitzone für Uhrzeiten und lokale Daten.",
        "fields": ["TIMEZONE"],
    },
    {
        "title":  "Idle-Verwaltung",
        "desc":   "Welche Idle-Module angezeigt werden und wie zwischen ihnen rotiert wird.",
        "fields": [
            "IDLE_MODULES",
            "IDLE_LAYOUT",
            "DASHBOARD_TILES",
            "IDLE_MODULE_ROTATION_SECONDS",
            "SCHEDULE_WINDOWS",
            "NIGHT_MODE_ENABLED",
            "NIGHT_MODE_START",
            "NIGHT_MODE_END",
            "NIGHT_MODE_INTERVAL_MINUTES",
            "NIGHT_MODE_IDLE_BEHAVIOR",
            "NIGHT_MODE_FIXED_MODULE",
        ],
    },
]


TIME_OF_DAY_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


def parse_time_of_day_to_minutes(raw_value: str) -> int | None:
    value = (raw_value or "").strip()
    if not TIME_OF_DAY_RE.match(value):
        return None
    hour, minute = value.split(":", 1)
    return int(hour) * 60 + int(minute)


# ---------------------------------------------------------------------------
# Parse-Hilfsfunktionen
# ---------------------------------------------------------------------------

def parse_idle_module_ids(raw_value: str) -> tuple[str, ...]:
    # Gültige IDs kommen jetzt aus der Registry – wir akzeptieren alles
    # (Registry prüft beim Laden ob das Modul existiert)
    parsed = []
    for item in (raw_value or "").split(","):
        mid = item.strip().lower()
        if mid and mid not in parsed:
            parsed.append(mid)
    return tuple(parsed)


def parse_dashboard_tiles(raw_value: str) -> tuple[tuple[str, int], ...]:
    """
    'dwd_weather:45, calendar:30, garbage' → (('dwd_weather', 45), ('calendar', 30), ('garbage', 0)).
    0 = Höhe wird später gleichmäßig aus dem Rest verteilt. Reihenfolge bleibt erhalten.
    """
    tiles: list[tuple[str, int]] = []
    seen: set[str] = set()
    for chunk in (raw_value or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        module_id, _, pct_raw = chunk.partition(":")
        module_id = module_id.strip().lower()
        if not module_id or module_id in seen:
            continue
        try:
            pct = max(0, min(100, int(pct_raw.strip()))) if pct_raw.strip() else 0
        except ValueError:
            pct = 0
        tiles.append((module_id, pct))
        seen.add(module_id)
    return tuple(tiles)


def parse_display_rotation(raw_value: str) -> int:
    return DISPLAY_ROTATION_ALIASES.get(raw_value.strip().lower(), 0)


def get_effective_render_size(base_w: int, base_h: int, rotation: int) -> tuple[int, int]:
    if rotation in {90, 270}:
        return base_h, base_w
    return base_w, base_h


def should_flip_output(rotation: int) -> bool:
    return rotation in {180, 270}


def parse_bool_env(raw_value: str | None, default: bool) -> bool:
    if raw_value is None:
        return default
    return raw_value.strip().lower() == "true"


def as_env_value(value) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value).strip()


def get_env_value(settings: dict[str, str], key: str, default: str) -> str:
    value = settings.get(key)
    if value is None or value == "":
        return default
    return str(value).strip()


def _parse_int(settings: dict, key: str, default: int,
               min_val: int | None = None, max_val: int | None = None) -> int:
    try:
        v = int(get_env_value(settings, key, str(default)))
        if min_val is not None:
            v = max(min_val, v)
        if max_val is not None:
            v = min(max_val, v)
        return v
    except ValueError:
        try:
            from app.logger import get_logger as _gl
            _gl("config").warning(f"Ungültiger Wert für {key}, nutze Default {default}")
        except Exception:
            import sys
            print(f"[WARN] Ungültiger Wert für {key}, nutze Default {default}", file=sys.stderr)
        return default


# ---------------------------------------------------------------------------
# RuntimeConfig – thread-sichere Konfiguration
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class RuntimeConfig:
    # Framework
    base_render_width:  int  = 1600
    base_render_height: int  = 1200
    refresh_interval:   int  = 60
    display_rotation:   int  = 0
    display_theme:      str  = "dark"
    output_format:      str  = "png"
    show_render_time:   bool = False
    panel_clean_interval_days: int = 14
    panel_clean_hour:   int  = 3
    render_width:       int  = 1600
    render_height:      int  = 1200
    timezone:           str  = "Europe/Berlin"
    # Idle-Verwaltung
    idle_module_ids:               tuple = ()
    idle_module_rotation_seconds:  int   = 120
    idle_layout:                   str   = "rotation"     # rotation | dashboard
    dashboard_tiles:               tuple = ()             # ((module_id, prozent), …)
    schedule_windows:              tuple = ()             # (schedule.Window, …) – leer: alter Nachtmodus gilt
    notify_url:                    str   = ""             # ntfy/Discord/Slack – leer: keine Benachrichtigung
    notify_offline_minutes:        int   = 30             # 0 = nie melden
    notify_events:                 tuple = ("offline", "firmware")
    notify_daily_hour:             int   = 7
    notify_base_url:               str   = ""
    notify_avatar_url:             str   = ""
    night_mode_enabled:            bool  = False
    night_mode_start:              str   = "23:00"
    night_mode_end:                str   = "07:00"
    night_mode_start_minutes:      int   = 1380
    night_mode_end_minutes:        int   = 420
    night_mode_interval_minutes:   int   = 15
    night_mode_interval_seconds:   int   = 900
    night_mode_idle_behavior:      str   = "rotate"
    night_mode_fixed_module_id:    str   = ""
    # Vollständige Settingsmap inkl. Modul-Feldern
    settings_values: dict[str, str] = field(default_factory=dict)


_cfg: RuntimeConfig = RuntimeConfig()
# Überschreibung nur für den laufenden Thread (Vorschau mit anderem Theme,
# eingefrorener Stand während eines Renders). Andere Threads sehen weiter _cfg.
_cfg_override: contextvars.ContextVar[RuntimeConfig | None] = contextvars.ContextVar("inkwall_cfg_override", default=None)


def get_cfg() -> RuntimeConfig:
    """Immer per Funktionsaufruf lesen – niemals _cfg direkt importieren!"""
    return _cfg_override.get() or _cfg


# Alle Display-Themes, die Module kennen müssen (Settings-Select + Vorschau).
# "eink": nur die sechs Spectra-6-Farben, flach, ohne Blur – siehe SPECTRA6_COLORS.
AVAILABLE_THEMES: tuple[str, ...] = ("dark", "light", "eink")
THEME_LABELS = {"dark": "Dark", "light": "Light", "eink": "E-Ink"}


def is_flat_theme(theme: str) -> bool:
    """True für Themes ohne Blur/Verläufe/Transparenz (derzeit nur eink)."""
    return theme == "eink"


@contextlib.contextmanager
def override_runtime_config(**changes):
    """
    Ersetzt die RuntimeConfig vorübergehend, aber nur für den laufenden
    Thread (z. B. display_theme für eine Vorschau). Ein paralleler Render,
    das Speichern oder eine andere Anfrage sehen die Änderung nie. Stellt
    beim Verlassen den vorherigen Stand wieder her, auch bei Exceptions.
    Ohne Änderungen friert der Block den aktuellen Stand ein: wer in der
    Zwischenzeit speichert, ändert den laufenden Render nicht mittendrin.
    """
    previous = get_cfg()
    current = previous
    if changes:
        settings = dict(previous.settings_values)
        if "display_theme" in changes:
            settings["DISPLAY_THEME"] = str(changes["display_theme"])
        current = _dc_replace(previous, settings_values=settings, **changes)
    token = _cfg_override.set(current)
    try:
        yield current
    finally:
        _cfg_override.reset(token)


def get_setting(name: str, default: str = "") -> str:
    value = get_cfg().settings_values.get(name)
    if value is None or value == "":
        return default
    return str(value)


def get_bool_setting(name: str, default: bool = False) -> bool:
    raw = get_cfg().settings_values.get(name)
    return parse_bool_env(raw, default)


def get_int_setting(
    name: str,
    default: int,
    min_val: int | None = None,
    max_val: int | None = None,
) -> int:
    raw = get_cfg().settings_values.get(name)
    if raw is None or raw == "":
        return default
    try:
        value = int(str(raw).strip())
    except ValueError:
        return default
    if min_val is not None:
        value = max(min_val, value)
    if max_val is not None:
        value = min(max_val, value)
    return value


def get_csv_setting(name: str) -> tuple[str, ...]:
    raw = get_setting(name, "")
    if not raw:
        return ()
    seen: list[str] = []
    for item in raw.split(","):
        cleaned = item.strip()
        if cleaned and cleaned not in seen:
            seen.append(cleaned)
    return tuple(seen)


# ---------------------------------------------------------------------------
# Env-IO
# ---------------------------------------------------------------------------

_ENV_KEY_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
# Zeichen, bei denen der Wert in Anführungszeichen muss, damit dotenv ihn
# unverändert zurückliest (# wäre sonst ein Kommentar, Leerzeichen am Rand
# gingen verloren, Quotes/Backslashes würden interpretiert).
_ENV_NEEDS_QUOTING_RE = re.compile(r"""[#"'\\\s$`]""")


def format_env_value(value) -> str:
    """Formatiert einen Wert für eine KEY=VALUE-Zeile, sodass dotenv ihn exakt zurückliest."""
    text = as_env_value(value)
    # Zeilenumbrüche würden neue Keys injizieren – niemals durchlassen
    text = text.replace("\r", " ").replace("\n", " ")
    if text == "" or not _ENV_NEEDS_QUOTING_RE.search(text):
        return text
    escaped = text.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def read_env_settings() -> dict[str, str]:
    if not ENV_FILE_PATH.exists():
        return {}
    return {
        key: as_env_value(value)
        # interpolate=False: ein "${HOME}" im Wert bleibt Text
        for key, value in dotenv_values(ENV_FILE_PATH, interpolate=False).items()
        if value is not None
    }


# Lesen, Ändern, Schreiben und Übernehmen der Einstellungen am Stück: zwei
# gleichzeitige Speichervorgänge (zwei Tabs, zwei Karten kurz nacheinander)
# würden sonst eine der Änderungen verlieren. Wer schreibt und danach
# apply_runtime_config() aufruft, hält die Sperre über beides.
settings_lock = threading.RLock()


def write_env_settings(updates: dict[str, str]) -> None:
    """Schreibt die aktive Env-Konfigurationsdatei atomar via Temp-Datei + rename."""
    for key in updates:
        if not _ENV_KEY_RE.match(key):
            raise ValueError(f"Ungültiger Settings-Key: {key!r}")
    with settings_lock:
        _write_env_settings_locked(updates)


def _write_env_settings_locked(updates: dict[str, str]) -> None:
    ENV_FILE_PATH.parent.mkdir(parents=True, exist_ok=True)
    lines = ENV_FILE_PATH.read_text(encoding="utf-8").splitlines() if ENV_FILE_PATH.exists() else []

    remaining = dict(updates)
    new_lines: list[str] = []

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key = line.split("=", 1)[0].strip()
        if key in remaining:
            new_lines.append(f"{key}={format_env_value(remaining.pop(key))}")
        else:
            new_lines.append(line)

    for key in SETTINGS_FIELD_ORDER:
        if key in remaining:
            new_lines.append(f"{key}={format_env_value(remaining.pop(key))}")

    for key, value in remaining.items():
        new_lines.append(f"{key}={format_env_value(value)}")

    content = "\n".join(new_lines).rstrip() + "\n"
    # Eigener Temp-Name je Aufruf: ein fester Name ließe zwei Schreiber dieselbe Datei tauschen
    fd, tmp_name = tempfile.mkstemp(prefix=f".{ENV_FILE_PATH.name}.", suffix=".tmp", dir=ENV_FILE_PATH.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(content)
        _copy_mode(ENV_FILE_PATH, tmp_name)
        os.replace(tmp_name, ENV_FILE_PATH)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _copy_mode(original: Path, tmp_name: str) -> None:
    """mkstemp legt 0600 an – die Rechte der bisherigen Datei übernehmen, sonst 0600 lassen."""
    try:
        os.chmod(tmp_name, os.stat(original).st_mode & 0o777)
    except OSError:
        pass


def validate_settings(updates: dict[str, str], all_fields: list[dict] | None = None) -> list[str]:
    """Serverseitige Validierung. Gibt Liste mit Fehlermeldungen zurück."""
    errors: list[str] = []

    fields_to_check = (all_fields or []) + SETTINGS_FIELDS
    field_map = {f["name"]: f for f in fields_to_check}
    for key, raw in updates.items():
        f = field_map.get(key)
        if not f or f.get("type") != "number":
            continue
        try:
            v = int(raw)
            if "min" in f and v < f["min"]:
                errors.append(f"{f['label']}: Mindestwert ist {f['min']}.")
            if "max" in f and v > f["max"]:
                errors.append(f"{f['label']}: Maximalwert ist {f['max']}.")
        except ValueError:
            errors.append(f"{f['label']}: Muss eine ganze Zahl sein.")

    if parse_bool_env(updates.get("NIGHT_MODE_ENABLED"), False):
        start_raw = get_env_value(updates, "NIGHT_MODE_START", "23:00")
        end_raw = get_env_value(updates, "NIGHT_MODE_END", "07:00")
        start_minutes = parse_time_of_day_to_minutes(start_raw)
        end_minutes = parse_time_of_day_to_minutes(end_raw)
        if start_minutes is None:
            errors.append("Nachtmodus ab: Bitte Uhrzeit im Format HH:MM angeben.")
        if end_minutes is None:
            errors.append("Nachtmodus bis: Bitte Uhrzeit im Format HH:MM angeben.")
        if start_minutes is not None and end_minutes is not None and start_minutes == end_minutes:
            errors.append("Nachtmodus: Start und Ende dürfen nicht identisch sein.")

        behavior = get_env_value(updates, "NIGHT_MODE_IDLE_BEHAVIOR", "rotate")
        if behavior not in {"rotate", "fixed"}:
            errors.append("Idle-Verhalten nachts: Ungültige Auswahl.")
        if behavior == "fixed":
            fixed_module = get_env_value(updates, "NIGHT_MODE_FIXED_MODULE", "")
            active_idle = parse_idle_module_ids(get_env_value(updates, "IDLE_MODULES", ""))
            if not fixed_module:
                errors.append("Festes Nachtmodul: Bitte ein Idle-Modul auswählen.")
            elif fixed_module not in active_idle:
                errors.append("Festes Nachtmodul: Das Modul muss auch in den aktiven Idle-Modulen enthalten sein.")

    if "SCHEDULE_WINDOWS" in updates:
        from app.schedule import validate_raw
        errors.extend(validate_raw(updates.get("SCHEDULE_WINDOWS", "")))

    notify_url = get_env_value(updates, "NOTIFY_URL", "").strip()
    if notify_url and not notify_url.lower().startswith(("http://", "https://")):
        errors.append("Benachrichtigung an: Bitte eine Adresse mit http:// oder https:// angeben.")
    for key, label in (("NOTIFY_BASE_URL", "Adresse des Servers"), ("NOTIFY_AVATAR_URL", "Profilbild")):
        value = get_env_value(updates, key, "").strip()
        if value and not value.lower().startswith(("http://", "https://")):
            errors.append(f"{label}: Bitte eine Adresse mit http:// oder https:// angeben.")

    return errors


def apply_runtime_config(settings: dict[str, str] | None = None) -> None:
    """Baut eine neue RuntimeConfig und ersetzt _cfg atomar."""
    global _cfg
    settings = settings or read_env_settings()

    base_w   = _parse_int(settings, "RENDER_WIDTH",  1600, 400, 3840)
    base_h   = _parse_int(settings, "RENDER_HEIGHT", 1200, 300, 2160)
    # Gerade Maße: das kompakte Panel-Format packt zwei Pixel je Byte, und mit
    # 90°/270° wird die Höhe zur Breite – eine ungerade Zahl ließe jeden Render scheitern
    base_w  -= base_w % 2
    base_h  -= base_h % 2
    rotation = parse_display_rotation(get_env_value(settings, "DISPLAY_ROTATION", "0"))
    render_w, render_h = get_effective_render_size(base_w, base_h, rotation)

    idle_modules_raw = get_env_value(settings, "IDLE_MODULES", "")
    refresh_interval = _parse_int(settings, "REFRESH_INTERVAL", 60, 10, 3600)
    output_format = get_env_value(settings, "OUTPUT_FORMAT", "png")
    show_render_time = parse_bool_env(settings.get("SHOW_RENDER_TIME"), False)
    panel_clean_interval_days = _parse_int(settings, "PANEL_CLEAN_INTERVAL_DAYS", 14, 0, 365)
    panel_clean_hour = _parse_int(settings, "PANEL_CLEAN_HOUR", 3, 0, 23)
    notify_url = get_env_value(settings, "NOTIFY_URL", "").strip()
    notify_offline_minutes = _parse_int(settings, "NOTIFY_OFFLINE_MINUTES", 30, 0, 1440)
    notify_events_raw = settings.get("NOTIFY_EVENTS")
    notify_events = parse_idle_module_ids("offline,firmware" if notify_events_raw is None else str(notify_events_raw))
    notify_daily_hour = _parse_int(settings, "NOTIFY_DAILY_HOUR", 7, 0, 23)
    notify_base_url = get_env_value(settings, "NOTIFY_BASE_URL", "").strip().rstrip("/")
    notify_avatar_url = get_env_value(settings, "NOTIFY_AVATAR_URL", "").strip()
    display_theme = get_env_value(settings, "DISPLAY_THEME", "dark").strip().lower()
    if display_theme not in AVAILABLE_THEMES:
        display_theme = "dark"
    timezone_name = get_env_value(settings, "TIMEZONE", "Europe/Berlin")
    idle_module_ids = parse_idle_module_ids(idle_modules_raw)
    idle_rotation_seconds = _parse_int(settings, "IDLE_MODULE_ROTATION_SECONDS", 120, 30, 3600)
    idle_layout = get_env_value(settings, "IDLE_LAYOUT", "rotation").strip().lower()
    if idle_layout not in {"rotation", "dashboard"}:
        idle_layout = "rotation"
    dashboard_tiles = parse_dashboard_tiles(get_env_value(settings, "DASHBOARD_TILES", ""))
    from app.schedule import parse_windows, serialize_windows
    schedule_windows = tuple(parse_windows(get_env_value(settings, "SCHEDULE_WINDOWS", "")))
    night_mode_enabled = parse_bool_env(settings.get("NIGHT_MODE_ENABLED"), False)
    night_mode_start = get_env_value(settings, "NIGHT_MODE_START", "23:00")
    night_mode_end = get_env_value(settings, "NIGHT_MODE_END", "07:00")
    night_mode_start_minutes = parse_time_of_day_to_minutes(night_mode_start)
    if night_mode_start_minutes is None:
        night_mode_start = "23:00"
        night_mode_start_minutes = 23 * 60
    night_mode_end_minutes = parse_time_of_day_to_minutes(night_mode_end)
    if night_mode_end_minutes is None:
        night_mode_end = "07:00"
        night_mode_end_minutes = 7 * 60
    night_mode_interval_minutes = _parse_int(settings, "NIGHT_MODE_INTERVAL_MINUTES", 15, 1, 720)
    night_mode_idle_behavior = get_env_value(settings, "NIGHT_MODE_IDLE_BEHAVIOR", "rotate")
    if night_mode_idle_behavior not in {"rotate", "fixed"}:
        night_mode_idle_behavior = "rotate"
    night_mode_fixed_module_id = get_env_value(settings, "NIGHT_MODE_FIXED_MODULE", "")

    normalized_settings = {
        key: as_env_value(value)
        for key, value in settings.items()
        if value is not None
    }
    normalized_settings.update({
        "REFRESH_INTERVAL": as_env_value(refresh_interval),
        "RENDER_WIDTH": as_env_value(base_w),
        "RENDER_HEIGHT": as_env_value(base_h),
        "DISPLAY_ROTATION": as_env_value(rotation),
        "DISPLAY_THEME": as_env_value(display_theme),
        "OUTPUT_FORMAT": as_env_value(output_format),
        "SHOW_RENDER_TIME": as_env_value(show_render_time),
        "PANEL_CLEAN_INTERVAL_DAYS": as_env_value(panel_clean_interval_days),
        "PANEL_CLEAN_HOUR": as_env_value(panel_clean_hour),
        "NOTIFY_URL": as_env_value(notify_url),
        "NOTIFY_OFFLINE_MINUTES": as_env_value(notify_offline_minutes),
        "NOTIFY_EVENTS": as_env_value(",".join(notify_events)),
        "NOTIFY_DAILY_HOUR": as_env_value(notify_daily_hour),
        "NOTIFY_BASE_URL": as_env_value(notify_base_url),
        "NOTIFY_AVATAR_URL": as_env_value(notify_avatar_url),
        "TIMEZONE": as_env_value(timezone_name),
        "IDLE_MODULES": as_env_value(",".join(idle_module_ids)),
        "IDLE_MODULE_ROTATION_SECONDS": as_env_value(idle_rotation_seconds),
        "IDLE_LAYOUT": as_env_value(idle_layout),
        "DASHBOARD_TILES": as_env_value(", ".join(f"{m}:{p}" if p else m for m, p in dashboard_tiles)),
        "SCHEDULE_WINDOWS": as_env_value(serialize_windows(list(schedule_windows))),
        "NIGHT_MODE_ENABLED": as_env_value(night_mode_enabled),
        "NIGHT_MODE_START": as_env_value(night_mode_start),
        "NIGHT_MODE_END": as_env_value(night_mode_end),
        "NIGHT_MODE_INTERVAL_MINUTES": as_env_value(night_mode_interval_minutes),
        "NIGHT_MODE_IDLE_BEHAVIOR": as_env_value(night_mode_idle_behavior),
        "NIGHT_MODE_FIXED_MODULE": as_env_value(night_mode_fixed_module_id),
    })

    _cfg = RuntimeConfig(
        base_render_width=base_w,
        base_render_height=base_h,
        refresh_interval=refresh_interval,
        display_rotation=rotation,
        display_theme=display_theme,
        output_format=output_format,
        show_render_time=show_render_time,
        panel_clean_interval_days=panel_clean_interval_days,
        panel_clean_hour=panel_clean_hour,
        notify_url=notify_url,
        notify_offline_minutes=notify_offline_minutes,
        notify_events=notify_events,
        notify_daily_hour=notify_daily_hour,
        notify_base_url=notify_base_url,
        notify_avatar_url=notify_avatar_url,
        render_width=render_w,
        render_height=render_h,
        timezone=timezone_name,
        idle_module_ids=idle_module_ids,
        idle_module_rotation_seconds=idle_rotation_seconds,
        idle_layout=idle_layout,
        dashboard_tiles=dashboard_tiles,
        schedule_windows=schedule_windows,
        night_mode_enabled=night_mode_enabled,
        night_mode_start=night_mode_start,
        night_mode_end=night_mode_end,
        night_mode_start_minutes=night_mode_start_minutes,
        night_mode_end_minutes=night_mode_end_minutes,
        night_mode_interval_minutes=night_mode_interval_minutes,
        night_mode_interval_seconds=night_mode_interval_minutes * 60,
        night_mode_idle_behavior=night_mode_idle_behavior,
        night_mode_fixed_module_id=night_mode_fixed_module_id,
        settings_values=normalized_settings,
    )


def get_settings_values() -> dict[str, str]:
    """Gibt alle aktuellen Konfigurationswerte als String-Dict zurück."""
    return dict(get_cfg().settings_values)


def collect_settings_form_data(form, all_fields: list[dict]) -> dict[str, str]:
    """
    Liest alle Formularfelder aus und gibt ein dict[str, str] für write_env_settings zurück.
    all_fields: kombinierte Liste aus Framework-Feldern + Modul-Feldern.
    """
    updates: dict[str, str] = {}
    current_values = get_settings_values()
    for f in all_fields:
        name = f["name"]
        if f.get("type") == "password" and not form.get(name, "").strip():
            # Passwort-Felder werden leer ausgeliefert; leer abschicken = beibehalten
            updates[name] = current_values.get(name, "")
        elif f.get("type") in ("checkbox_group", "priority_list"):
            updates[name] = ",".join(form.getlist(name))
        else:
            updates[name] = as_env_value(form.get(name, ""))
    return updates


def get_settings_runtime_summary() -> dict[str, str]:
    cfg = get_cfg()
    return {
        "render_size":         f"{cfg.render_width}x{cfg.render_height}",
        "rotation":            f"{cfg.display_rotation}°",
        "theme":               THEME_LABELS.get(cfg.display_theme, cfg.display_theme),
        "output_format":       "BMP (Spectra 6)" if cfg.output_format == "bmp" else "PNG",
        "idle_modules":        ", ".join(cfg.idle_module_ids) if cfg.idle_module_ids else "Keine",
        "idle_layout":         "Dashboard" if cfg.idle_layout == "dashboard" else "Rotation",
        "idle_rotation":       f"{cfg.idle_module_rotation_seconds}s",
        "night_mode":          (
            f"Aktiv {cfg.night_mode_start}–{cfg.night_mode_end}, alle {cfg.night_mode_interval_minutes} min"
            if cfg.night_mode_enabled else "Deaktiviert"
        ),
    }


# ---------------------------------------------------------------------------
# Lokale Zeit (Container laufen in UTC – nie datetime.now() ohne TZ nutzen)
# ---------------------------------------------------------------------------

WEEKDAYS_DE = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
WEEKDAYS_DE_LONG = ("Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag")
MONTHS_DE = ("Januar", "Februar", "März", "April", "Mai", "Juni", "Juli",
             "August", "September", "Oktober", "November", "Dezember")


def format_date_long(d=None) -> str:
    """'Mittwoch, 3. September' – für Kopfzeilen auf dem Display."""
    d = d or now_local()
    return f"{WEEKDAYS_DE_LONG[d.weekday()]}, {d.day}. {MONTHS_DE[d.month - 1]}"


def local_tz():
    """ZoneInfo der konfigurierten Zeitzone, Fallback Europe/Berlin, dann UTC."""
    from datetime import timezone as _tz
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
    name = get_cfg().timezone or "Europe/Berlin"
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError:
        try:
            return ZoneInfo("Europe/Berlin")
        except ZoneInfoNotFoundError:
            return _tz.utc


def now_local():
    """Aktuelle Zeit als tz-aware datetime in der konfigurierten Zeitzone."""
    from datetime import datetime as _dt
    return _dt.now(local_tz())


def format_weekday_short(d) -> str:
    """'Mi' für einen date/datetime – unabhängig vom System-Locale."""
    return WEEKDAYS_DE[d.weekday()]


# ---------------------------------------------------------------------------
# Font-Loading mit Cache
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def load_font(size: int, is_bold: bool = False):
    if os.name == "nt":
        candidates = (
            ["C:/Windows/Fonts/arialbd.ttf", "C:/Windows/Fonts/calibrib.ttf", "C:/Windows/Fonts/segoeuib.ttf"]
            if is_bold else
            ["C:/Windows/Fonts/arial.ttf", "C:/Windows/Fonts/calibri.ttf", "C:/Windows/Fonts/segoeui.ttf"]
        )
    else:
        # fonts-dejavu-core (Debian/Ubuntu) oder ttf-dejavu (Arch)
        candidates = (
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
            ]
            if is_bold else
            [
                "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/TTF/DejaVuSans.ttf",
                "/usr/share/fonts/dejavu/DejaVuSans.ttf",
            ]
        )
    for path in candidates:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                pass
    # Kein TrueType-Font gefunden – Bitmap-Fallback (kein anchor-Support!)
    import logging as _logging
    _logging.getLogger("inkwall.config").warning(
        f"load_font({size}, bold={is_bold}): kein TrueType-Font gefunden "
        f"(Kandidaten: {candidates}). Bitmap-Fallback aktiv – "
        f"anchor-Parameter in draw.text() funktioniert NICHT."
    )
    return ImageFont.load_default()


# Initiale Konfiguration laden
apply_runtime_config()
