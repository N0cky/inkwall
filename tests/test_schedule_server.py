"""
Tests für den Zeitplan im Server und in der Anzeige-API: Fensterinhalte und
Takt im Render-Pfad (Layout aus dem Fenster), Wake-Intervall an Fenstergrenzen,
Anzeige-JSON mit Zeitplan (auch aus dem alten Nachtmodus), Speichern und
Prüfen der Fenster.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.config as config
import app.server as server
from app.module_base import InkwallModule


class _Tile(InkwallModule):
    MODULE_PRIORITY = 100

    def __init__(self, module_id: str, name: str, configured: bool = True, tile: bool = True):
        self.MODULE_ID, self.MODULE_NAME, self._configured, self._tile = module_id, name, configured, tile
        self.rendered: list[str] = []

    def is_enabled(self, env):
        idle = {x.strip() for x in env.get("IDLE_MODULES", "").split(",") if x.strip()}
        return self._configured and self.MODULE_ID in idle

    def fetch_content(self, env):
        return {"v": 1}

    def render(self, env, content):
        self.rendered.append("full")
        return Image.new("RGB", (8, 8), (255, 0, 0))

    def render_tile(self, env, content, width, height):
        if not self._tile:
            return None
        self.rendered.append("tile")
        return Image.new("RGB", (width, height), (0, 0, 255))

    def describe_status(self, env):
        return {"state": "ready", "reason": ""} if self._configured else {"state": "missing", "reason": "fehlt"}

    def get_state_key(self, content):
        return "c"


class _NoTile(_Tile):
    """Inhalt ohne Kachel: render_tile ist die Basis-Implementierung (supports_tile() → False)."""
    render_tile = InkwallModule.render_tile


def _registry_patches(modules):
    # server und display_api teilen sich dasselbe Registry-Modul – einmal patchen reicht
    return [
        patch("app.module_registry.get_idle_modules", return_value=modules),
        patch("app.module_registry.get_priority_modules", return_value=[]),
        patch("app.module_registry.get_modules", return_value=modules),
        patch("app.module_registry.get_module_by_id", side_effect=lambda mid: next((m for m in modules if m.MODULE_ID == mid), None)),
    ]


THU_0700 = datetime(2026, 9, 10, 7, 0)      # Donnerstag


class ProgrammeWithWindowTest(unittest.TestCase):
    def setUp(self) -> None:
        self.weather = _Tile("dwd_weather", "Wetter")
        self.news = _Tile("tagesschau", "Tagesschau")
        self.gallery = _Tile("gallery", "Gallery", configured=False)
        self._patches = _registry_patches([self.weather, self.news, self.gallery])
        for p in self._patches:
            p.start()
        settings = dict(config.read_env_settings())
        settings.update({
            "IDLE_MODULES": "dwd_weather,tagesschau", "IDLE_LAYOUT": "rotation", "IDLE_MODULE_ROTATION_SECONDS": "120",
            "DASHBOARD_TILES": "", "NIGHT_MODE_ENABLED": "false",
            "SCHEDULE_WINDOWS": "Morgens|Mo-Fr|06:00-09:00|dashboard|600|tagesschau:70,gallery,dwd_weather; Nachts|*|23:00-07:00||900|",
        })
        config.apply_runtime_config(settings)
        self.env = config.get_settings_values()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        config.apply_runtime_config()

    def test_window_content_layout_and_interval_apply(self) -> None:
        with patch("app.server._get_local_now", return_value=THU_0700):
            modules, rotation = server._get_effective_idle_modules(self.env)
            env, cfg, state = server._effective_programme(self.env)
        self.assertEqual([m.MODULE_ID for m in modules], ["tagesschau", "dwd_weather"], "Fensterreihenfolge, Gallery nicht eingerichtet")
        self.assertEqual(rotation, 600)
        self.assertEqual(cfg.idle_layout, "dashboard")
        self.assertEqual(cfg.dashboard_tiles, (("tagesschau", 70), ("gallery", 0), ("dwd_weather", 0)))
        self.assertEqual(env["IDLE_MODULES"], "tagesschau,gallery,dwd_weather")
        self.assertEqual(state["name"], "Morgens")
        self.assertEqual(config.get_cfg().idle_layout, "rotation", "globale Config bleibt unberührt")

    def test_outside_windows_the_programme_applies(self) -> None:
        with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 12, 0)):
            modules, rotation = server._get_effective_idle_modules(self.env)
            _, cfg, state = server._effective_programme(self.env)
        self.assertEqual([m.MODULE_ID for m in modules], ["dwd_weather", "tagesschau"])
        self.assertEqual((rotation, cfg.idle_layout, state["window"]), (120, "rotation", None))
        self.assertEqual(state["next"].name, "Nachts")

    def test_window_without_ready_content_falls_back_to_programme(self) -> None:
        settings = dict(config.get_settings_values())
        settings["SCHEDULE_WINDOWS"] = "Galerie|*|00:00-23:59|||gallery"
        config.apply_runtime_config(settings)
        with patch("app.server._get_local_now", return_value=THU_0700):
            modules, _ = server._get_effective_idle_modules(config.get_settings_values())
        self.assertEqual([m.MODULE_ID for m in modules], ["dwd_weather", "tagesschau"])

    def test_render_path_uses_the_window_layout(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        root = Path(tmp.name)
        paths = [patch.object(server, "CURRENT_IMAGE_PATH", root / "c.png"), patch.object(server, "CURRENT_BMP_PATH", root / "c.bmp"),
                 patch.object(server, "CURRENT_EPD_PATH", root / "c.epd"), patch.object(server, "STATE_PATH", root / "s.txt"),
                 patch("app.history.HISTORY_DIR", root / "h"), patch("app.history.HISTORY_INDEX", root / "h" / "i.json")]
        for p in paths:
            p.start()
        try:
            server._esp32_state = {"hash": "", "format": "png", "state": "idle", "media_type": "idle", "rendered_at": ""}
            with patch("app.server._get_local_now", return_value=THU_0700):
                key = server.render_if_changed(None)
            self.assertTrue(key.startswith("dashboard:"), key)
            self.assertEqual(self.news.rendered, ["tile"])
            self.assertEqual(self.weather.rendered, ["tile"])
            with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 12, 0)):
                key = server.render_if_changed(None)
            self.assertFalse(key.startswith("dashboard:"), "mittags gilt das Programm: Rotation")
        finally:
            for p in paths:
                p.stop()
            tmp.cleanup()

    def test_wake_interval_follows_window_borders(self) -> None:
        with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 8, 55)):
            seconds, reason = server._apply_schedule_interval(120, "Idle")
        self.assertEqual(seconds, 300)
        self.assertIn("„Morgens“ endet um 09:00", reason)
        with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 7, 30)):
            seconds, reason = server._apply_schedule_interval(120, "Idle")
        self.assertEqual(seconds, 600, "im Fenster gilt dessen Takt")
        with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 22, 59)):
            seconds, reason = server._apply_schedule_interval(120, "Idle")
        self.assertEqual(seconds, 60)
        self.assertIn("„Nachts“ beginnt um 23:00", reason)
        with patch("app.server._get_local_now", return_value=datetime(2026, 9, 10, 12, 0)):
            self.assertEqual(server._apply_schedule_interval(120, "Idle"), (120, "Idle"))
            self.assertLessEqual(server._get_background_poll_seconds(), 11 * 3600)


class DisplayApiScheduleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.weather = _Tile("dwd_weather", "Wetter")
        self.news = _NoTile("tagesschau", "Tagesschau")
        self._patches = _registry_patches([self.weather, self.news])
        for p in self._patches:
            p.start()
        self.client = server.app.test_client()
        self._apply_patch = patch("app.server._apply_updates_and_render", side_effect=lambda updates, wait_seconds=0.0: config.apply_runtime_config({**config.get_settings_values(), **updates}))
        self._apply_patch.start()

    def tearDown(self) -> None:
        self._apply_patch.stop()
        for p in reversed(self._patches):
            p.stop()
        config.apply_runtime_config()

    def _settings(self, **extra):
        settings = dict(config.read_env_settings())
        settings.update({"IDLE_MODULES": "dwd_weather,tagesschau", "IDLE_LAYOUT": "rotation", "SCHEDULE_WINDOWS": "", "NIGHT_MODE_ENABLED": "false"})
        settings.update(extra)
        config.apply_runtime_config(settings)

    def test_legacy_night_mode_is_reported_as_a_window(self) -> None:
        self._settings(NIGHT_MODE_ENABLED="true", NIGHT_MODE_START="22:30", NIGHT_MODE_END="06:30", NIGHT_MODE_INTERVAL_MINUTES="20",
                       NIGHT_MODE_IDLE_BEHAVIOR="fixed", NIGHT_MODE_FIXED_MODULE="tagesschau")
        with patch("app.display_api.now_local", return_value=datetime(2026, 9, 10, 23, 0), create=True), \
             patch("app.config.now_local", return_value=datetime(2026, 9, 10, 23, 0)):
            sch = self.client.get("/api/display").get_json()["schedule"]
        self.assertEqual(sch["source"], "night")
        (w,) = sch["windows"]
        self.assertEqual((w["name"], w["start"], w["end"], w["interval_seconds"]), ("Nachts", "22:30", "06:30", 1200))
        self.assertEqual(w["content"], [{"id": "tagesschau", "height": None}])
        self.assertTrue(w["active"])
        self.assertEqual(sch["active_name"], "Nachts")

    def test_saving_windows_writes_schedule_and_retires_night_mode(self) -> None:
        self._settings(NIGHT_MODE_ENABLED="true")
        payload = {"schedule": {"windows": [
            {"name": "Morgens", "days": [0, 1, 2, 3, 4], "start": "06:00", "end": "09:00", "layout": "rotation", "interval_seconds": 120,
             "content": [{"id": "dwd_weather", "height": None}]},
            {"name": "Nachts", "days": [0, 1, 2, 3, 4, 5, 6], "start": "23:00", "end": "07:00", "layout": "", "interval_seconds": 900, "content": []},
        ]}}
        response = self.client.put("/api/display", json=payload)
        self.assertEqual(response.status_code, 200, response.get_json())
        cfg = config.get_cfg()
        self.assertEqual([w.name for w in cfg.schedule_windows], ["Morgens", "Nachts"])
        self.assertEqual(cfg.schedule_windows[0].content, (("dwd_weather", 0),))
        self.assertFalse(cfg.night_mode_enabled)
        self.assertEqual(config.get_settings_values()["SCHEDULE_WINDOWS"],
                         "Morgens|Mo,Di,Mi,Do,Fr|06:00-09:00|rotation|120|dwd_weather; Nachts|*|23:00-07:00||900|")
        state = response.get_json()["display"]["schedule"]
        self.assertEqual(state["source"], "schedule")
        self.assertEqual(len(state["windows"]), 2)

    def test_invalid_windows_are_rejected_with_field_errors(self) -> None:
        self._settings()
        response = self.client.put("/api/display", json={"schedule": {"windows": [
            {"name": "Kaputt", "days": [], "start": "06:00", "end": "06:00", "layout": "dashboard", "interval_seconds": 0,
             "content": [{"id": "tagesschau"}, {"id": "nope"}]},
        ]}})
        self.assertEqual(response.status_code, 400)
        errors = response.get_json()["errors"]
        joined = " ".join([errors["fields"].get("SCHEDULE_WINDOWS", "")] + errors["general"])
        self.assertIn("Wochentag", joined)
        self.assertIn("nicht gleich", joined)
        self.assertIn("nope", joined)
        self.assertIn("keine Kachel", joined)
        self.assertEqual(config.get_cfg().schedule_windows, ())

    def test_settings_validation_rejects_broken_raw_value(self) -> None:
        errors = config.validate_settings({"SCHEDULE_WINDOWS": "Gut|*|08:00-10:00|||; Schlecht|Mo-Xy|8-10|||"})
        self.assertEqual(len(errors), 2)
        self.assertTrue(all(e.startswith("Zeitplan:") for e in errors))


if __name__ == "__main__":
    unittest.main()
