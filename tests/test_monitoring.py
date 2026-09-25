"""
Tests für die Beobachtung: Zähler, ACK-Historie und Kennzahlen, Benachrichtigung
(einmal bei Ausfall, Entwarnung bei Rückmeldung, Formate je Ziel), Prometheus-
Text und die Routen /metrics, /api/device/history, /api/notify/test.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import app.config as config
import app.device as device
import app.http_client as http_client
import app.monitoring as mon
import app.server as server


def _ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


NOW = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)


class _Isolated(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        root = Path(self._tmp.name)
        self._patches = [
            patch.object(mon, "ACK_HISTORY_PATH", root / "ack_history.jsonl"),
            patch.object(device, "DEVICE_STATE_PATH", root / "device_state.json"),
        ]
        for p in self._patches:
            p.start()
        mon.reset_counters()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        mon.reset_counters()
        self._tmp.cleanup()


class CountersAndHistoryTest(_Isolated):
    def test_counters_with_labels(self) -> None:
        mon.increment("inkwall_renders_total", module="dwd_weather")
        mon.increment("inkwall_renders_total", module="dwd_weather")
        mon.increment("inkwall_renders_total", module="calendar")
        self.assertEqual(mon.counter_values(), [
            ("inkwall_renders_total", {"module": "calendar"}, 1),
            ("inkwall_renders_total", {"module": "dwd_weather"}, 2),
        ])

    def test_record_read_and_stats(self) -> None:
        stamps = [NOW - timedelta(minutes=m) for m in (60, 55, 50, 20, 15, 10)]
        for i, when in enumerate(stamps):
            ack = {"ack_at": _ts(when), "result": "error" if i == 3 else "updated", "rssi": -60 - i * 3, "cycle_ms": 8000 + i * 500,
                   "device_id": "esp", "fw_version": "1.2.3", "error": "boom" if i == 3 else ""}
            mon.record_ack(ack, hash_matches=True)
        entries = mon.read_ack_history(hours=24, now=NOW)
        self.assertEqual(len(entries), 6)
        self.assertEqual(entries[0]["t"], _ts(stamps[0]), "chronologisch")
        self.assertEqual(entries[3]["error"], "boom")
        self.assertNotIn("error", entries[0])
        self.assertEqual(len(mon.read_ack_history(hours=0.5, now=NOW)), 3, "nur die letzte halbe Stunde")
        self.assertEqual(len(mon.read_ack_history(limit=2, now=NOW)), 2)

        stats = mon.ack_stats(entries, expected_seconds=300, now=NOW)
        self.assertEqual(stats["count"], 6)
        self.assertEqual(stats["errors"], 1)
        self.assertEqual(stats["longest_gap_s"], 30 * 60, "50 → 20 min vor jetzt")
        self.assertEqual(stats["gaps_over_threshold"], 1, "30 min > 3 × 5 min; 10 min seit der letzten Meldung nicht")
        self.assertEqual(stats["since_last_s"], 600)
        self.assertEqual(stats["rssi_min"], -75)
        self.assertEqual(stats["avg_cycle_ms"], 9250)
        self.assertEqual(stats["results"], {"updated": 5, "error": 1})

    def test_history_is_trimmed(self) -> None:
        with patch.object(mon, "ACK_HISTORY_KEEP", 5):
            for i in range(12):
                mon.record_ack({"ack_at": _ts(NOW - timedelta(minutes=12 - i)), "result": "unchanged"})
        lines = mon.ACK_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
        # Getrimmt wird, sobald das Doppelte überschritten ist (nach dem 11. Eintrag auf 5), danach wächst es wieder
        self.assertEqual(len(lines), 6)
        self.assertEqual(mon.clear_ack_history(), 6)
        self.assertEqual(mon.read_ack_history(), [])

    def test_empty_stats(self) -> None:
        stats = mon.ack_stats([], now=NOW)
        self.assertEqual((stats["count"], stats["longest_gap_s"], stats["avg_cycle_ms"], stats["since_last_s"]), (0, 0, None, None))


class NotificationTest(_Isolated):
    def _capture(self):
        calls: list[dict] = []

        def fake_post(url, **kw):
            calls.append({"url": url, **kw})
            return SimpleNamespace(status_code=200)

        return calls, patch.object(http_client.HTTP_SESSION, "post", side_effect=fake_post)

    def test_formats_per_target(self) -> None:
        calls, p = self._capture()
        with p:
            self.assertTrue(mon.send_notification("https://ntfy.sh/topic", "Titel", "Text", tags="warning", priority="high"))
            self.assertTrue(mon.send_notification("https://discord.com/api/webhooks/1/x", "Titel", "Text"))
            self.assertTrue(mon.send_notification("https://hooks.slack.com/services/x", "Titel", "Text"))
            self.assertFalse(mon.send_notification("ftp://nope", "Titel", "Text"))
        self.assertEqual(calls[0]["data"], b"Text")
        self.assertEqual(calls[0]["headers"]["Title"], "Titel")
        self.assertEqual(calls[0]["headers"]["Tags"], "warning")
        self.assertEqual(calls[0]["headers"]["Priority"], "high")
        self.assertEqual(calls[1]["json"], {"content": "**Titel**\nText"})
        self.assertEqual(calls[2]["json"], {"text": "*Titel*\nText"})

    def test_rejected_or_failing_delivery_is_false(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "post", return_value=SimpleNamespace(status_code=404)):
            self.assertFalse(mon.send_notification("https://ntfy.sh/x", "t", "m"))
        with patch.object(http_client.HTTP_SESSION, "post", side_effect=RuntimeError("offline")):
            self.assertFalse(mon.send_notification("https://ntfy.sh/x", "t", "m"))

    def test_offline_is_reported_once_and_online_after_ack(self) -> None:
        calls, p = self._capture()
        ack = {"ack_at": _ts(NOW - timedelta(minutes=45)), "device_id": "esp32-1"}
        with p:
            self.assertIsNone(mon.check_device_offline(ack, "https://ntfy.sh/x", 60, now=NOW), "noch unter der Schwelle")
            self.assertEqual(mon.check_device_offline(ack, "https://ntfy.sh/x", 30, now=NOW), "offline")
            self.assertIsNone(mon.check_device_offline(ack, "https://ntfy.sh/x", 30, now=NOW + timedelta(minutes=5)), "nur einmal")
            self.assertIsNone(mon.check_device_offline(ack, "", 30, now=NOW), "ohne Adresse nichts")
            self.assertIsNone(mon.check_device_offline({}, "https://ntfy.sh/x", 30, now=NOW), "ohne Rückmeldung nichts")
            state = device.load_device_state()
            self.assertTrue(state.get("offline_notified_at"))
            later = {"ack_at": _ts(NOW + timedelta(minutes=20)), "device_id": "esp32-1"}
            self.assertEqual(mon.note_device_ack(later, "https://ntfy.sh/x", now=NOW + timedelta(minutes=20)), "online")
            self.assertIsNone(mon.note_device_ack(later, "https://ntfy.sh/x", now=NOW + timedelta(minutes=25)), "Merker gelöscht")
        self.assertEqual(len(calls), 2)
        self.assertIn("seit 45 min nicht gemeldet", calls[0]["data"].decode("utf-8"))
        self.assertIn("esp32-1", calls[0]["data"].decode("utf-8"))
        self.assertIn("wieder", calls[1]["data"].decode("utf-8"))
        self.assertNotIn("offline_notified_at", device.load_device_state())

    def test_threshold_zero_disables(self) -> None:
        ack = {"ack_at": _ts(NOW - timedelta(hours=5)), "device_id": "esp"}
        with patch.object(http_client.HTTP_SESSION, "post") as post:
            self.assertIsNone(mon.check_device_offline(ack, "https://ntfy.sh/x", 0, now=NOW))
            post.assert_not_called()


class PrometheusTextTest(_Isolated):
    def test_text_contains_counters_gauges_and_escaped_labels(self) -> None:
        mon.increment("inkwall_renders_total", module="dwd_weather")
        mon.increment("inkwall_acks_total", result="updated")
        mon.record_ack({"ack_at": _ts(NOW - timedelta(minutes=5)), "result": "updated", "rssi": -66, "cycle_ms": 9000, "device_id": 'esp "one"'})
        esp = {"hash": "abc", "media_type": "dwd_weather", "rendered_at": _ts(NOW - timedelta(minutes=3)), "state": "x"}
        ack = {"ack_at": _ts(NOW - timedelta(minutes=5)), "hash": "abc", "device_id": 'esp "one"', "rssi": -66, "cycle_ms": 9000, "fw_version": "1.2.3"}
        text = mon.prometheus_text(esp, ack, (120, "Idle"), 60,
                                   [{"id": "dwd_weather", "enabled": True, "state": "ready"}, {"id": "calendar", "enabled": False, "state": "missing"}],
                                   {"window": SimpleNamespace(name="Nachts"), "seconds_until_change": 500}, offline_minutes=30, now=NOW)
        self.assertIn(f'inkwall_info{{version="{config.APP_VERSION}"}} 1', text)
        self.assertIn('inkwall_renders_total{module="dwd_weather"} 1', text)
        self.assertIn('inkwall_acks_total{result="updated"} 1', text)
        self.assertIn("inkwall_image_age_seconds 180", text)
        self.assertIn("inkwall_next_wake_seconds 120", text)
        self.assertIn('inkwall_device_last_ack_age_seconds{device="esp \\"one\\""} 300', text)
        self.assertIn('inkwall_device_online{device="esp \\"one\\""} 1', text)
        self.assertIn('inkwall_device_hash_matches{device="esp \\"one\\""} 1', text)
        self.assertIn('inkwall_device_rssi_dbm{device="esp \\"one\\""} -66', text)
        self.assertIn('inkwall_device_firmware_info{device="esp \\"one\\"",version="1.2.3"} 1', text)
        self.assertIn('inkwall_device_acks{device="esp \\"one\\"",hours="24"} 1', text)
        self.assertIn('inkwall_module_ready{module="calendar",state="missing"} 0', text)
        self.assertIn('inkwall_module_enabled{module="dwd_weather"} 1', text)
        self.assertIn('inkwall_schedule_window_active{window="Nachts"} 1', text)
        self.assertIn("inkwall_schedule_seconds_until_change 500", text)
        self.assertTrue(text.endswith("\n"))

    def test_offline_device_reports_zero(self) -> None:
        ack = {"ack_at": _ts(NOW - timedelta(hours=2)), "device_id": "esp"}
        text = mon.prometheus_text({}, ack, (120, ""), 60, [], None, offline_minutes=30, now=NOW)
        self.assertIn('inkwall_device_online{device="esp"} 0', text)


class RoutesTest(_Isolated):
    def setUp(self) -> None:
        super().setUp()
        self.client = server.app.test_client()
        self._ack = patch.object(server, "_last_ack", {})
        self._ack.start()

    def tearDown(self) -> None:
        self._ack.stop()
        super().tearDown()

    def test_metrics_and_history_routes(self) -> None:
        response = self.client.post("/ack", json={"device_id": "esp-t", "hash": "x", "result": "updated", "rssi": -70, "cycle_ms": 8100})
        self.assertEqual(response.status_code, 200)
        metrics = self.client.get("/metrics")
        self.assertEqual(metrics.status_code, 200)
        self.assertIn("text/plain", metrics.content_type)
        body = metrics.get_data(as_text=True)
        self.assertIn("inkwall_info", body)
        self.assertIn('inkwall_acks_total{result="updated"} 1', body)
        self.assertIn('inkwall_device_rssi_dbm{device="esp-t"} -70', body)
        history = self.client.get("/api/device/history?hours=24").get_json()
        self.assertEqual(history["stats"]["count"], 1)
        self.assertEqual(history["entries"][0]["rssi"], -70)
        self.assertEqual(history["entries"][0]["match"], False)
        self.assertEqual(device.load_device_state()["last_ack"]["device_id"], "esp-t", "letzte Rückmeldung wird gesichert")
        self.assertEqual(self.client.delete("/api/device/history").get_json()["removed"], 1)

    def test_notify_test_route(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(), "NOTIFY_URL": ""})
        try:
            self.assertEqual(self.client.post("/api/notify/test", json={}).status_code, 400)
            self.assertEqual(self.client.post("/api/notify/test", json={"url": "ftp://x"}).status_code, 400)
            with patch.object(http_client.HTTP_SESSION, "post", return_value=SimpleNamespace(status_code=200)) as post:
                self.assertEqual(self.client.post("/api/notify/test", json={"url": "https://ntfy.sh/x"}).status_code, 200)
                self.assertEqual(post.call_args.args[0], "https://ntfy.sh/x")
            with patch.object(http_client.HTTP_SESSION, "post", return_value=SimpleNamespace(status_code=500)):
                self.assertEqual(self.client.post("/api/notify/test", json={"url": "https://ntfy.sh/x"}).status_code, 502)
        finally:
            config.apply_runtime_config()

    def test_restore_last_ack_from_device_state(self) -> None:
        device.save_device_state({"last_ack": {"ack_at": _ts(NOW), "device_id": "esp-r", "hash": "h"}})
        with patch.object(server, "_last_ack", {}):
            server._restore_last_ack()
            self.assertEqual(server._last_ack["device_id"], "esp-r")

    def test_settings_validation_for_notify_url(self) -> None:
        self.assertTrue(any("http" in e for e in config.validate_settings({"NOTIFY_URL": "ntfy.sh/x"})))
        self.assertEqual(config.validate_settings({"NOTIFY_URL": "https://ntfy.sh/x"}), [])


if __name__ == "__main__":
    unittest.main()
