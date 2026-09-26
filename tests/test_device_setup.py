"""
0.6.0: Einrichtungskarte – Einstellungen fürs Panel mit einem Klick,
Kontakte des Geräts (fragt nach dem Bild, scheitert am Token) und die
Hinweise dazu in der Statusleiste.
"""

from __future__ import annotations

import time
import unittest
from datetime import datetime, timezone
from unittest.mock import patch

import app.config as config
import app.display_api as api
import app.server as server


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        server._device_contact.update(meta=None, rejected=None)
        server._last_ack = {}
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        server._device_contact.update(meta=None, rejected=None)
        server._last_ack = {}
        config.apply_runtime_config()


class PanelFixTest(_Base):
    def test_fix_updates_only_what_is_wrong(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin", "OUTPUT_FORMAT": "png", "DISPLAY_ROTATION": "0", "DISPLAY_THEME": "dark"})
        self.assertEqual(api.panel_fix_updates(config.get_cfg()),
                         {"OUTPUT_FORMAT": "bmp", "RENDER_WIDTH": "1600", "RENDER_HEIGHT": "1200", "DISPLAY_ROTATION": "90"})
        self.assertEqual(api.panel_fix_updates(config.get_cfg(), theme=True)["DISPLAY_THEME"], "eink")
        # 270° (auf dem Kopf) ist auch Hochformat 1200 × 1600 – bleibt
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin", "DISPLAY_ROTATION": "270", "DISPLAY_THEME": "eink"})
        self.assertEqual(api.panel_fix_updates(config.get_cfg(), theme=True), {})

    def test_fix_endpoint_applies_and_reports(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin", "OUTPUT_FORMAT": "png"})
        with patch.object(server, "write_env_settings") as write, patch.object(server, "request_render"):
            data = self.client.post("/api/device/setup/fix", json={"theme": False}).get_json()
        self.assertEqual(data["changed"], ["OUTPUT_FORMAT"])
        self.assertEqual(write.call_args[0][0], {"OUTPUT_FORMAT": "bmp"})
        self.assertEqual(data["setup"]["issues"], [], "danach passt es")
        with patch.object(server, "write_env_settings") as write, patch.object(server, "request_render"):
            again = self.client.post("/api/device/setup/fix", json={}).get_json()
        self.assertEqual(again["changed"], [])
        write.assert_not_called()


class SetupStateTest(_Base):
    def test_fresh_server_without_device(self) -> None:
        config.apply_runtime_config({"TIMEZONE": "Europe/Berlin"})
        with patch.object(server, "_device_token", return_value=""):
            state = self.client.get("/api/device/setup").get_json()
        self.assertEqual(state["issues"], [])
        self.assertIsNone(state["device"])
        self.assertFalse(state["token_required"])
        self.assertEqual(state["contact"], {"meta": None, "rejected": None})
        self.assertIn("PartitionScheme=app3M_fat9M_16MB", state["build"]["fqbn"])

    def test_token_required_is_a_flag_not_the_value(self) -> None:
        with patch.object(server, "_device_token", return_value="sehr-geheim"):
            body = self.client.get("/api/device/setup").get_data(as_text=True)
        self.assertIn('"token_required":true', body.replace(" ", ""))
        self.assertNotIn("sehr-geheim", body)

    def test_device_asking_for_the_image_is_noted(self) -> None:
        self.client.get("/meta.json?sleep=from_meta", headers={"User-Agent": "ESP32HTTPClient"})
        state = self.client.get("/api/device/setup").get_json()
        self.assertEqual(state["contact"]["meta"]["path"], "/meta.json")
        # Ein Browser, der /meta.json öffnet, ist kein Gerät
        server._device_contact["meta"] = None
        self.client.get("/meta.json", headers={"User-Agent": "Mozilla/5.0"})
        self.assertIsNone(server._device_contact["meta"])

    def test_token_rejection_of_a_device_is_noted_until_it_reports(self) -> None:
        with patch.object(server, "_device_token", return_value="richtig"):
            self.client.get("/firmware.json", headers={"User-Agent": "Mozilla/5.0"})
            self.assertIsNone(server._device_contact["rejected"], "Klick im Browser zählt nicht")
            self.client.post("/ack", json={}, headers={"User-Agent": "ESP32HTTPClient", "X-Inkwall-Token": "falsch"})
            state = self.client.get("/api/device/setup").get_json()
        self.assertEqual(state["contact"]["rejected"]["path"], "/ack")
        issues = api.build_issues({}, {}, 300, {"ok": True}, contact=server._device_contact)
        self.assertTrue(any("abgewiesen" in i["text"] for i in issues))
        # Eine spätere Rückmeldung (mit richtigem Token) hebt den Hinweis auf
        later = datetime.fromtimestamp(time.time() + 5, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        self.assertIsNone(api.recent_rejection({"ack_at": later}, server._device_contact))
        old = {"rejected": {"at": time.time() - 2 * 3600, "remote": "x", "path": "/ack"}}
        self.assertIsNone(api.recent_rejection({}, old), "nach einer Stunde nicht mehr aktuell")

    def test_device_card_is_on_the_page(self) -> None:
        html = self.client.get("/geraet").get_data(as_text=True)
        for marker in ('id="setupCard"', 'id="suConfig"', 'id="suFlash"', 'id="step4Body"'):
            self.assertIn(marker, html)


if __name__ == "__main__":
    unittest.main()
