"""
0.7.0: Kalendertermine im freien Platz der Müllabfuhr-Seite (GARBAGE_CALENDAR).
"""

from __future__ import annotations

import unittest
from datetime import date, datetime, time, timedelta
from unittest.mock import patch
from zoneinfo import ZoneInfo

from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from modules.garbage import data_source as ds
from modules.garbage import module as garbage
from modules.garbage.renderer import render_garbage_module

BERLIN = ZoneInfo("Europe/Berlin")
TODAY = date(2026, 9, 26)


def _garbage_content(n_days: int = 2) -> dict:
    events = [{"date": TODAY + timedelta(days=2 + 3 * i), "summary": s, "label": ""}
              for i, s in enumerate(["Restmülltonne", "Gelbe Tonne", "Biotonne", "Papiertonne", "Restmülltonne", "Gelbe Tonne"][:n_days + 1])]
    return ds.build_garbage_content(events, TODAY, 21)


def _calendar_days(n: int) -> list[dict]:
    days = []
    for i in range(n):
        d = TODAY + timedelta(days=i)
        days.append({"date": d, "relative": "Heute" if i == 0 else "", "events": [
            {"summary": f"Termin {i}", "start": datetime.combine(d, time(9 + i % 8), tzinfo=BERLIN), "all_day": False, "color": "blue"},
        ]})
    return days


class _FakeCalendar:
    MODULE_ID = "calendar"

    def __init__(self, days, refresh=False):
        self._days, self._refresh = days, refresh

    def fetch_content(self, env):
        return {"days": [{"date": TODAY, "relative": "Heute", "events": []}] + self._days}

    def should_refresh(self, env):
        return self._refresh


class GarbageCalendarModuleTest(unittest.TestCase):
    def test_calendar_days_only_when_wanted(self) -> None:
        cal = _FakeCalendar(_calendar_days(3))
        with patch.object(ds, "fetch_garbage_content", return_value=_garbage_content()), \
             patch("app.module_registry.get_module_by_id", return_value=cal):
            off = garbage.fetch_content({"GARBAGE_CALENDAR": "off"})
            on = garbage.fetch_content({"GARBAGE_CALENDAR": "calendar"})
        self.assertNotIn("calendar", off)
        self.assertEqual([len(d["events"]) for d in on["calendar"]], [1, 1, 1], "leerer Heute-Tag fällt weg")

    def test_state_key_follows_the_calendar(self) -> None:
        base = _garbage_content()
        a = garbage.get_state_key({**base, "calendar": _calendar_days(2)})
        b = garbage.get_state_key({**base, "calendar": _calendar_days(3)})
        self.assertNotEqual(a, b)
        self.assertEqual(garbage.get_state_key(base), garbage.get_state_key({**base, "calendar": []}))

    def test_refresh_when_the_calendar_wants_one(self) -> None:
        with patch.object(ds, "should_refresh_garbage", return_value=False), \
             patch("app.module_registry.get_module_by_id", return_value=_FakeCalendar([], refresh=True)):
            self.assertTrue(garbage.should_refresh({"GARBAGE_CALENDAR": "calendar"}))
            self.assertFalse(garbage.should_refresh({"GARBAGE_CALENDAR": "off"}))

    def test_without_calendar_module_nothing_breaks(self) -> None:
        with patch.object(ds, "fetch_garbage_content", return_value=_garbage_content()), \
             patch("app.module_registry.get_module_by_id", return_value=None):
            self.assertEqual(garbage.fetch_content({"GARBAGE_CALENDAR": "calendar"})["calendar"], [])


class GarbageCalendarRenderTest(unittest.TestCase):
    def _render(self, content, theme="eink", w=1200, h=1600, compact=False):
        from app.config import load_font
        svc = ModuleRenderServices(render_width=w, render_height=h, display_theme=theme, load_font=load_font)
        return render_garbage_module(svc, content, compact=compact)

    def test_free_space_gets_the_calendar(self) -> None:
        base = _garbage_content()
        plain = self._render(base)
        with_cal = self._render({**base, "calendar": _calendar_days(4)})
        # Das untere Drittel war leer und ist es jetzt nicht mehr
        lower = (0, 1150, 1200, 1500)
        self.assertEqual(len(plain.crop(lower).getcolors(1200 * 350)), 1, "vorher: nur Hintergrund")
        self.assertGreater(len(with_cal.crop(lower).getcolors(1200 * 350)), 1)
        palette = set(SPECTRA6_COLORS.values())
        self.assertEqual(sum(n for n, c in with_cal.getcolors(1200 * 1600) if c not in palette), 0, "nur Panelfarben")

    def test_many_entries_end_with_a_more_line_and_fit(self) -> None:
        for theme in ("eink", "dark", "light"):
            for w, h in ((1200, 1600), (800, 480), (1600, 1200)):
                with self.subTest(theme=theme, size=(w, h)):
                    img = self._render({**_garbage_content(5), "calendar": _calendar_days(30)}, theme, w, h)
                    self.assertEqual(img.size, (w, h))

    def test_tile_ignores_the_calendar(self) -> None:
        base = _garbage_content()
        a = self._render(base, w=1200, h=500, compact=True)
        b = self._render({**base, "calendar": _calendar_days(4)}, w=1200, h=500, compact=True)
        self.assertEqual(a.tobytes(), b.tobytes())


if __name__ == "__main__":
    unittest.main()
