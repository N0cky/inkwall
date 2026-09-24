"""
Gemeinsamer HTTP-Client für alle Inkwall-Module.

HTTP_SESSION  – requests.Session mit Retry-Logik (shared singleton)
download_image – lädt eine Bild-URL und gibt ein PIL-Image zurück

Dieses Modul ist ein reines Framework-Utility und hat keine
Abhängigkeiten zu Modul-spezifischem Code.
"""

from __future__ import annotations

import contextlib
import contextvars
import io
import threading
import time

import requests
from PIL import Image
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from app.logger import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# Globale HTTP-Session (wird von allen Modulen geteilt)
# ---------------------------------------------------------------------------

# Nach einem fehlgeschlagenen Fetch (Netzwerk, HTTP-Fehler, "nicht gefunden")
# warten Datenquellen mindestens so lange, bevor sie es erneut versuchen.
# Verhindert Fetch- und Re-Render-Schleifen im Poll-Takt bei Ausfällen.
FETCH_RETRY_BACKOFF_SECONDS = 300

# Verbindungsaufbau darf nie so lange dauern wie das Lesen der Antwort: ein
# abgeschalteter Server soll den Render nicht 3 × 30 s aufhalten.
CONNECT_TIMEOUT_SECONDS = 6


class _TimeoutAdapter(HTTPAdapter):
    """Ein einzelner timeout-Wert gilt fürs Lesen, der Verbindungsaufbau bekommt höchstens CONNECT_TIMEOUT_SECONDS."""

    def send(self, request, timeout=None, **kwargs):
        if isinstance(timeout, (int, float)) and not isinstance(timeout, bool):
            timeout = (min(CONNECT_TIMEOUT_SECONDS, timeout), timeout)
        return super().send(request, timeout=timeout, **kwargs)


HTTP_SESSION = requests.Session()
# Wiederholt nur, was schnell scheitert: Verbindungsfehler und 502/503/504.
# - read=0: ein Server, der nicht antwortet, kostet einmal das Timeout, nicht dreimal
# - kein Retry-After: urllib3 würde ungedeckelt so lange schlafen, wie der Server
#   verlangt – unter dem Render-Lock hinge dann der ganze Worker (und das Speichern)
_retry = Retry(total=2, connect=2, read=0, status=2, backoff_factor=0.5,
               status_forcelist=[502, 503, 504], respect_retry_after_header=False)
HTTP_SESSION.mount("http://",  _TimeoutAdapter(max_retries=_retry))
HTTP_SESSION.mount("https://", _TimeoutAdapter(max_retries=_retry))


# ---------------------------------------------------------------------------
# Nur aus dem Cache (Zusammenfassungen, /metrics)
# ---------------------------------------------------------------------------

_cache_only: contextvars.ContextVar[bool] = contextvars.ContextVar("inkwall_cache_only", default=False)


@contextlib.contextmanager
def cache_only():
    """
    Datenquellen liefern in diesem Block nur, was sie schon haben, und gehen
    nicht ins Netz. Für alles, was nur anzeigt (Zusammenfassungen auf der
    Anzeige-Seite, /metrics): sonst würde jeder Seitenaufruf oder Scrape
    abgelaufene Quellen neu laden – auch die von abgeschalteten Inhalten.
    """
    token = _cache_only.set(True)
    try:
        yield
    finally:
        _cache_only.reset(token)


def network_allowed() -> bool:
    """False innerhalb von cache_only(): Datenquellen antworten dann aus dem Cache."""
    return not _cache_only.get()


# ---------------------------------------------------------------------------
# Bild-Download
# ---------------------------------------------------------------------------

def download_image(url: str | None) -> Image.Image | None:
    """Lädt eine Bild-URL und gibt ein RGB-PIL-Image zurück. None bei Fehler."""
    if not url:
        return None
    try:
        response = HTTP_SESSION.get(url, timeout=20)
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except Exception as exc:
        # Netzwerkfehler sind erwartbar – Warning ohne Traceback. Secrets in
        # URLs werden vom Logger maskiert.
        log.warning(f"download_image: {exc}")
        return None


# ---------------------------------------------------------------------------
# Bild-Download mit Cache (für Artwork, das über mehrere Renders gleich bleibt)
# ---------------------------------------------------------------------------

IMAGE_CACHE_MAX_ENTRIES = 32
_image_cache: dict[str, dict] = {}
_image_cache_lock = threading.Lock()


def download_image_cached(
    url: str | None,
    ttl_seconds: int = 1800,
    negative_ttl_seconds: int = 300,
) -> Image.Image | None:
    """
    Wie download_image(), aber mit In-Memory-Cache pro URL.
    Erfolgreiche Downloads bleiben ttl_seconds gültig, Fehlschläge
    negative_ttl_seconds (damit eine tote URL nicht pro Render neu probiert wird).
    Gibt immer eine Kopie zurück, damit Aufrufer das Bild verändern dürfen.
    """
    if not url:
        return None
    now = time.time()

    with _image_cache_lock:
        entry = _image_cache.get(url)
        if entry is not None:
            ttl = ttl_seconds if entry["image"] is not None else negative_ttl_seconds
            if now - entry["fetched_at"] < ttl:
                img = entry["image"]
                return img.copy() if img is not None else None

    image = download_image(url)

    with _image_cache_lock:
        _image_cache[url] = {"fetched_at": now, "image": image.copy() if image is not None else None}
        while len(_image_cache) > IMAGE_CACHE_MAX_ENTRIES:
            oldest = min(_image_cache, key=lambda k: _image_cache[k]["fetched_at"])
            del _image_cache[oldest]

    return image


def clear_image_cache() -> None:
    with _image_cache_lock:
        _image_cache.clear()
