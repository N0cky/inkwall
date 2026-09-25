"""
Zentrales Logging für Inkwall.

Verwendung in jedem Modul:
    from app.logger import get_logger
    log = get_logger(__name__)

Log-Dateien liegen in <project_root>/logs/ (tägl. Rotation, 7 Tage).
Format der Dateien: JSON-Lines (eine JSON-Zeile pro Eintrag) – für einfaches
Parsen durch den /api/logs-Endpunkt.
"""

from __future__ import annotations

import json
import logging
import logging.handlers
from datetime import datetime, timezone
from pathlib import Path

# ---------------------------------------------------------------------------
# Pfade
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# Log-Pfad: per PLEXINK_LOGS_DIR überschreibbar (Docker: /logs)
import os as _os
_logs_env = (_os.environ.get("INKWALL_LOGS_DIR", "").strip() or _os.environ.get("PLEXINK_LOGS_DIR", "").strip())   # alter Präfix gilt weiter
LOGS_DIR  = Path(_logs_env) if _logs_env else PROJECT_ROOT / "logs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)

LOG_FILE = LOGS_DIR / "app.jsonl"


# ---------------------------------------------------------------------------
# Formatter
# ---------------------------------------------------------------------------

import re as _re

# Query-Parameter, deren Werte nie im Log landen dürfen (Plex-Token, Steam-Key, …).
_SECRET_PARAM_RE = _re.compile(
    r"([?&](?:X-Plex-Token|key|api_key|apikey|token|access_token|password|secret)=)[^&\s'\"]+",
    _re.IGNORECASE,
)
# Geheimnisse im Pfad einer URL: Webhook-Tokens, private Kalender-Links,
# Zugangsdaten vor dem @. requests nennt die URL in jeder Fehlermeldung.
_SECRET_PATH_RES = (
    (_re.compile(r"(discord(?:app)?\.com/api/webhooks/\d+/)[\w-]+", _re.IGNORECASE), r"\1***"),
    (_re.compile(r"(hooks\.slack\.com/services/[^/\s]+/[^/\s]+/)[\w-]+", _re.IGNORECASE), r"\1***"),
    (_re.compile(r"(ntfy\.sh/)[\w-]+", _re.IGNORECASE), r"\1***"),
    (_re.compile(r"(/private-)[0-9a-f]{8,}", _re.IGNORECASE), r"\1***"),                  # Google Kalender
    (_re.compile(r"(/public-calendars/)[\w-]+", _re.IGNORECASE), r"\1***"),              # Nextcloud
    (_re.compile(r"(icloud\.com/published/\d+/)[\w-]+", _re.IGNORECASE), r"\1***"),      # iCloud
    (_re.compile(r"(\b[a-z][a-z0-9+.-]*://[^/\s:@]+:)[^@\s/]+@", _re.IGNORECASE), r"\1***@"),
)
# Werte, die als Geheimnis konfiguriert sind (Passwort-Felder, Webhook-Adresse,
# UI-Passwort, Geräte-Token). Der Server trägt sie nach jedem Speichern ein.
_KNOWN_SECRETS: tuple[str, ...] = ()
_MIN_SECRET_LEN = 6


def set_known_secrets(values) -> None:
    """Diese Werte tauchen in keiner Log-Zeile und keiner Fehlermeldung mehr auf."""
    global _KNOWN_SECRETS
    cleaned = {str(v).strip() for v in values if v and len(str(v).strip()) >= _MIN_SECRET_LEN}
    # Längste zuerst: eine URL vor einem Token, der in ihr steckt
    _KNOWN_SECRETS = tuple(sorted(cleaned, key=len, reverse=True))


def redact_secrets(text: str) -> str:
    """Maskiert Geheimnisse in URLs und bekannte Geheimnis-Werte. Wird auf jede Log-Zeile angewendet."""
    if not text:
        return text
    for secret in _KNOWN_SECRETS:
        if secret in text:
            text = text.replace(secret, "***")
    text = _SECRET_PARAM_RE.sub(r"\1***", text)
    for pattern, repl in _SECRET_PATH_RES:
        text = pattern.sub(repl, text)
    return text


class _JsonLineFormatter(logging.Formatter):
    """Serialisiert jeden Log-Eintrag als einzelne JSON-Zeile (UTF-8)."""

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            if not record.exc_text:
                record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            msg = f"{msg}\n{record.exc_text}"
        msg = redact_secrets(msg)

        # Komponentenname kürzen
        name = record.name
        if name.startswith("inkwall."):
            name = name[len("inkwall."):]
        elif name == "inkwall":
            name = "server"

        payload = {
            "ts":    datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "name":  name,
            "msg":   msg,
        }
        # Ereignisse (log_event) bekommen eine Art, damit die Oberfläche sie
        # von Routine-Zeilen wie "Rendered …" unterscheiden kann
        event = getattr(record, "event", None)
        if event:
            payload["event"] = str(event)
        return json.dumps(payload, ensure_ascii=False)


class _RedactingConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        return redact_secrets(super().format(record))


_CONSOLE_FMT = _RedactingConsoleFormatter(
    "%(asctime)s  %(levelname)-8s  %(name)-22s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)


# ---------------------------------------------------------------------------
# Setup (einmalig beim ersten Import)
# ---------------------------------------------------------------------------

def _setup() -> logging.Logger:
    root = logging.getLogger("inkwall")
    if root.handlers:
        return root          # bereits initialisiert (z. B. durch Auto-Reload)

    root.setLevel(logging.DEBUG)

    # Datei-Handler: JSON-Lines, täglich rotiert, 7 Tage Aufbewahrung
    fh = logging.handlers.TimedRotatingFileHandler(
        LOG_FILE,
        when="midnight",
        backupCount=7,
        encoding="utf-8",
        utc=False,
    )
    fh.setFormatter(_JsonLineFormatter())
    # Datei auf INFO: DEBUG-Rauschen (Cover-Probing, Cache-Treffer) bläht die
    # JSONL auf und macht /api/logs langsam. Konsole bleibt DEBUG für die Entwicklung.
    fh.setLevel(logging.INFO)
    root.addHandler(fh)

    # Konsolen-Handler: menschenlesbar
    ch = logging.StreamHandler()
    ch.setFormatter(_CONSOLE_FMT)
    ch.setLevel(logging.DEBUG)
    root.addHandler(ch)

    # Flask/Werkzeug-Logs nicht doppelt ausgeben
    logging.getLogger("werkzeug").propagate = False

    return root


_root_logger = _setup()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

EVENT_KINDS = ("switch", "device", "settings", "source", "render_error", "system")


def log_event(kind: str, message: str, level: int = logging.INFO) -> None:
    """
    Ereignis für die Oberfläche: Inhalt gewechselt, Gerät gemeldet, Einstellungen
    gespeichert, Quelle nicht erreichbar. Landet als normale Logzeile mit
    zusätzlichem "event"-Feld in der JSONL, /api/logs?events=1 filtert darauf.
    """
    logging.getLogger("inkwall.events").log(level, message, extra={"event": kind})


def get_logger(name: str) -> logging.Logger:
    """
    Gibt einen benannten Child-Logger zurück.

    Empfohlene Verwendung:
        from app.logger import get_logger
        log = get_logger(__name__)

    Der Modul-Pfad ``app.foo.bar`` wird automatisch zu ``inkwall.foo.bar``
    umbenannt, damit alle Einträge unter dem ``inkwall``-Baum landen.
    """
    if name.startswith("app."):
        clean = "inkwall." + name[4:]
    elif not name.startswith("inkwall"):
        clean = f"inkwall.{name}"
    else:
        clean = name
    return logging.getLogger(clean)
