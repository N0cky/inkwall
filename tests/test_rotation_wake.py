"""
Tests für die Slot-Ausrichtung der Idle-Rotation: Das Gerät soll jedes Bild genau
einmal holen, kurz nachdem es an der Slot-Grenze entstanden ist – statt mit jedem
Zyklus um die eigene Zyklusdauer zu driften.
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

import app.server as server
from app.schedule import ALL_DAYS, Window


class AlignedWakeTest(unittest.TestCase):
    def test_wake_lands_after_next_boundary(self) -> None:
        # Grenze bei 1200, Gerät braucht 40 s pro Zyklus, holen soll es 15 s nach der Grenze
        seconds, boundary = server._aligned_rotation_wake(300, now=1000.0, cycle_seconds=40)
        self.assertEqual((seconds, boundary), (175, 1200.0))
        self.assertEqual(1000 + 40 + seconds, 1200 + server.ROTATION_WAKE_MARGIN_S, "Aufwachen = Grenze + Marge")

    def test_boundary_too_close_skips_to_the_next_one(self) -> None:
        seconds, boundary = server._aligned_rotation_wake(300, now=1190.0, cycle_seconds=40)
        self.assertEqual(boundary, 1500.0)
        self.assertEqual(seconds, 285)
        self.assertGreaterEqual(seconds, server.MIN_WAKE_S)

    def test_device_cycle_from_last_ack(self) -> None:
        with patch.dict(server._last_ack, {"cycle_ms": 38077}, clear=True):
            self.assertEqual(server._device_cycle_seconds(), 38)
        with patch.dict(server._last_ack, {}, clear=True):
            self.assertEqual(server._device_cycle_seconds(), server.DEFAULT_DEVICE_CYCLE_S)
        with patch.dict(server._last_ack, {"cycle_ms": "kaputt"}, clear=True):
            self.assertEqual(server._device_cycle_seconds(), server.DEFAULT_DEVICE_CYCLE_S)
        with patch.dict(server._last_ack, {"cycle_ms": 900000}, clear=True):
            self.assertEqual(server._device_cycle_seconds(), 180, "nach oben begrenzt")


class SuggestNextWakeTest(unittest.TestCase):
    def _run(self, schedule_state: dict, now: float = 1000.0, rotation: int = 300):
        with (
            patch("app.server.get_cfg", return_value=SimpleNamespace(refresh_interval=60, idle_module_rotation_seconds=rotation)),
            patch("app.server.get_settings_values", return_value={}),
            patch("app.server._get_effective_idle_modules", return_value=([object()], rotation)),
            patch("app.server._get_schedule_state", return_value=schedule_state),
            patch("app.server.time.time", return_value=now),
            patch.dict(server._last_ack, {"cycle_ms": 40000}, clear=True),
        ):
            return server._suggest_next_wake("garbage:x:3", "garbage")

    def test_idle_module_wakes_at_slot_boundary(self) -> None:
        seconds, reason = self._run({"active": False, "window": None, "seconds_until_end": 0, "seconds_until_change": 0, "next": None})
        self.assertEqual(seconds, 175)
        self.assertIn("nächstes Bild um", reason)

    def test_window_end_comes_first(self) -> None:
        night = Window("Nachts", ALL_DAYS, 23 * 60, 7 * 60, "", 900, ())
        seconds, reason = self._run({"active": True, "window": night, "seconds_until_end": 50, "seconds_until_change": 50,
                                     "label": night.label, "next": None}, rotation=900)
        self.assertEqual(seconds, 50)
        self.assertIn("endet um 07:00", reason)

    def test_window_uses_its_own_slot_grid(self) -> None:
        night = Window("Nachts", ALL_DAYS, 23 * 60, 7 * 60, "", 900, ())
        seconds, reason = self._run({"active": True, "window": night, "seconds_until_end": 5000, "seconds_until_change": 5000,
                                     "label": night.label, "next": None}, now=1000.0, rotation=900)
        # Grenze bei 1800: 1800 + 15 - 40 - 1000
        self.assertEqual(seconds, 775)
        self.assertIn("Nachts", reason)

    def test_upcoming_window_start_comes_first(self) -> None:
        morning = Window("Morgen", ALL_DAYS, 6 * 60, 9 * 60, "", 0, ())
        seconds, reason = self._run({"active": False, "window": None, "seconds_until_end": 0, "seconds_until_change": 30, "next": morning})
        self.assertEqual(seconds, 30)
        self.assertIn("beginnt um 06:00", reason)

    def test_placeholder_keeps_plain_rotation(self) -> None:
        with (
            patch("app.server.get_cfg", return_value=SimpleNamespace(refresh_interval=60, idle_module_rotation_seconds=180)),
            patch("app.server._get_schedule_state", return_value={"active": False, "window": None, "seconds_until_end": 0,
                                                                   "seconds_until_change": 0, "next": None}),
        ):
            seconds, _ = server._suggest_next_wake("__no_content__", "idle")
        self.assertEqual(seconds, 180)


class BackgroundPollTest(unittest.TestCase):
    def test_worker_wakes_at_the_slot_boundary(self) -> None:
        with (
            patch("app.server.get_settings_values", return_value={}),
            patch("app.server.get_cfg", return_value=SimpleNamespace(refresh_interval=60)),
            patch("app.server._registry.get_modules", return_value=[]),
            patch("app.server._get_schedule_state", return_value={"active": False, "seconds_until_end": 0, "seconds_until_change": 0}),
            patch("app.server._get_effective_idle_modules", return_value=([object()], 300)),
            patch("app.server._esp32_state", {"media_type": "garbage"}),
            patch("app.server.time.time", return_value=1470.0),
        ):
            self.assertEqual(server._get_background_poll_seconds(), 31, "30 s bis zur Grenze, plus eine Sekunde")
        with (
            patch("app.server.get_settings_values", return_value={}),
            patch("app.server.get_cfg", return_value=SimpleNamespace(refresh_interval=60)),
            patch("app.server._registry.get_modules", return_value=[]),
            patch("app.server._get_schedule_state", return_value={"active": False, "seconds_until_end": 0, "seconds_until_change": 0}),
            patch("app.server._get_effective_idle_modules", return_value=([object()], 300)),
            patch("app.server._esp32_state", {"media_type": "garbage"}),
            patch("app.server.time.time", return_value=1000.0),
        ):
            self.assertEqual(server._get_background_poll_seconds(), 60, "sonst der normale Poll")


if __name__ == "__main__":
    unittest.main()
