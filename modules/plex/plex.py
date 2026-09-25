"""
Plex-API-Zugriff, Session-Parsing und Artwork-Helpers.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET

import requests
from PIL import Image

from app.config import get_csv_setting, get_setting
from app.http_client import HTTP_SESSION, download_image_cached
from app.logger import get_logger

log = get_logger(__name__)


# ---------------------------------------------------------------------------
# Konstanten (Session-Felder, Artwork-Reihenfolgen, Aliasse)
# ---------------------------------------------------------------------------

DEFAULT_SESSION_PRIORITY              = ["movie", "episode", "track"]

PLAYBACK_LABELS = {
    "playing":   "Now Playing",
    "paused":    "Pausiert",
    "buffering": "Lädt",
    "stopped":   "Gestoppt",
}

PLAYER_STATE_PRIORITY = {
    "playing":   0,
    "buffering": 1,
    "paused":    2,
    "stopped":   3,
    "unknown":   4,
}

VIDEO_SESSION_FIELDS = {
    "mediaCategory":    "video",
    "type":             "video",
    "title":            "Unbekannt",
    "grandparentTitle": "",
    "parentTitle":      "",
    "parentIndex":      "",
    "index":            "",
    "year":             "",
    "thumb":            "",
    "art":              "",
    "parentThumb":      "",
    "grandparentThumb": "",
    "parentArt":        "",
    "grandparentArt":   "",
    "ratingKey":        "",
    "duration":         "",
    "viewOffset":       "",
}

TRACK_SESSION_FIELDS = {
    "mediaCategory":    "music",
    "type":             "track",
    "title":            "Unbekannt",
    "grandparentTitle": "",
    "parentTitle":      "",
    "parentIndex":      "",
    "index":            "",
    "year":             "",
    "thumb":            "",
    "art":              "",
    "ratingKey":        "",
    "duration":         "",
    "viewOffset":       "",
}

ARTWORK_FIELD_ORDERS = {
    "movie": {
        "movie_thumb": ["thumb", "art"],
        "movie_art":   ["art", "thumb"],
        "auto":        ["thumb", "art"],
    },
    "episode": {
        "series_thumb":  ["grandparentThumb", "parentThumb", "thumb", "grandparentArt", "parentArt", "art"],
        "series_art":    ["grandparentArt", "parentArt", "art", "grandparentThumb", "parentThumb", "thumb"],
        "season_thumb":  ["parentThumb", "grandparentThumb", "thumb", "parentArt", "grandparentArt", "art"],
        "season_art":    ["parentArt", "grandparentArt", "art", "parentThumb", "grandparentThumb", "thumb"],
        "episode_thumb": ["thumb", "grandparentThumb", "parentThumb", "art", "grandparentArt", "parentArt"],
        "episode_art":   ["art", "thumb", "grandparentArt", "parentArt", "grandparentThumb", "parentThumb"],
        "auto":          ["grandparentThumb", "parentThumb", "thumb", "grandparentArt", "parentArt", "art"],
    },
    "default": {
        "auto": ["thumb", "art"],
    },
}

SESSION_PRIORITY_ALIASES = {
    "film": "movie", "filme": "movie", "movie": "movie", "movies": "movie",
    "serie": "episode", "serien": "episode", "series": "episode", "episode": "episode", "episodes": "episode",
    "musik": "track", "music": "track", "track": "track", "tracks": "track",
}

EPISODE_ARTWORK_SOURCE_ALIASES = {
    "auto": "auto",
    "episode_thumb": "episode_thumb", "thumbnail": "episode_thumb", "thumb": "episode_thumb",
    "episode_art": "episode_art", "art": "episode_art",
    "series_thumb": "series_thumb", "series_cover": "series_thumb", "show_thumb": "series_thumb",
    "series_art": "series_art", "show_art": "series_art",
    "season_thumb": "season_thumb", "season_cover": "season_thumb",
    "season_art": "season_art",
}

MOVIE_ARTWORK_SOURCE_ALIASES = {
    "auto": "auto",
    "movie_thumb": "movie_thumb", "poster": "movie_thumb", "cover": "movie_thumb", "thumb": "movie_thumb",
    "movie_art": "movie_art", "background": "movie_art", "backdrop": "movie_art", "art": "movie_art",
}


# ---------------------------------------------------------------------------
# Settings-Parser
# ---------------------------------------------------------------------------

def parse_session_priority(raw_value: str) -> tuple[str, ...]:
    if not raw_value.strip():
        return tuple(DEFAULT_SESSION_PRIORITY)
    parsed = []
    for item in raw_value.split(","):
        normalized = SESSION_PRIORITY_ALIASES.get(item.strip().lower())
        if normalized and normalized not in parsed:
            parsed.append(normalized)
    for fallback in DEFAULT_SESSION_PRIORITY:
        if fallback not in parsed:
            parsed.append(fallback)
    return tuple(parsed)


def parse_episode_artwork_source(raw_value: str) -> str:
    return EPISODE_ARTWORK_SOURCE_ALIASES.get(raw_value.strip().lower(), "series_thumb")


def parse_movie_artwork_source(raw_value: str) -> str:
    return MOVIE_ARTWORK_SOURCE_ALIASES.get(raw_value.strip().lower(), "movie_thumb")


# ---------------------------------------------------------------------------
# XML-Session-Parsing
# ---------------------------------------------------------------------------

def parse_xml_session(element, field_defaults: dict[str, str]) -> dict:
    return {name: element.attrib.get(name, default) for name, default in field_defaults.items()}


def parse_video_session(video) -> dict:
    return parse_xml_session(video, VIDEO_SESSION_FIELDS)


def parse_track_session(track) -> dict:
    return parse_xml_session(track, TRACK_SESSION_FIELDS)


def extract_player_state(element) -> str:
    player_node = element.find("Player")
    if player_node is None:
        return "unknown"
    return (player_node.attrib.get("state") or "unknown").strip().lower()


def get_playback_label(player_state: str, media_label: str = "") -> str:
    base_label = PLAYBACK_LABELS.get(player_state, "Unbekannter Status")
    return f"{base_label} · {media_label}" if media_label else base_label


def extract_session_user(element) -> str:
    for tag in ("User", "Account"):
        node = element.find(tag)
        if node is not None:
            title = (node.attrib.get("title") or "").strip()
            if title:
                return title
    return ""


def is_allowed_user(username: str) -> bool:
    allowed_plex_users = frozenset(u.strip().lower() for u in get_csv_setting("ALLOWED_PLEX_USERS"))
    if not allowed_plex_users:
        return True
    return username.strip().lower() in allowed_plex_users


# ---------------------------------------------------------------------------
# Plex-API
# ---------------------------------------------------------------------------

def plex_get(path: str, params: dict | None = None) -> requests.Response:
    plex_base_url = get_setting("PLEX_BASE_URL", "").rstrip("/")
    plex_token = get_setting("PLEX_TOKEN", "")
    if not plex_base_url or not plex_token:
        raise RuntimeError("PLEX_BASE_URL oder PLEX_TOKEN fehlt")
    query_params = dict(params or {})
    query_params["X-Plex-Token"] = plex_token
    url = f"{plex_base_url}{path}"
    response = HTTP_SESSION.get(url, params=query_params, timeout=20)
    response.raise_for_status()
    return response


def create_session_from_element(element, parser) -> dict:
    session = parser(element)
    session["user"] = extract_session_user(element)
    session["playerState"] = extract_player_state(element)
    return session


def collect_sessions(root) -> list[dict]:
    sessions = []
    for video in root.findall(".//Video"):
        if is_allowed_user(extract_session_user(video)):
            sessions.append(create_session_from_element(video, parse_video_session))
    for track in root.findall(".//Track"):
        if is_allowed_user(extract_session_user(track)):
            sessions.append(create_session_from_element(track, parse_track_session))
    return sessions


def select_preferred_session(sessions: list[dict]) -> dict | None:
    if not sessions:
        return None
    session_priority = parse_session_priority(get_setting("SESSION_PRIORITY", "movie,episode,track"))

    # Nur Medientypen zulassen, die explizit in session_priority aktiviert sind.
    # Nicht enthaltene Typen sind deaktiviert – sie werden komplett ignoriert.
    # session_priority enthält "movie", "episode", "track" – das sind die
    # type-Werte aus dem Plex-XML, nicht mediaCategory ("video" / "music").
    allowed = set(session_priority)
    sessions = [s for s in sessions if s.get("type") in allowed]
    if not sessions:
        return None

    type_priority = {media_type: i for i, media_type in enumerate(session_priority)}

    def sort_key(session: dict) -> tuple[int, int]:
        return (
            PLAYER_STATE_PRIORITY.get(session.get("playerState", "unknown"), PLAYER_STATE_PRIORITY["unknown"]),
            type_priority.get(session.get("type", ""), len(session_priority)),
        )

    return min(sessions, key=sort_key)


_ERROR_LOG_INTERVAL_SECONDS = 600
_last_error_logged_at = 0.0


def get_active_session() -> dict | None:
    global _last_error_logged_at
    try:
        resp = plex_get("/status/sessions")
        root = ET.fromstring(resp.text)
        session = select_preferred_session(collect_sessions(root))
        if _last_error_logged_at:
            log.info("Plex ist wieder erreichbar")
            _last_error_logged_at = 0.0
        return session
    except Exception as exc:
        # Nicht erreichbarer Plex ist ein erwartbarer Zustand: eine Warning,
        # dann alle 10 min eine Erinnerung – kein Traceback pro Tick.
        import time as _time
        now = _time.time()
        if now - _last_error_logged_at >= _ERROR_LOG_INTERVAL_SECONDS:
            log.warning(f"Plex nicht erreichbar: {exc}")
            _last_error_logged_at = now
        return None


# ---------------------------------------------------------------------------
# Artwork-Helpers
# ---------------------------------------------------------------------------

def build_plex_image_url(image_path: str) -> str | None:
    if not image_path:
        return None
    plex_base_url = get_setting("PLEX_BASE_URL", "").rstrip("/")
    plex_token = get_setting("PLEX_TOKEN", "")
    return f"{plex_base_url}{image_path}?X-Plex-Token={plex_token}"


def get_artwork_source_config(media_type: str) -> str:
    if media_type == "movie":
        return parse_movie_artwork_source(get_setting("MOVIE_ARTWORK_SOURCE", "movie_thumb"))
    if media_type == "episode":
        return parse_episode_artwork_source(get_setting("EPISODE_ARTWORK_SOURCE", "series_thumb"))
    return "auto"


def get_artwork_candidates(session: dict) -> list[str]:
    media_type = session.get("type", "")
    source_config = get_artwork_source_config(media_type)
    source_orders = ARTWORK_FIELD_ORDERS.get(media_type, ARTWORK_FIELD_ORDERS["default"])
    field_order = source_orders.get(source_config, source_orders["auto"])
    candidates = []
    for field_name in field_order:
        value = session.get(field_name, "")
        if value and value not in candidates:
            candidates.append(value)
    return candidates


# download_image ist in app.http_client definiert und oben re-exportiert.


def download_session_artwork(session: dict | None) -> Image.Image | None:
    if not session:
        return None
    # Gecacht: bei laufender Wiedergabe wird jede Minute neu gerendert,
    # das Poster soll dabei nicht jedes Mal vom Plex-Server geladen werden.
    for image_path in get_artwork_candidates(session):
        image = download_image_cached(build_plex_image_url(image_path), ttl_seconds=3600, negative_ttl_seconds=60)
        if image is not None:
            return image
    return None
