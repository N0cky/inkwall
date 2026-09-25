"""
Protokoll zwischen Server und Firmware ab 1.3.0:
- Firmware wird nur eingespielt, wenn sie neuer ist – ausser beim Hochladen erzwungen
- /meta.json trägt firmware_force und bestätigt sleep_from=meta
- mit sleep=from_meta rechnet der Server nur die Zeit bis meta.json ein (meta_ms)
- das ACK trägt reset_reason und meta_ms; ein Absturz wird als Ereignis gemeldet
"""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from app import device, server
from tests.test_device import _TmpFirmware, _fake_firmware


class VersionTest(unittest.TestCase):
    def test_version_key(self) -> None:
        self.assertEqual(device.version_key("1.3.0"), (1, 3, 0))
        self.assertEqual(device.version_key("1.10"), (1, 10, 0))
        self.assertEqual(device.version_key("1.2.3-selftest"), (1, 2, 3))
        self.assertEqual(device.version_key("kaputt"), (0, 0, 0))
        self.assertGreater(device.version_key("1.10.0"), device.version_key("1.9.9"))

    def test_update_expected(self) -> None:
        hosted = {"version": "1.3.1", "force": False}
        self.assertTrue(device.firmware_update_expected(hosted, "1.3.0"))
        self.assertFalse(device.firmware_update_expected({"version": "1.2.9"}, "1.3.0"), "älter: 1.3.0 bleibt")
        self.assertFalse(device.firmware_update_expected({"version": "1.3.0-selftest"}, "1.3.0"), "nicht neuer")
        self.assertTrue(device.firmware_update_expected({"version": "1.2.9", "force": True}, "1.3.0"), "erzwungen")
        self.assertTrue(device.firmware_update_expected({"version": "1.2.2"}, "1.2.3"), "alte Firmware nimmt jede andere Version")
        self.assertFalse(device.firmware_update_expected({"version": "1.3.0"}, "1.3.0"))
        self.assertFalse(device.firmware_update_expected(None, "1.3.0"))
        self.assertFalse(device.firmware_update_expected(hosted, ""), "Gerät ohne Versionsangabe")


class ForcedUploadTest(_TmpFirmware):
    def _upload(self, version: str, force: bool) -> dict:
        data = {"file": (io.BytesIO(_fake_firmware(version)), "fw.bin")}
        if force:
            data["force"] = "1"
        response = server.app.test_client().post("/api/device/firmware", data=data, content_type="multipart/form-data")
        self.assertEqual(response.status_code, 200, response.get_json())
        return response.get_json()["firmware"]

    def test_force_flag_reaches_meta_and_display(self) -> None:
        client = server.app.test_client()
        self.assertFalse(self._upload("1.2.0", force=False)["force"])
        server._last_ack = {"fw_version": "1.3.0", "ack_at": "2026-09-25T10:00:00Z"}
        self.assertFalse(client.get("/meta.json").get_json()["firmware_force"])
        state = client.get("/api/display").get_json()["firmware"]
        self.assertFalse(state["update_pending"], "1.3.0 spielt 1.2.0 nicht ein")
        self.assertTrue(state["not_newer"])

        self.assertTrue(self._upload("1.2.0", force=True)["force"])
        self.assertTrue(client.get("/meta.json").get_json()["firmware_force"])
        state = client.get("/api/display").get_json()["firmware"]
        self.assertTrue(state["update_pending"])
        self.assertTrue(state["force"])


class SleepFromMetaTest(unittest.TestCase):
    def setUp(self) -> None:
        self._ack = patch.object(server, "_last_ack", {"cycle_ms": 42000, "meta_ms": 6400})
        self._ack.start()

    def tearDown(self) -> None:
        self._ack.stop()

    def test_meta_confirms_the_mode_only_when_asked(self) -> None:
        client = server.app.test_client()
        self.assertEqual(client.get("/meta.json?sleep=from_meta").get_json().get("sleep_from"), "meta")
        self.assertNotIn("sleep_from", client.get("/meta.json").get_json())

    def test_rotation_wake_counts_only_the_time_before_meta(self) -> None:
        self.assertEqual(server._device_pre_meta_seconds(), 6)
        seen: list = []

        def capture(rotation, now=None, cycle_seconds=None):
            seen.append(cycle_seconds)
            return 100, 0.0

        with patch.object(server, "_aligned_rotation_wake", side_effect=capture), \
             patch.object(server, "_get_schedule_state", return_value={"window": None, "next": None, "seconds_until_change": 0}):
            server._rotation_wake(from_meta=True)
            server._rotation_wake()
        self.assertEqual(seen, [6, None], "mit sleep=from_meta nur die Zeit bis meta.json, sonst wie bisher")

    def test_default_without_measurement(self) -> None:
        with patch.object(server, "_last_ack", {}):
            self.assertEqual(server._device_pre_meta_seconds(), server.DEFAULT_DEVICE_PRE_META_S)


class AckFieldsTest(_TmpFirmware):
    def test_reset_reason_and_meta_time_are_kept(self) -> None:
        ack = device.normalize_ack({"reset_reason": "task_wdt", "meta_ms": "7123", "result": "updated"}, "1.2.3.4")
        self.assertEqual(ack["reset_reason"], "task_wdt")
        self.assertEqual(ack["meta_ms"], 7123)

    def test_crash_is_reported_as_event(self) -> None:
        client = server.app.test_client()
        with patch.object(server, "log_event") as log_event:
            client.post("/ack", json={"device_id": "esp", "hash": "x", "result": "updated", "reset_reason": "panic"})
            client.post("/ack", json={"device_id": "esp", "hash": "x", "result": "updated", "reset_reason": "deepsleep"})
        crash_events = [c for c in log_event.call_args_list if "neu gestartet" in str(c)]
        self.assertEqual(len(crash_events), 1)
        self.assertIn("Absturz", str(crash_events[0]))
        state = client.get("/api/display").get_json()["device"]
        self.assertEqual(state["reset_reason"], "deepsleep")
        self.assertEqual(state["reset_label"], "Aufwachen")


if __name__ == "__main__":
    unittest.main()
