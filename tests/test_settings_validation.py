from __future__ import annotations

import os
import tempfile
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from pathlib import Path

import app.server as server_module
from app.schedule import ALL_DAYS, Window
from app.config import CONFIG_DIR, get_settings_values, resolve_env_file_path


class SettingsValidationFlowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server_module.app.test_client()

    @staticmethod
    def _all_error_text(response) -> str:
        errors = (response.get_json() or {}).get("errors") or {}
        return " | ".join(list(errors.get("fields", {}).values()) + list(errors.get("general", [])))

    def test_invalid_module_settings_block_save(self) -> None:
        with (
            patch("app.server.write_env_settings") as write_env,
            patch("app.server.apply_runtime_config") as apply_cfg,
            patch("app.server.request_render") as render_image,
        ):
            response = self.client.put("/api/settings/dwd_weather", json={"values": {"DWD_WEATHER_STATION_ID": "abc"}})

        self.assertEqual(response.status_code, 400)
        self.assertIn("DWD_WEATHER_STATION_ID", response.get_json()["errors"]["fields"])
        self.assertIn("numerische Stations-IDs", self._all_error_text(response))
        write_env.assert_not_called()
        apply_cfg.assert_not_called()
        render_image.assert_not_called()

    def test_gallery_enabled_without_paths_blocks_save(self) -> None:
        with (
            patch("app.server.write_env_settings") as write_env,
            patch("app.server.apply_runtime_config") as apply_cfg,
            patch("app.server.request_render") as render_image,
            # app.server hat den Namen direkt importiert – dort patchen, nicht in app.config
            patch("app.server.get_settings_values", return_value={**get_settings_values(), "GALLERY_PATHS": ""}),
        ):
            response = self.client.put("/api/display", json={"content": [{"id": "gallery", "enabled": True}]})

        self.assertEqual(response.status_code, 400)
        self.assertIn("Bitte mindestens einen lokalen Ordner angeben", self._all_error_text(response))
        self.assertIn("GALLERY_PATHS", response.get_json()["errors"]["fields"], "Fehler hängt am Feld")
        write_env.assert_not_called()
        apply_cfg.assert_not_called()
        render_image.assert_not_called()

    def test_valid_module_save_writes_and_renders(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with (
                patch("app.server.write_env_settings") as write_env,
                patch("app.server.apply_runtime_config") as apply_cfg,
                patch("app.server.request_render") as render_image,
            ):
                response = self.client.put("/api/settings/gallery", json={"values": {"GALLERY_PATHS": [{"path": tmp}]}})

            self.assertEqual(response.status_code, 200, response.get_json())
            write_env.assert_called_once()
            self.assertEqual(write_env.call_args[0][0]["GALLERY_PATHS"], tmp)
            apply_cfg.assert_called_once()
            render_image.assert_called_once()

    def test_steam_enabled_without_profile_or_key_blocks_save(self) -> None:
        with (
            patch("app.server.write_env_settings") as write_env,
            patch("app.server.apply_runtime_config") as apply_cfg,
            patch("app.server.request_render") as render_image,
            patch("app.server.get_settings_values", return_value={**get_settings_values(), "STEAM_PROFILE": "", "STEAM_API_KEY": ""}),
        ):
            response = self.client.put("/api/display", json={"live": [{"id": "steam", "enabled": True}]})

        self.assertEqual(response.status_code, 400)
        text = self._all_error_text(response)
        self.assertIn("SteamID64, Vanity-Name oder Profil-URL", text)
        self.assertIn("API-Key", text)
        write_env.assert_not_called()
        apply_cfg.assert_not_called()
        render_image.assert_not_called()

    def test_night_mode_fixed_module_must_be_active_idle_module(self) -> None:
        with (
            patch("app.server.write_env_settings") as write_env,
            patch("app.server.apply_runtime_config") as apply_cfg,
            patch("app.server.request_render") as render_image,
        ):
            response = self.client.put("/api/display", json={
                "content": [{"id": "dwd_weather", "enabled": True}, {"id": "tagesschau", "enabled": True}, {"id": "gallery", "enabled": False}],
                "night": {"enabled": True, "behavior": "fixed", "fixed_module": "gallery"},
            })

        self.assertEqual(response.status_code, 400)
        self.assertIn("Das Modul muss auch in den aktiven Idle-Modulen enthalten sein", self._all_error_text(response))
        write_env.assert_not_called()
        apply_cfg.assert_not_called()
        render_image.assert_not_called()

    def test_meta_json_uses_gallery_custom_next_wake(self) -> None:
        with patch.dict(
            server_module._esp32_state,
            {
                "state": "gallery:12:C:\\images\\frame.jpg:123",
                "format": "png",
                "media_type": "gallery",
                "hash": "abc123",
                "rendered_at": "2026-04-20T09:00:00Z",
            },
            clear=True,
        ), patch(
            "app.server.get_settings_values",
            return_value={
                "GALLERY_INTERVAL_MODE": "custom",
                "GALLERY_INTERVAL_SECONDS": "420",
            },
        ), patch(
            "app.server._get_schedule_state",
            return_value={"active": False, "seconds_until_end": 0, "label": ""},
        ):
            response = self.client.get("/meta.json")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["next_wake_sec"], 420)
        self.assertEqual(payload["next_wake_reason"], "Gallery – eigenes Bildwechsel-Intervall")

    def test_background_poll_uses_module_specific_interval(self) -> None:
        gallery = SimpleNamespace(
            is_enabled=lambda env: True,
            get_background_poll_seconds=lambda env: 30,
        )
        with (
            patch("app.server.get_settings_values", return_value={}),
            patch("app.server.get_cfg", return_value=SimpleNamespace(refresh_interval=60)),
            patch("app.server._registry.get_modules", return_value=[gallery]),
            patch("app.server._get_schedule_state", return_value={"active": False, "seconds_until_end": 0, "label": ""}),
        ):
            self.assertEqual(server_module._get_background_poll_seconds(), 30)

    def test_night_mode_clamps_next_wake_to_mode_end(self) -> None:
        cfg = SimpleNamespace(
            idle_module_rotation_seconds=180,
            refresh_interval=60,
        )
        night = Window("Nachts", ALL_DAYS, 23 * 60, 7 * 60, "", 900, ())
        with (
            patch("app.server.get_cfg", return_value=cfg),
            patch("app.server._get_schedule_state", return_value={"active": True, "window": night, "seconds_until_end": 120,
                                                                   "seconds_until_change": 120, "label": night.label, "next": None}),
        ):
            seconds, reason = server_module._suggest_next_wake("__no_content__", "idle")

        self.assertEqual(seconds, 120)
        self.assertIn("07:00", reason)

    def test_plex_next_wake_ignores_night_mode(self) -> None:
        cfg = SimpleNamespace(
            refresh_interval=60,
            idle_module_rotation_seconds=180,
        )
        with (
            patch("app.server.get_cfg", return_value=cfg),
            patch("app.server._get_schedule_state", return_value={"active": True, "window": Window("Nachts", ALL_DAYS, 1380, 420, "", 900, ()),
                                                                   "seconds_until_end": 120, "seconds_until_change": 120, "label": "23:00–07:00", "next": None}),
        ):
            seconds, reason = server_module._suggest_next_wake("plex:123:playing:slot", "plex")

        self.assertEqual(seconds, 60)
        self.assertIn("Plex playing", reason)

    def test_night_mode_can_pin_a_single_idle_module(self) -> None:
        dwd = SimpleNamespace(MODULE_ID="dwd_weather", is_enabled=lambda env: True)
        tagesschau = SimpleNamespace(MODULE_ID="tagesschau", is_enabled=lambda env: True)
        cfg = SimpleNamespace(idle_module_rotation_seconds=180)
        night = Window("Nachts", ALL_DAYS, 23 * 60, 7 * 60, "", 900, (("tagesschau", 0),))
        with (
            patch("app.server.get_cfg", return_value=cfg),
            patch("app.server._get_schedule_state", return_value={"active": True, "window": night, "seconds_until_end": 3600,
                                                                   "seconds_until_change": 3600, "label": night.label, "next": None}),
            patch("app.server._registry.get_idle_modules", return_value=[dwd, tagesschau]),
        ):
            modules, rotation = server_module._get_effective_idle_modules({})

        self.assertEqual([m.MODULE_ID for m in modules], ["tagesschau"])
        self.assertEqual(rotation, 900)

    def test_ensure_runtime_started_is_idempotent(self) -> None:
        fake_thread = SimpleNamespace(start=lambda: None)
        with (
            patch("app.server._runtime_started", False),
            patch("app.server._worker_thread", None),
            patch("app.server.log_startup_config") as log_startup,
            patch("app.server.render_image") as render_image,
            patch("pathlib.Path.exists", return_value=False),
            patch("app.server._registry.get_modules", return_value=[]),
            patch("app.server.threading.Thread", return_value=fake_thread) as thread_ctor,
        ):
            server_module.ensure_runtime_started()
            server_module.ensure_runtime_started()

        log_startup.assert_called_once()
        # Kein Render beim Start (Gunicorns Start-Timeout) – das erledigt der Worker sofort
        render_image.assert_not_called()
        thread_ctor.assert_called_once()

    def test_default_config_file_path_points_to_config_directory(self) -> None:
        # tests/__init__.py setzt INKWALL_CONFIG_FILE für die Isolation – hier
        # wird explizit der Zustand "nicht gesetzt" geprüft.
        with patch.dict("os.environ", {}, clear=False):
            os.environ.pop("INKWALL_CONFIG_FILE", None)
            self.assertEqual(resolve_env_file_path(), CONFIG_DIR / "settings.env")

    def test_custom_config_file_path_is_respected(self) -> None:
        custom = Path("/tmp/custom-settings.env")
        with patch.dict("os.environ", {"INKWALL_CONFIG_FILE": str(custom)}, clear=False):
            self.assertEqual(resolve_env_file_path(), custom)


if __name__ == "__main__":
    unittest.main()
