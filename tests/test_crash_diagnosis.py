"""
0.8.0 / Firmware 1.3.2: der Arbeitsschritt, in dem ein Start ungeplant endete
(Unterspannung, Absturz), kommt mit der Rückmeldung und erscheint im Ereignis,
im Verlauf und in den Kennzahlen.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path
from unittest.mock import patch

import app.device as device
import app.monitoring as monitoring
import app.server as server

FIRMWARE = Path(__file__).resolve().parents[1] / "esp32" / "Inkwall" / "Inkwall.ino"


class CrashTextTest(unittest.TestCase):
    def test_normalize_keeps_the_new_fields(self) -> None:
        ack = device.normalize_ack({"reset_reason": "brownout", "last_phase": "display", "last_phase_ms": "14210",
                                    "last_phase_boot": 229, "unbekannt": "x"}, "10.0.0.5")
        self.assertEqual((ack["last_phase"], ack["last_phase_ms"], ack["last_phase_boot"]), ("display", 14210, 229))
        self.assertNotIn("unbekannt", ack)

    def test_text(self) -> None:
        self.assertEqual(device.crash_text({"reset_reason": "brownout", "last_phase": "display", "last_phase_boot": 229}),
                         "Unterspannung beim Bildaufbau am Panel (Start #229)")
        self.assertEqual(device.crash_text({"reset_reason": "brownout"}), "Unterspannung", "Firmware vor 1.3.2")
        self.assertEqual(device.crash_text({"reset_reason": "panic", "last_phase": "neu"}), "Absturz im Schritt „neu“")
        self.assertEqual(device.crash_text({"reset_reason": "deepsleep", "last_phase": "display"}), "")

    def test_every_firmware_phase_has_a_label(self) -> None:
        source = FIRMWARE.read_text(encoding="utf-8")
        names = re.search(r"PHASE_NAMES\[PH_COUNT\] = \{([^}]*)\}", source).group(1)
        phases = [n.strip().strip('"') for n in names.split(",") if n.strip().strip('"')]
        self.assertEqual(sorted(phases), sorted(device.PHASE_LABELS))


class HistoryAndStatsTest(unittest.TestCase):
    def test_history_entry_and_stats(self) -> None:
        with patch.object(monitoring, "ACK_HISTORY_PATH", Path(self._tmp()) / "acks.jsonl"):
            monitoring.record_ack({"ack_at": "2026-09-26T09:10:54Z", "result": "updated", "reset_reason": "brownout",
                                   "last_phase": "display"}, True)
            monitoring.record_ack({"ack_at": "2026-09-26T09:15:49Z", "result": "updated", "reset_reason": "deepsleep"}, True)
            monitoring.record_ack({"ack_at": "2026-09-26T09:20:49Z", "result": "updated", "reset_reason": "brownout",
                                   "last_phase": "wifi"}, True)
            monitoring.record_ack({"ack_at": "2026-09-26T09:25:49Z", "result": "updated", "reset_reason": "brownout",
                                   "last_phase": "display"}, True)
            entries = monitoring.read_ack_history()
        self.assertEqual([e.get("reset") for e in entries], ["brownout", None, "brownout", "brownout"])
        self.assertEqual(entries[0]["phase"], "display")
        stats = monitoring.ack_stats(entries)
        self.assertEqual(stats["crashes"], 3)
        self.assertEqual(stats["crash_reasons"], {"brownout": 3})
        self.assertEqual(stats["crash_phases"], {"display": 2, "wifi": 1})

    def _tmp(self) -> str:
        import tempfile
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        return tmp.name


class AckEventTest(unittest.TestCase):
    def test_event_names_the_step(self) -> None:
        events = []
        with patch.object(server, "log_event", side_effect=lambda kind, msg, *a: events.append(msg)), \
             patch.object(monitoring, "record_ack"), patch.object(server, "_dispatch_ack_notifications"), \
             patch("app.device.update_device_state"), patch("app.device.append_device_log"):
            response = server.app.test_client().post("/ack", json={
                "device_id": "esp", "result": "updated", "reset_reason": "brownout", "last_phase": "flash", "last_phase_boot": 12})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(any("Unterspannung beim Speichern im Flash (Start #12)" in e for e in events), events)


if __name__ == "__main__":
    unittest.main()
