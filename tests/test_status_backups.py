"""
0.5.0: automatische Sicherungen von settings.env (app/settings_backup.py,
/api/settings/backups) und die Statusleiste jeder Seite (/api/issues).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app.config as config
import app.display_api as api
import app.server as server
import app.settings_backup as sb

BERLIN = ZoneInfo("Europe/Berlin")


class _EnvFileBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.env_file = Path(self._tmp.name) / "settings.env"
        self.env_file.write_text("DISPLAY_THEME=eink\nNOTIFY_URL=https://ntfy.sh/geheim-topic\n", encoding="utf-8")
        self._patch = patch.object(config, "ENV_FILE_PATH", self.env_file)
        self._patch.start()

    def tearDown(self) -> None:
        self._patch.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()


class SettingsBackupTest(_EnvFileBase):
    def test_every_change_keeps_the_previous_state(self) -> None:
        config.write_env_settings({"DISPLAY_THEME": "dark"})
        backups = sb.list_backups()
        self.assertEqual(len(backups), 1)
        self.assertIn("DISPLAY_THEME=eink", backups[0].read_text(encoding="utf-8"))
        config.write_env_settings({"DISPLAY_THEME": "dark"})
        self.assertEqual(len(sb.list_backups()), 1, "gleicher Inhalt: kein neuer Stand")

    def test_old_states_are_pruned_and_listed_newest_first(self) -> None:
        now = datetime(2026, 9, 26, 12, 0, tzinfo=BERLIN)
        for i in range(sb.KEEP + 5):
            sb.backup_text(f"X={i}\n", now=now + timedelta(minutes=i))
        backups = sb.list_backups()
        self.assertEqual(len(backups), sb.KEEP)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), f"X={sb.KEEP + 4}\n")
        self.assertEqual(sb.created_at(backups[0]), now + timedelta(minutes=sb.KEEP + 4))
        # Zwei Stände in derselben Sekunde bekommen verschiedene Namen
        a = sb.backup_text("A=1\n", now=now + timedelta(hours=5))
        b = sb.backup_text("A=2\n", now=now + timedelta(hours=5))
        self.assertNotEqual(a, b)
        self.assertEqual(sb.list_backups()[0], b)

    def test_find_accepts_only_backup_names(self) -> None:
        path = sb.backup_text("A=1\n")
        self.assertEqual(sb.find(path.name), path)
        for bad in ("../settings.env", "settings.env", "settings-2026.env", "", "settings-20260926-120000.env/../x"):
            with self.subTest(bad=bad):
                self.assertIsNone(sb.find(bad))

    def test_changed_keys(self) -> None:
        self.assertEqual(sb.changed_keys({"A": "1", "B": "2"}, {"A": "1", "B": "3", "C": "x"}), ["B", "C"])

    def test_restore_replaces_the_file_and_keeps_the_current_state(self) -> None:
        config.write_env_settings({"DISPLAY_THEME": "dark", "NEW_KEY": "1"})
        old = sb.list_backups()[0]
        sb.restore(old)
        text = self.env_file.read_text(encoding="utf-8")
        self.assertIn("DISPLAY_THEME=eink", text)
        self.assertNotIn("NEW_KEY", text, "Schlüssel von später verschwinden wieder")
        self.assertIn("NEW_KEY=1", sb.list_backups()[0].read_text(encoding="utf-8"), "der Stand davor ist gesichert")

    def test_api_lists_changes_without_values_and_restores(self) -> None:
        config.apply_runtime_config(config.read_env_settings())
        with patch.object(server, "request_render"):
            config.write_env_settings({"NOTIFY_URL": "https://ntfy.sh/anderes-geheimnis", "DISPLAY_THEME": "light"})
            client = server.app.test_client()
            listing = client.get("/api/settings/backups").get_json()
            self.assertEqual(listing["keep"], sb.KEEP)
            entry = listing["backups"][0]
            self.assertEqual({c["key"] for c in entry["changed"]}, {"NOTIFY_URL", "DISPLAY_THEME"})
            self.assertIn("Display-Theme", [c["label"] for c in entry["changed"]])
            body = json.dumps(listing)
            self.assertNotIn("geheim", body, "keine Werte, nur Namen")
            response = client.post(f"/api/settings/backups/{entry['id']}/restore")
            self.assertEqual(response.status_code, 200, response.get_json())
            self.assertEqual(config.get_cfg().display_theme, "eink", "Laufzeit-Konfiguration übernommen")
            self.assertEqual(client.post("/api/settings/backups/settings.env/restore").status_code, 404)
            self.assertEqual(client.post("/api/settings/backups/..%2Fsettings.env/restore").status_code, 404)

    def test_backup_failure_does_not_block_saving(self) -> None:
        with patch.object(sb, "backup_text", side_effect=OSError("voll")):
            config.write_env_settings({"DISPLAY_THEME": "light"})
        self.assertIn("DISPLAY_THEME=light", self.env_file.read_text(encoding="utf-8"))


class IssuesTest(unittest.TestCase):
    def setUp(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(), "IDLE_MODULES": ""})

    def tearDown(self) -> None:
        config.apply_runtime_config()

    def _ack(self, minutes_ago: int, **extra) -> dict:
        at = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
        return {"ack_at": at.strftime("%Y-%m-%dT%H:%M:%SZ"), "device_id": "esp", **extra}

    def test_all_fine_means_no_issues(self) -> None:
        with patch("app.device.firmware_info", return_value=None):
            self.assertEqual(api.build_issues({}, self._ack(2, fw_version="1.3.1"), 300, {"ok": True}), [])
            self.assertEqual(api.build_issues({}, {}, 300, {"ok": True}), [], "noch nie gemeldet: kein Hinweis")

    def test_device_worker_firmware_and_warnings(self) -> None:
        fw = {"version": "1.4.0"}
        with patch("app.device.firmware_info", return_value=fw), patch("app.device.firmware_update_expected", return_value=True):
            issues = api.build_issues({}, self._ack(90, fw_version="1.3.1"), 300, {"ok": False},
                                      alerts=[{"title": "Hochwasser", "severity": "severe"}, {"title": "Warntag", "severity": "minor"}])
        texts = [(i["level"], i["text"]) for i in issues]
        self.assertTrue(any(level == "danger" and "Render-Worker" in text for level, text in texts))
        self.assertTrue(any(level == "danger" and "seit 90 min nicht gemeldet" in text for level, text in texts))
        self.assertTrue(any(level == "info" and "Firmware 1.4.0 wartet" in text for level, text in texts))
        self.assertIn(("danger", "Warnung: Hochwasser"), texts)
        self.assertIn(("info", "Warnung: Warntag"), texts)

    def test_device_error_is_a_warning(self) -> None:
        with patch("app.device.firmware_info", return_value=None):
            issues = api.build_issues({}, self._ack(1, result="error", error="HTTP 404"), 300, {"ok": True})
        self.assertEqual([(i["level"], i["href"]) for i in issues], [("warn", "/geraet")])
        self.assertIn("HTTP 404", issues[0]["text"])

    def test_enabled_contents_that_are_not_ready_or_stale(self) -> None:
        def mod(mid, name, status):
            return SimpleNamespace(MODULE_ID=mid, MODULE_NAME=name, describe_status=lambda env: status)

        mods = [mod("calendar", "Kalender", {"state": "error", "reason": "nicht erreichbar"}),
                mod("garbage", "Müllabfuhr", {"state": "ready", "reason": ""}),
                mod("nina", "Warnungen (NINA)", {"state": "missing", "reason": "Ort fehlt"}),
                mod("steam", "Steam", {"state": "error", "reason": "aus"})]
        enabled = {"calendar", "garbage", "nina"}
        since = datetime(2026, 9, 26, 8, 30, tzinfo=BERLIN)
        with patch.object(api._registry, "get_idle_modules", return_value=mods), \
             patch.object(api, "_module_enabled", side_effect=lambda m, env: m.MODULE_ID in enabled):
            issues = api.build_issues({}, {}, 300, {"ok": True}, stale_sources=[("garbage", "Müllabfuhr", since)])
        self.assertEqual([(i["href"], i["text"]) for i in issues], [
            ("/inhalte#calendar", "Kalender: nicht erreichbar"),
            ("/inhalte#garbage", "Müllabfuhr: Quelle nicht erreichbar, gezeigt wird der Stand vom 26.09. 08:30."),
            ("/inhalte#nina", "Warnungen (NINA): Ort fehlt"),
        ])

    def test_endpoint(self) -> None:
        server._last_ack = {}
        with patch.object(server, "_stale_sources", return_value=[]), patch.object(server, "_active_alerts", return_value=[]):
            response = server.app.test_client().get("/api/issues")
        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.get_json()["issues"], list)

    def test_every_page_has_the_strip(self) -> None:
        client = server.app.test_client()
        for page in ("/", "/inhalte", "/geraet", "/system"):
            with self.subTest(page=page):
                self.assertIn('id="issueStrip"', client.get(page).get_data(as_text=True))


if __name__ == "__main__":
    unittest.main()
