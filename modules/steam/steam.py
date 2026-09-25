"""
Steam-API-Zugriff für das Steam-Modul.

Unterstützt:
- SteamID64
- Vanity-Name
- vollständige Steam-Community-Profile-URLs
"""

from __future__ import annotations

import io
import re
import threading
import time

from PIL import Image
import requests

from app.config import get_setting
from app.http_client import HTTP_SESSION, download_image_cached
from app.logger import get_logger

log = get_logger(__name__)

STEAM_API_BASE = "https://api.steampowered.com"
STEAM_COMMUNITY_URL_RE = re.compile(
    r"^https?://steamcommunity\.com/(?P<kind>id|profiles)/(?P<value>[^/?#]+)/?$",
    re.IGNORECASE,
)
STEAM_ID_RE = re.compile(r"^\d{17}$")

PERSONA_STATE_LABELS = {
    0: "Offline",
    1: "Online",
    2: "Beschäftigt",
    3: "Abwesend",
    4: "Snooze",
    5: "Möchte handeln",
    6: "Möchte spielen",
}


def _build_store_asset_path(asset_url_format: str, filename: str) -> str | None:
    asset_format = (asset_url_format or "").strip()
    asset_name = (filename or "").strip()
    if not asset_format or not asset_name:
        return None

    asset_path = asset_format.replace("${FILENAME}", asset_name)
    asset_path = asset_path.lstrip("/")
    if asset_path.startswith("http://") or asset_path.startswith("https://"):
        return asset_path
    if not asset_path.startswith("store_item_assets/"):
        asset_path = f"store_item_assets/{asset_path}"
    return asset_path


def parse_steam_profile_input(raw_value: str) -> tuple[str, str] | None:
    """
    Akzeptiert SteamID64, Vanity-Namen oder vollständige Profil-URLs.
    Rückgabe:
      ("steamid", "7656...")
      ("vanity", "gabelogannewell")
    """
    value = (raw_value or "").strip()
    if not value:
        return None

    match = STEAM_COMMUNITY_URL_RE.match(value)
    if match:
        kind = match.group("kind").lower()
        parsed_value = match.group("value").strip()
        if not parsed_value:
            return None
        return ("steamid" if kind == "profiles" else "vanity", parsed_value)

    if STEAM_ID_RE.match(value):
        return "steamid", value

    return "vanity", value


def _steam_api_get(path: str, params: dict[str, str]) -> dict:
    response = HTTP_SESSION.get(f"{STEAM_API_BASE}{path}", params=params, timeout=20)
    response.raise_for_status()
    return response.json()


def _download_steam_candidate(url: str | None) -> Image.Image | None:
    """
    Lädt ein Steam-Bild bewusst leiser als download_image():
    404 bei Cover-Probing ist erwartbar und soll nicht als voller Error mit
    Traceback im Log landen.
    """
    if not url:
        return None

    try:
        response = HTTP_SESSION.get(url, timeout=20)
        if response.status_code == 503:
            log.debug(f"Steam artwork candidate temporarily unavailable (503): {url}")
            return None
        if response.status_code == 404:
            log.debug(f"Steam artwork candidate not found: {url}")
            return None
        response.raise_for_status()
        return Image.open(io.BytesIO(response.content)).convert("RGB")
    except requests.exceptions.RetryError as exc:
        log.debug(f"Steam artwork candidate retry-exhausted: {url} ({exc})")
        return None
    except requests.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else "?"
        log.warning(f"Steam artwork HTTP error {status_code}: {url}")
        return None
    except Exception as exc:
        log.error(f"_download_steam_candidate: {exc}", exc_info=True)
        return None


_PROFILE_ID_TTL_SECONDS = 24 * 3600
_profile_id_cache: dict[tuple[str, str], tuple[float, str]] = {}
_profile_id_lock = threading.Lock()


def resolve_steam_profile_to_id(profile_input: str, api_key: str) -> str | None:
    """
    Löst Vanity-Namen/Profil-URLs zur SteamID64 auf. Erfolgreiche Auflösungen
    werden 24 h gecacht. Fehlschläge (API down, Tippfehler) werden NICHT
    gecacht, damit ein transienter Fehler nicht bis zum Neustart klebt.
    """
    parsed = parse_steam_profile_input(profile_input)
    if parsed is None:
        return None

    kind, value = parsed
    if kind == "steamid":
        return value

    cache_key = (profile_input, api_key)
    now = time.time()
    with _profile_id_lock:
        cached = _profile_id_cache.get(cache_key)
        if cached and now - cached[0] < _PROFILE_ID_TTL_SECONDS:
            return cached[1]

    try:
        payload = _steam_api_get(
            "/ISteamUser/ResolveVanityURL/v1/",
            {"key": api_key, "vanityurl": value},
        )
    except Exception as exc:
        log.warning(f"resolve_steam_profile_to_id: {exc}")
        return None

    response = payload.get("response", {})
    if int(response.get("success", 0)) != 1:
        return None
    steam_id = str(response.get("steamid", "")).strip()
    if not steam_id:
        return None
    with _profile_id_lock:
        _profile_id_cache[cache_key] = (now, steam_id)
    return steam_id


def get_player_summary() -> dict | None:
    api_key = get_setting("STEAM_API_KEY", "").strip()
    profile_input = get_setting("STEAM_PROFILE", "").strip()
    if not api_key or not profile_input:
        return None

    steam_id = resolve_steam_profile_to_id(profile_input, api_key)
    if not steam_id:
        return None

    try:
        payload = _steam_api_get(
            "/ISteamUser/GetPlayerSummaries/v2/",
            {"key": api_key, "steamids": steam_id},
        )
    except Exception as exc:
        log.error(f"get_player_summary: {exc}", exc_info=True)
        return None

    players = payload.get("response", {}).get("players", [])
    if not players:
        return None
    player = dict(players[0])
    player["steamid"] = steam_id
    return player


def extract_active_game(summary: dict | None) -> dict | None:
    if not summary:
        return None

    game_name = str(summary.get("gameextrainfo", "")).strip()
    game_id = str(summary.get("gameid", "")).strip()
    if not game_name or not game_id:
        return None

    persona_name = str(summary.get("personaname", "")).strip() or "Steam User"
    personastate = int(summary.get("personastate", 0) or 0)

    return {
        "steamid": str(summary.get("steamid", "")).strip(),
        "profileurl": str(summary.get("profileurl", "")).strip(),
        "personaname": persona_name,
        "personastate": personastate,
        "personastate_label": PERSONA_STATE_LABELS.get(personastate, "Unbekannt"),
        "avatar": str(summary.get("avatar", "")).strip(),
        "avatarmedium": str(summary.get("avatarmedium", "")).strip(),
        "avatarfull": str(summary.get("avatarfull", "")).strip(),
        "gameid": game_id,
        "gamename": game_name,
        "lastlogoff": str(summary.get("lastlogoff", "")).strip(),
    }


def get_active_game() -> dict | None:
    return extract_active_game(get_player_summary())


def get_game_artwork_urls(game_id: str) -> list[str]:
    normalized = (game_id or "").strip()
    if not normalized:
        return []

    return [
        f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{normalized}/library_600x900_2x.jpg",
        f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{normalized}/library_600x900.jpg",
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{normalized}/library_600x900_2x.jpg",
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{normalized}/library_600x900.jpg",
        f"https://shared.cloudflare.steamstatic.com/store_item_assets/steam/apps/{normalized}/header.jpg",
        f"https://shared.fastly.steamstatic.com/store_item_assets/steam/apps/{normalized}/header.jpg",
        f"https://cdn.cloudflare.steamstatic.com/steam/apps/{normalized}/header.jpg",
    ]


def get_store_item_asset_urls(game_id: str, country_code: str = "DE") -> list[str]:
    normalized = (game_id or "").strip()
    if not normalized.isdigit():
        return []

    input_json = {
        "ids": [{"appid": int(normalized)}],
        "context": {"country_code": country_code},
        "data_request": {"include_assets": True},
    }

    try:
        payload = _steam_api_get(
            "/IStoreBrowseService/GetItems/v1/",
            {"input_json": __import__("json").dumps(input_json, separators=(",", ":"))},
        )
    except Exception as exc:
        log.error(f"get_store_item_asset_urls: {exc}", exc_info=True)
        return []

    store_items = payload.get("response", {}).get("store_items", [])
    if not store_items:
        return []

    assets = store_items[0].get("assets", {}) or {}
    asset_url_format = str(assets.get("asset_url_format", "")).strip()
    candidate_paths = [
        _build_store_asset_path(asset_url_format, str(assets.get("library_capsule_2x", "")).strip()),
        _build_store_asset_path(asset_url_format, str(assets.get("library_capsule", "")).strip()),
        _build_store_asset_path(asset_url_format, str(assets.get("header", "")).strip()),
        _build_store_asset_path(asset_url_format, str(assets.get("main_capsule", "")).strip()),
    ]

    deduped: list[str] = []
    hosts = (
        "https://shared.fastly.steamstatic.com",
        "https://shared.cloudflare.steamstatic.com",
        "https://shared.steamstatic.com",
        "https://shared.akamai.steamstatic.com",
    )
    for candidate_path in candidate_paths:
        if not candidate_path:
            continue
        if candidate_path.startswith("http://") or candidate_path.startswith("https://"):
            if candidate_path not in deduped:
                deduped.append(candidate_path)
            continue
        for host in hosts:
            candidate = f"{host}/{candidate_path}"
            if candidate not in deduped:
                deduped.append(candidate)
    return deduped


_ARTWORK_TTL_SECONDS = 6 * 3600
_ARTWORK_NEGATIVE_TTL_SECONDS = 600
_artwork_cache: dict[str, tuple[float, Image.Image | None]] = {}
_artwork_lock = threading.Lock()


def download_steam_artwork(game_id: str, fallback_avatar_url: str = "") -> Image.Image | None:
    """
    Cover für ein Spiel. Das Probing über bis zu ~20 Kandidaten-URLs ist
    teuer, deshalb wird das Ergebnis pro game_id gecacht (auch "nichts
    gefunden", kürzer). Bei laufendem Spiel wird jede Minute neu gerendert.
    """
    now = time.time()
    if game_id:
        with _artwork_lock:
            cached = _artwork_cache.get(game_id)
            if cached:
                fetched_at, image = cached
                ttl = _ARTWORK_TTL_SECONDS if image is not None else _ARTWORK_NEGATIVE_TTL_SECONDS
                if now - fetched_at < ttl:
                    return image.copy() if image is not None else download_image_cached(fallback_avatar_url)

    found: Image.Image | None = None
    for url in get_game_artwork_urls(game_id):
        found = _download_steam_candidate(url)
        if found is not None:
            break
    if found is None:
        for url in get_store_item_asset_urls(game_id):
            found = _download_steam_candidate(url)
            if found is not None:
                break

    if game_id:
        with _artwork_lock:
            _artwork_cache[game_id] = (now, found.copy() if found is not None else None)
            while len(_artwork_cache) > 16:
                oldest = min(_artwork_cache, key=lambda k: _artwork_cache[k][0])
                del _artwork_cache[oldest]

    if found is not None:
        return found
    return download_image_cached(fallback_avatar_url)
