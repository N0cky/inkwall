"""
Automatische Sicherungen von settings.env.

Vor jeder Änderung der Datei legt write_env_settings den bisherigen Stand in
<Konfigurationsordner>/backups ab (im Container /config/backups, also im
Volume). Die letzten KEEP Stände bleiben; ein Stand, der dem neuesten
Backup gleicht, wird nicht doppelt abgelegt. Die System-Seite listet sie mit
den Werten, die sich seitdem geändert haben (nur die Namen, keine Werte),
und holt einen Stand zurück – vorher wird der jetzige gesichert, damit auch
das Zurückholen rückgängig zu machen ist.

Die Backups enthalten dieselben Geheimnisse wie settings.env und bekommen
deren Dateirechte.
"""

from __future__ import annotations

import contextlib
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path

from app import config
from app.logger import get_logger

log = get_logger(__name__)

KEEP = 30
_NAME_RE = re.compile(r"^settings-(\d{8}-\d{6})(?:-(\d+))?\.env$")


def backup_dir() -> Path:
    return config.ENV_FILE_PATH.parent / "backups"


def list_backups() -> list[Path]:
    """Alle Backups, neuestes zuerst (der Name trägt Datum und Uhrzeit)."""
    folder = backup_dir()
    if not folder.is_dir():
        return []
    files = [p for p in folder.iterdir() if p.is_file() and _NAME_RE.match(p.name)]

    def order(p: Path) -> tuple:
        m = _NAME_RE.match(p.name)
        return m.group(1), int(m.group(2) or 0)

    return sorted(files, key=order, reverse=True)


def created_at(path: Path) -> datetime | None:
    m = _NAME_RE.match(path.name)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y%m%d-%H%M%S").replace(tzinfo=config.local_tz())
    except ValueError:
        return None


def backup_text(text: str, now: datetime | None = None) -> Path | None:
    """
    Einen Stand (Inhalt von settings.env) ablegen und alte Stände über KEEP
    hinaus löschen. None, wenn der Stand dem neuesten Backup gleicht.
    """
    folder = backup_dir()
    folder.mkdir(parents=True, exist_ok=True)
    backups = list_backups()
    if backups:
        with contextlib.suppress(OSError):
            if backups[0].read_text(encoding="utf-8") == text:
                return None
    stamp = (now or config.now_local()).strftime("%Y%m%d-%H%M%S")
    path = folder / f"settings-{stamp}.env"
    n = 1
    while path.exists():
        path = folder / f"settings-{stamp}-{n}.env"
        n += 1
    fd, tmp_name = tempfile.mkstemp(prefix=".backup.", suffix=".tmp", dir=folder)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        config._copy_mode(config.ENV_FILE_PATH, tmp_name)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise
    prune()
    return path


def prune(keep: int = KEEP) -> None:
    for old in list_backups()[keep:]:
        with contextlib.suppress(OSError):
            old.unlink()


def find(name: str) -> Path | None:
    """Backup nach Dateiname – nur Namen im erwarteten Format, nur im Backup-Ordner."""
    if not _NAME_RE.match(name or ""):
        return None
    path = backup_dir() / name
    return path if path.is_file() else None


def read_values(path: Path) -> dict[str, str]:
    from dotenv import dotenv_values
    return {key: config.as_env_value(value)
            for key, value in dotenv_values(path, interpolate=False).items() if value is not None}


def changed_keys(backup_values: dict[str, str], current: dict[str, str]) -> list[str]:
    """Schlüssel, deren Wert sich zwischen Backup und jetzt unterscheidet (neue und entfernte eingeschlossen)."""
    keys = set(backup_values) | set(current)
    return sorted(k for k in keys if (backup_values.get(k) or "") != (current.get(k) or ""))


def restore(path: Path) -> None:
    """
    Stand zurückholen: den jetzigen sichern, dann die Datei als Ganzes
    ersetzen (auch Schlüssel, die es damals nicht gab, verschwinden wieder).
    Die Laufzeit-Konfiguration übernimmt der Aufrufer.
    """
    text = path.read_text(encoding="utf-8")
    with config.settings_lock:
        target = config.ENV_FILE_PATH
        if target.exists():
            backup_text(target.read_text(encoding="utf-8"))
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            config._copy_mode(target, tmp_name)
            os.replace(tmp_name, target)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise
