"""
Tests für Block 3: lokale Zeit, deutsche Wochentage, Artwork-Caches,
Plex-Verhalten ohne Konfiguration, Log-API.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, date
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.config as config
import app.http_client as http_client
import app.server as server
import modules.steam.steam as steam
from modules.dwd_weather.renderer import format_day_label


class LocalTimeTest(unittest.TestCase):
    def test_now_local_is_tz_aware_in_configured_zone(self) -> None:
        now = config.now_local()
        self.assertIsNotNone(now.tzinfo)
        self.assertEqual(str(now.tzinfo), config.get_cfg().timezone or "Europe/Berlin")

    def test_weekday_labels_are_german(self) -> None:
        self.assertEqual(config.format_weekday_short(date(2026, 9, 2)), "Mi")
        self.assertEqual(config.format_weekday_short(datetime(2026, 9, 6)), "So")
        self.assertEqual(format_day_label("2026-09-03"), "Do 03.09.")
        self.assertEqual(format_day_label("kaputt"), "kaputt")


class DownloadImageCachedTest(unittest.TestCase):
    def setUp(self) -> None:
        http_client.clear_image_cache()

    def tearDown(self) -> None:
        http_client.clear_image_cache()

    def test_second_call_hits_cache(self) -> None:
        img = Image.new("RGB", (2, 2), (1, 2, 3))
        with patch.object(http_client, "download_image", return_value=img) as dl:
            a = http_client.download_image_cached("http://x/a.png")
            b = http_client.download_image_cached("http://x/a.png")
        self.assertEqual(dl.call_count, 1)
        self.assertIsNot(a, b, "Aufrufer bekommen Kopien, nicht das Cache-Objekt")
        self.assertEqual(a.size, (2, 2))

    def test_failure_is_cached_only_for_negative_ttl(self) -> None:
        with patch.object(http_client, "download_image", return_value=None) as dl:
            with patch.object(http_client.time, "time", side_effect=[1000.0, 1010.0, 2000.0]):
                self.assertIsNone(http_client.download_image_cached("http://x/b.png", negative_ttl_seconds=60))
                self.assertIsNone(http_client.download_image_cached("http://x/b.png", negative_ttl_seconds=60))
                self.assertEqual(dl.call_count, 1, "innerhalb negative_ttl kein neuer Versuch")
                http_client.download_image_cached("http://x/b.png", negative_ttl_seconds=60)
        self.assertEqual(dl.call_count, 2, "nach negative_ttl wird erneut geladen")

    def test_cache_is_bounded(self) -> None:
        img = Image.new("RGB", (1, 1))
        with patch.object(http_client, "download_image", return_value=img):
            for i in range(http_client.IMAGE_CACHE_MAX_ENTRIES + 10):
                http_client.download_image_cached(f"http://x/{i}.png")
        self.assertLessEqual(len(http_client._image_cache), http_client.IMAGE_CACHE_MAX_ENTRIES)


class SteamCachesTest(unittest.TestCase):
    def setUp(self) -> None:
        steam._profile_id_cache.clear()
        steam._artwork_cache.clear()

    def test_profile_resolution_does_not_cache_failures(self) -> None:
        with patch.object(steam, "_steam_api_get", side_effect=RuntimeError("down")) as api:
            self.assertIsNone(steam.resolve_steam_profile_to_id("https://steamcommunity.com/id/foo", "K"))
            self.assertIsNone(steam.resolve_steam_profile_to_id("https://steamcommunity.com/id/foo", "K"))
        self.assertEqual(api.call_count, 2, "Fehlschlag darf nicht gecacht werden")

        with patch.object(steam, "_steam_api_get", return_value={"response": {"success": 1, "steamid": "76561198000000000"}}) as api:
            self.assertEqual(steam.resolve_steam_profile_to_id("https://steamcommunity.com/id/foo", "K"), "76561198000000000")
            self.assertEqual(steam.resolve_steam_profile_to_id("https://steamcommunity.com/id/foo", "K"), "76561198000000000")
        self.assertEqual(api.call_count, 1, "Erfolg wird gecacht")

    def test_artwork_probing_runs_once_per_game(self) -> None:
        img = Image.new("RGB", (3, 3))
        with (
            patch.object(steam, "get_game_artwork_urls", return_value=["u1", "u2"]),
            patch.object(steam, "get_store_item_asset_urls", return_value=[]),
            patch.object(steam, "_download_steam_candidate", side_effect=[None, img]) as probe,
        ):
            a = steam.download_steam_artwork("123")
            b = steam.download_steam_artwork("123")
        self.assertEqual(probe.call_count, 2, "zweiter Render darf nicht erneut proben")
        self.assertIsNotNone(a)
        self.assertIsNotNone(b)


class PlexWithoutConfigTest(unittest.TestCase):
    def test_plex_is_not_enabled_without_url_and_token(self) -> None:
        from modules.plex import module as plex_module
        self.assertFalse(plex_module.is_enabled({"PLEX_MODULE_ENABLED": "true"}))
        self.assertFalse(plex_module.is_enabled({"PLEX_MODULE_ENABLED": "true", "PLEX_BASE_URL": "http://p"}))
        self.assertTrue(plex_module.is_enabled({"PLEX_MODULE_ENABLED": "true", "PLEX_BASE_URL": "http://p", "PLEX_TOKEN": "t"}))
        self.assertFalse(plex_module.is_enabled({"PLEX_MODULE_ENABLED": "false", "PLEX_BASE_URL": "http://p", "PLEX_TOKEN": "t"}))

    def test_unreachable_plex_logs_warning_once_not_error_per_tick(self) -> None:
        import modules.plex.plex as plex
        plex._last_error_logged_at = 0.0
        with (
            patch.object(plex, "plex_get", side_effect=RuntimeError("connection refused")),
            patch.object(plex, "log") as log,
        ):
            for _ in range(5):
                self.assertIsNone(plex.get_active_session())
        self.assertEqual(log.warning.call_count, 1)
        log.error.assert_not_called()


class ApiLogsTest(unittest.TestCase):
    def test_returns_newest_entries_in_chronological_order(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            lines = [json.dumps({"ts": f"2026-09-03T00:00:{i:02d}", "level": "INFO", "name": "server", "msg": f"m{i}"}) for i in range(50)]
            (logs_dir / "app.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
            with patch.object(server, "LOGS_DIR", logs_dir):
                client = server.app.test_client()
                data = client.get("/api/logs?limit=5").get_json()
        self.assertEqual([e["msg"] for e in data], ["m45", "m46", "m47", "m48", "m49"])

    def test_search_and_level_filters(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            entries = [
                {"ts": "t", "level": "DEBUG", "name": "a", "msg": "debug noise"},
                {"ts": "t", "level": "INFO", "name": "a", "msg": "Rendered [dwd]"},
                {"ts": "t", "level": "ERROR", "name": "b", "msg": "render kaputt"},
            ]
            (logs_dir / "app.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            with patch.object(server, "LOGS_DIR", logs_dir):
                client = server.app.test_client()
                only_errors = client.get("/api/logs?level=ERROR").get_json()
                search = client.get("/api/logs?search=rendered").get_json()
        self.assertEqual([e["msg"] for e in only_errors], ["render kaputt"])
        self.assertEqual([e["msg"] for e in search], ["Rendered [dwd]"])


if __name__ == "__main__":
    unittest.main()
