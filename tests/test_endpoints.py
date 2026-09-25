"""
Erststart-Standardwerte (passend zum mitgelieferten Panel) und die kleineren
HTTP-Endpunkte: /refresh, /webhook, Modul-Rescan, Feldoptionen, Modul-Aktionen,
/current.* ohne BMP-Ausgabe.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

import app.config as config
import app.display_api as api
import app.server as server


class FirstRunDefaultsTest(unittest.TestCase):
    """Ohne eigene Einstellungen bekommt die mitgelieferte Firmware ein Bild, das sie annimmt."""

    def tearDown(self) -> None:
        config.apply_runtime_config()

    def test_defaults_match_the_panel(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin"})   # sonst nichts gesetzt
        cfg = config.get_cfg()
        self.assertEqual((cfg.render_width, cfg.render_height), (1200, 1600))
        self.assertEqual(cfg.output_format, "bmp")
        self.assertEqual(cfg.display_theme, "eink")
        self.assertEqual(cfg.display_rotation, 90)
        self.assertEqual(api.panel_setup_issues(cfg), [])

    def test_runtime_config_class_defaults_agree(self) -> None:
        cfg = config.RuntimeConfig()
        self.assertEqual((cfg.render_width, cfg.render_height), (1200, 1600))
        self.assertEqual((cfg.output_format, cfg.display_theme, cfg.display_rotation), ("bmp", "eink", 90))

    def test_form_shows_the_defaults_instead_of_the_first_option(self) -> None:
        """Sonst schriebe das Speichern der Gerät-Karte still „png“, „0°“ und „dark“ zurück."""
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin"})
        with patch.object(api, "get_settings_values", return_value={"TIMEZONE": "Europe/Berlin"}):
            fields = {f["name"]: f["value"] for f in api.build_module_settings("framework")["fields"]}
        self.assertEqual(fields["OUTPUT_FORMAT"], "bmp")
        self.assertEqual(fields["DISPLAY_THEME"], "eink")
        self.assertEqual(fields["DISPLAY_ROTATION"], "90")

    def test_setup_issues_name_format_and_size(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin", "OUTPUT_FORMAT": "png", "DISPLAY_ROTATION": "0"})
        issues = api.panel_setup_issues(config.get_cfg())
        self.assertEqual(len(issues), 2)
        self.assertIn("PNG", issues[0])
        self.assertIn("1600 × 1200", issues[1])
        state = server.app.test_client().get("/api/display").get_json()
        self.assertEqual(state["panel"]["setup_issues"], issues)

    def test_bmp_endpoints_explain_404_without_bmp_output(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin", "OUTPUT_FORMAT": "png"})
        client = server.app.test_client()
        for path in ("/current.bmp", "/current.epd"):
            with self.subTest(path=path):
                response = client.get(path)
                self.assertEqual(response.status_code, 404)
                self.assertIn("OUTPUT_FORMAT=bmp", response.get_data(as_text=True))


class SmallEndpointsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.app.test_client()

    def test_refresh_reports_completed_or_queued(self) -> None:
        with patch.object(server, "request_render", return_value=True) as rr:
            data = self.client.post("/refresh").get_json()
        self.assertEqual((data["ok"], data["completed"], data["message"]), (True, True, "refreshed"))
        rr.assert_called_once()
        with patch.object(server, "request_render", return_value=False):
            self.assertEqual(self.client.post("/refresh").get_json()["message"], "queued")

    def test_plex_webhook_queues_a_render(self) -> None:
        with patch.object(server, "request_render") as rr:
            response = self.client.post("/webhook", data={"payload": '{"event": "media.play"}'})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json(), {"ok": True, "queued": True})
        self.assertEqual(rr.call_args.kwargs.get("reason"), "Webhook")

    def test_rescan_and_module_list(self) -> None:
        from types import SimpleNamespace
        info = [{"id": "demo", "name": "Demo"}]
        loaded = [SimpleNamespace(MODULE_ID="demo"), SimpleNamespace(MODULE_ID="other")]
        with patch.object(server._registry, "reload_modules", return_value=loaded), \
             patch.object(server._registry, "get_module_info_list", return_value=info):
            data = self.client.post("/api/rescan-modules").get_json()
            self.assertEqual((data["ok"], data["count"], data["modules"]), (True, 2, info))
            self.assertEqual(self.client.get("/api/modules").get_json(), info)

    def test_rescan_failure_is_reported_without_secrets(self) -> None:
        with patch.object(server._registry, "reload_modules", side_effect=RuntimeError("kaputt https://x/?token=geheim")):
            response = self.client.post("/api/rescan-modules")
        self.assertEqual(response.status_code, 500)
        self.assertNotIn("geheim", response.get_data(as_text=True))

    def test_field_options(self) -> None:
        with patch.object(server._registry, "get_module_field_options", return_value=[["a", "A"]]):
            self.assertEqual(self.client.get("/api/module-field-options/demo/X").get_json(), [["a", "A"]])
        with patch.object(server._registry, "get_module_field_options", return_value=None):
            self.assertEqual(self.client.get("/api/module-field-options/demo/X").status_code, 404)

    def test_module_action(self) -> None:
        class _Mod:
            def handle_api_action(self, action, env):
                return ({"ok": True, "action": action}, 200) if action == "ping" else None

        with patch.object(server._registry, "get_module_by_id", side_effect=lambda mid: _Mod() if mid == "demo" else None):
            self.assertEqual(self.client.get("/api/module-action/demo/ping").get_json(), {"ok": True, "action": "ping"})
            self.assertEqual(self.client.get("/api/module-action/demo/nope").status_code, 404)
            self.assertEqual(self.client.get("/api/module-action/other/ping").status_code, 404)


if __name__ == "__main__":
    unittest.main()
