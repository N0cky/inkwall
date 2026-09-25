"""
Tests für das Müllabfuhr-Modul: ICS-Parser, Quellen-Parsing, {year}-Platzhalter,
Farbzuordnung, Inhaltsaufbau relativ zu einem festen "heute", Rendering in
allen drei Themes und der Modul-Lifecycle.
"""

from __future__ import annotations

from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import json
import tempfile
import time
import unittest

from PIL import Image

import app.config as config
import app.http_client as http_client
from modules.garbage import data_source as ds
from modules.garbage import module as garbage
from modules.garbage.renderer import render_garbage_module
from app.module_services import ModuleRenderServices

FIXTURE = Path(__file__).resolve().parent / "fixtures" / "garbage_wetzlar.ics"
ICS_TEXT = FIXTURE.read_text(encoding="utf-8")
# Das Fixture enthält jedes dritte Event des Jahres; ab dem 10.09. liegt
# der nächste Termin (21.09.) im 14-Tage-Fenster.
FIXED_TODAY = date(2026, 9, 10)


def _fixed_now(*_a, **_k):
    return datetime(2026, 9, 10, 8, 0, tzinfo=config.local_tz())


class IcsParserTest(unittest.TestCase):
    def test_parses_all_events_with_dates_and_summaries(self) -> None:
        events = ds.parse_ics_events(ICS_TEXT, around=FIXED_TODAY)
        self.assertEqual(len(events), 31)
        self.assertTrue(all(isinstance(e["date"], date) for e in events))
        self.assertEqual({e["summary"] for e in events},
                         {"Altpapiertonne", "Biotonne", "Gelbe Tonne", "Restmülltonne"})
        self.assertEqual(events[0]["date"], date(2026, 1, 12))

    def test_handles_date_variants_and_folding(self) -> None:
        text = (
            "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\n"
            "DTSTART;VALUE=DATE:20260105\r\n"
            "SUMMARY:Gelbe\r\n  Tonne lang\r\n"          # gefaltete Zeile
            "END:VEVENT\r\nBEGIN:VEVENT\r\n"
            "DTSTART;TZID=Europe/Berlin:20260106T060000\r\n"
            "SUMMARY:Papier\\, Pappe\r\n"
            "END:VEVENT\r\nBEGIN:VEVENT\r\n"
            "SUMMARY:ohne Datum\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        )
        events = ds.parse_ics_events(text, around=FIXED_TODAY)
        self.assertEqual([(e["date"], e["summary"]) for e in events],
                         [(date(2026, 1, 5), "Gelbe Tonne lang"), (date(2026, 1, 6), "Papier, Pappe")])

    def test_utc_midnight_lands_on_the_right_day(self) -> None:
        # 22:00 UTC = 00:00 Uhr in Berlin – früher las der Parser nur die Ziffern und landete am Vortag
        text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:u\r\nDTSTART:20261014T220000Z\r\n"
                "DTEND:20261015T220000Z\r\nSUMMARY:Biotonne\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        self.assertEqual([e["date"] for e in ds.parse_ics_events(text, around=FIXED_TODAY)], [date(2026, 10, 15)])

    def test_repeating_collection_and_cancelled_date(self) -> None:
        # Manche Kommunen schreiben „alle zwei Wochen“ als Regel statt als Einzeltermine
        text = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:gelb\r\nDTSTART;VALUE=DATE:20260902\r\n"
                "SUMMARY:Gelbe Tonne\r\nRRULE:FREQ=WEEKLY;INTERVAL=2;COUNT=4\r\nEND:VEVENT\r\n"
                "BEGIN:VEVENT\r\nUID:bio\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Biotonne\r\n"
                "STATUS:CANCELLED\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n")
        events = ds.parse_ics_events(text, around=FIXED_TODAY)
        self.assertEqual([(e["date"], e["summary"]) for e in events], [
            (date(2026, 9, 2), "Gelbe Tonne"), (date(2026, 9, 16), "Gelbe Tonne"),
            (date(2026, 9, 30), "Gelbe Tonne"), (date(2026, 10, 14), "Gelbe Tonne"),
        ])

    def test_html_error_page_is_not_a_calendar(self) -> None:
        from app import ics
        with self.assertRaises(ics.IcsError):
            ds.parse_ics_events("<!DOCTYPE html><html><body>Wartungsarbeiten</body></html>")


class SettingsParsingTest(unittest.TestCase):
    def test_sources_with_labels_and_separators(self) -> None:
        raw = "Zuhause|https://a.test/x.ics; https://b.test/y.ics\nkaputt|ftp://nope"
        self.assertEqual(ds.parse_sources(raw),
                         [("Zuhause", "https://a.test/x.ics"), ("", "https://b.test/y.ics")])

    def test_year_placeholder(self) -> None:
        self.assertEqual(ds.expand_year("https://x/termine-{year}.php?m=1", 2027), "https://x/termine-2027.php?m=1")

    def test_type_classification_with_overrides(self) -> None:
        self.assertEqual(ds.classify_type("Restmülltonne"), "black")
        self.assertEqual(ds.classify_type("Biotonne"), "green")
        self.assertEqual(ds.classify_type("Gelbe Tonne"), "yellow")
        self.assertEqual(ds.classify_type("Altpapiertonne"), "blue")
        self.assertEqual(ds.classify_type("Sperrmüll"), "red")
        self.assertEqual(ds.classify_type("Irgendwas"), "black")
        overrides = ds.parse_type_overrides("bio=blue, papier=red, unsinn=lila")
        self.assertEqual(overrides, [("bio", "blue"), ("papier", "red")])
        self.assertEqual(ds.classify_type("Biotonne", overrides), "blue")


class ContentBuildTest(unittest.TestCase):
    def test_groups_upcoming_days_relative_to_today(self) -> None:
        events = [{**e, "label": ""} for e in ds.parse_ics_events(ICS_TEXT, around=FIXED_TODAY)]
        content = ds.build_garbage_content(events, FIXED_TODAY, 14)
        self.assertIsNotNone(content)
        self.assertEqual(content["today"], "2026-09-10")
        self.assertTrue(content["days"], "im 14-Tage-Fenster müssen Termine liegen")
        for day in content["days"]:
            self.assertGreaterEqual(day["in_days"], 0)
            self.assertLessEqual(day["in_days"], 14)
        self.assertEqual(content["next"]["date"], content["days"][0]["date"])
        self.assertFalse(content["next_outside_window"])

    def test_next_event_beyond_window_is_still_reported(self) -> None:
        events = [{"date": date(2026, 10, 20), "summary": "Biotonne", "label": ""}]
        content = ds.build_garbage_content(events, FIXED_TODAY, 14)
        self.assertEqual(content["days"], [])
        self.assertTrue(content["next_outside_window"])
        self.assertEqual(content["next"]["relative"], "In 40 Tagen")

    def test_relative_labels(self) -> None:
        self.assertEqual(ds.relative_day_label(0), "Heute")
        self.assertEqual(ds.relative_day_label(1), "Morgen")
        self.assertEqual(ds.relative_day_label(2), "Übermorgen")
        self.assertEqual(ds.relative_day_label(5), "In 5 Tagen")

    def test_no_future_events_returns_none(self) -> None:
        self.assertIsNone(ds.build_garbage_content([{"date": date(2020, 1, 1), "summary": "x"}], FIXED_TODAY, 14))


class FetchAndLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        ds.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_patch = patch.object(ds, "CACHE_FILE", Path(self._tmp.name) / "garbage_cache.json")
        self._cache_patch.start()
        self.settings = dict(config.read_env_settings())
        self.settings.update({
            "IDLE_MODULES": "garbage",
            "GARBAGE_ICS_URLS": "Zuhause|https://kommune.test/abfuhr-{year}.ics",
            "GARBAGE_DAYS_AHEAD": "14",
        })
        config.apply_runtime_config(self.settings)
        self.env = config.get_settings_values()

    def tearDown(self) -> None:
        ds.clear_cache()
        self._cache_patch.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def _fake_get(self, url, timeout=None, headers=None, **kw):
        self.requested.append(url)
        return SimpleNamespace(status_code=200, text=ICS_TEXT, raise_for_status=lambda: None)

    def test_fetch_substitutes_year_and_caches(self) -> None:
        self.requested: list[str] = []
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._fake_get), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            content = garbage.fetch_content(self.env)
            garbage.fetch_content(self.env)
        self.assertEqual(self.requested, ["https://kommune.test/abfuhr-2026.ics"], "einmal laden, dann Cache")
        self.assertIsNotNone(content)
        self.assertEqual(content["next"]["events"][0]["label"], "Zuhause")
        self.assertFalse(garbage.should_refresh(self.env))

    def test_fetch_failure_uses_backoff_and_returns_none_without_cache(self) -> None:
        calls = {"n": 0}

        def failing(url, **kw):
            calls["n"] += 1
            raise RuntimeError("down")

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=failing), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            self.assertIsNone(garbage.fetch_content(self.env))
            self.assertIsNone(garbage.fetch_content(self.env))
        self.assertEqual(calls["n"], 1, "innerhalb des Backoffs kein zweiter Versuch")

    def test_is_enabled_requires_idle_membership_and_url(self) -> None:
        self.assertTrue(garbage.is_enabled(self.env))
        self.assertFalse(garbage.is_enabled({**self.env, "IDLE_MODULES": "tagesschau"}))
        self.assertFalse(garbage.is_enabled({**self.env, "GARBAGE_ICS_URLS": ""}))

    def test_state_key_changes_with_day(self) -> None:
        base = {"today": "2026-09-03", "next": {"date": date(2026, 9, 7), "events": [{"summary": "Biotonne"}]}}
        k1 = garbage.get_state_key(base)
        k2 = garbage.get_state_key({**base, "today": "2026-09-04"})
        self.assertNotEqual(k1, k2)

    def test_validation_messages(self) -> None:
        env = {**self.env, "GARBAGE_ICS_URLS": "", "IDLE_MODULES": "garbage"}
        self.assertTrue(any("ICS-Adresse" in e for e in garbage.validate_settings({}, env)))
        env = {**self.env, "GARBAGE_ICS_URLS": "keine-url"}
        self.assertTrue(any("gültige" in e for e in garbage.validate_settings({}, env)))
        env = {**self.env, "GARBAGE_TYPE_COLORS": "bio=lila"}
        self.assertTrue(any("Tonnenfarben" in e for e in garbage.validate_settings({}, env)))
        self.assertEqual(garbage.validate_settings({}, self.env), [])


class RenderTest(unittest.TestCase):
    def _content(self) -> dict:
        events = [{**e, "label": "Zuhause"} for e in ds.parse_ics_events(ICS_TEXT, around=FIXED_TODAY)]
        return ds.build_garbage_content(events, FIXED_TODAY, 14)

    def test_renders_in_all_themes_and_sizes(self) -> None:
        content = self._content()
        for theme in config.AVAILABLE_THEMES:
            for (w, h) in ((1200, 1600), (1600, 1200), (600, 800)):
                services = ModuleRenderServices(render_width=w, render_height=h, display_theme=theme, load_font=config.load_font)
                img = render_garbage_module(services, content)
                self.assertIsInstance(img, Image.Image)
                self.assertEqual(img.size, (w, h))

    def test_renders_empty_window_and_no_content(self) -> None:
        services = ModuleRenderServices(render_width=800, render_height=600, display_theme="eink", load_font=config.load_font)
        far = ds.build_garbage_content([{"date": date(2026, 12, 20), "summary": "Biotonne", "label": ""}], FIXED_TODAY, 14)
        self.assertEqual(render_garbage_module(services, far).size, (800, 600))
        self.assertEqual(render_garbage_module(services, {}).size, (800, 600))

    def test_eink_render_is_mostly_on_palette(self) -> None:
        from app.image_rendering import SPECTRA6_COLORS
        services = ModuleRenderServices(render_width=1200, render_height=1600, display_theme="eink", load_font=config.load_font)
        img = render_garbage_module(services, self._content()).convert("RGB")
        palette = set(SPECTRA6_COLORS.values())
        total = img.width * img.height
        exact = sum(c for c, col in (img.getcolors(total) or []) if col in palette)
        self.assertGreater(exact / total, 0.9)


if __name__ == "__main__":
    unittest.main()


def _series(summary: str, label: str, weekday_dates: list[date]) -> list[dict]:
    return [{"date": d, "summary": summary, "label": label} for d in weekday_dates]


class ReminderAndShiftTest(unittest.TestCase):
    """Erledigt-Uhrzeit, Erinnerung am Vorabend, verschobene Termine, Spalten je Adresse."""

    MONDAYS = [date(2026, 8, 3), date(2026, 8, 17), date(2026, 8, 31), date(2026, 9, 14), date(2026, 9, 28), date(2026, 10, 26)]

    def test_today_is_skipped_once_done(self) -> None:
        events = _series("Biotonne", "", [date(2026, 9, 10), date(2026, 9, 24)])
        before = ds.build_garbage_content(events, date(2026, 9, 10), 14, today_done=False)
        after = ds.build_garbage_content(events, date(2026, 9, 10), 14, today_done=True)
        self.assertEqual(before["next"]["relative"], "Heute")
        self.assertTrue(before["urgent"])
        self.assertEqual(after["next"]["date"], date(2026, 9, 24))
        self.assertFalse(after["urgent"])

    def test_tomorrow_is_urgent_only_in_the_evening(self) -> None:
        events = _series("Restmüll", "", [date(2026, 9, 11)])
        day = ds.build_garbage_content(events, date(2026, 9, 10), 14, reminder_active=False)
        evening = ds.build_garbage_content(events, date(2026, 9, 10), 14, reminder_active=True)
        self.assertFalse(day["urgent"])
        self.assertTrue(evening["urgent"])
        self.assertEqual(evening["reminder"], "Morgen rausstellen")

    def test_shifted_date_is_marked_with_usual_weekday(self) -> None:
        # Vier Montage plus ein Dienstag (Feiertagsverschiebung)
        events = _series("Biotonne", "Zuhause", self.MONDAYS + [date(2026, 10, 13)])
        content = ds.build_garbage_content(events, date(2026, 10, 1), 30)
        shifted = [ev for d in content["days"] for ev in d["events"] if ev["shifted_from"]]
        self.assertEqual(len(shifted), 1)
        self.assertEqual(shifted[0]["date"], date(2026, 10, 13))
        self.assertEqual(shifted[0]["shifted_from"], "Montag")
        regular = [ev for d in content["days"] for ev in d["events"] if not ev["shifted_from"]]
        self.assertTrue(regular, "die Montage bleiben ohne Hinweis")

    def test_short_series_gets_no_shift_hint(self) -> None:
        events = _series("Sperrmüll", "", [date(2026, 9, 14), date(2026, 9, 15)])
        content = ds.build_garbage_content(events, date(2026, 9, 10), 14)
        self.assertTrue(all(not ev["shifted_from"] for d in content["days"] for ev in d["events"]))

    def test_by_label_and_kinds(self) -> None:
        events = (_series("Biotonne", "Hohe Straße", [date(2026, 9, 14)])
                  + _series("Papiertonne", "Zwirleinstraße", [date(2026, 9, 12)])
                  + _series("Gelber Sack", "Hohe Straße", [date(2026, 9, 20)]))
        content = ds.build_garbage_content(events, date(2026, 9, 10), 14)
        self.assertEqual([c["label"] for c in content["by_label"]], ["Hohe Straße", "Zwirleinstraße"])
        self.assertEqual(content["by_label"][0]["next"]["date"], date(2026, 9, 14))
        self.assertEqual(content["by_label"][1]["next"]["date"], date(2026, 9, 12))
        kinds = {k["summary"]: (k["color"], k["icon"]) for k in content["kinds"]}
        self.assertEqual(kinds["Gelber Sack"], ("yellow", "sack"))
        self.assertEqual(kinds["Papiertonne"], ("blue", "bin"), "steht 'Tonne' drin, ist es eine Tonne")
        self.assertEqual(kinds["Biotonne"], ("green", "bin"))
        single = ds.build_garbage_content(_series("Biotonne", "", [date(2026, 9, 14)]), date(2026, 9, 10), 14)
        self.assertEqual(single["by_label"], [], "eine Adresse → keine Spalten")

    def test_icon_classification(self) -> None:
        self.assertEqual(ds.classify_icon("Sperrmüll auf Abruf"), "bulky")
        self.assertEqual(ds.classify_icon("Weihnachtsbaumabfuhr"), "tree")
        self.assertEqual(ds.classify_icon("Altpapier"), "paper")
        self.assertEqual(ds.classify_icon("Altpapiertonne"), "bin")
        self.assertEqual(ds.classify_icon("Gelber Sack"), "sack")
        self.assertEqual(ds.classify_icon("Restmüll"), "bin")


class DiskCacheAndMissingYearTest(FetchAndLifecycleTest):
    def test_last_good_state_survives_restart_and_is_marked_stale(self) -> None:
        self.requested: list[str] = []
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._fake_get), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            self.assertIsNotNone(garbage.fetch_content(self.env))
        cache_file = ds.CACHE_FILE
        self.assertTrue(cache_file.exists(), "Stand wird auf Platte gesichert")
        # Datei altern lassen (drei Cache-Perioden) und Prozess "neu starten"
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        for entry in raw.values():
            entry["fetched_at"] = time.time() - 3 * ds.DEFAULT_CACHE_SECONDS
        cache_file.write_text(json.dumps(raw), encoding="utf-8")
        with ds._LOCK:
            ds._CACHE.clear()
            ds._DISK_LOADED = False

        def failing(url, **kw):
            raise RuntimeError("Kommune offline")

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=failing), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            content = garbage.fetch_content(self.env)
        self.assertIsNotNone(content, "alter Stand statt leerer Kachel")
        self.assertTrue(content["stale_since"], "Stand vom … wird gemeldet")
        self.assertEqual(content["next"]["events"][0]["label"], "Zuhause")

    def test_404_reports_missing_year_instead_of_nothing(self) -> None:
        def not_found(url, **kw):
            return SimpleNamespace(status_code=404, text="", raise_for_status=lambda: None)

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=not_found), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            content = garbage.fetch_content(self.env)
            result = garbage.probe(self.env)
        self.assertIsNotNone(content)
        self.assertEqual(content["missing_years"], [2026])
        self.assertIsNone(content["next"])
        self.assertFalse(result["ok"])
        self.assertTrue(any("404" in line for line in result["details"]))

    def test_probe_lists_next_dates_and_detected_kinds(self) -> None:
        self.requested = []
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._fake_get), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            result = garbage.probe(self.env)
        self.assertTrue(result["ok"], result)
        self.assertIn("Nächste Termine:", result["details"])
        self.assertIn("Erkannte Tonnen:", result["details"])
        self.assertTrue(any("→" in line for line in result["details"]))

    def test_is_urgent_follows_content(self) -> None:
        self.requested = []
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._fake_get), \
             patch.object(ds, "now_local", side_effect=_fixed_now):
            self.assertFalse(garbage.is_urgent(self.env), "am Vormittag ohne Termin morgen nicht dringend")

        def evening_before(*_a, **_k):
            return datetime(2026, 9, 20, 19, 0, tzinfo=config.local_tz())   # 21.09. ist Abfuhr im Fixture

        ds.clear_cache()
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._fake_get), \
             patch.object(ds, "now_local", side_effect=evening_before):
            self.assertTrue(garbage.is_urgent(self.env))
            key = garbage.get_state_key(garbage.fetch_content(self.env))
        self.assertIn("urgent", key)


class GarbageRendererTests(unittest.TestCase):
    def test_same_bin_at_several_addresses_becomes_one_line(self) -> None:
        from modules.garbage.renderer import _group_events
        groups = _group_events([
            {"summary": "Biotonne", "label": "Hohe Straße", "color": "green"},
            {"summary": "Biotonne", "label": "Zwirleinstraße", "color": "green"},
            {"summary": "Papiertonne", "label": "Hohe Straße", "color": "blue"},
        ])
        self.assertEqual([g["summary"] for g in groups], ["Biotonne", "Papiertonne"])
        self.assertEqual(groups[0]["labels"], ["Hohe Straße", "Zwirleinstraße"])

    def test_tile_renders_at_small_heights(self) -> None:
        from datetime import date
        from app.config import override_runtime_config
        from app.module_services import ModuleRenderServices
        from modules.garbage.data_source import build_garbage_content
        from modules.garbage.renderer import render_garbage_module
        events = [
            {"date": date(2026, 9, 7), "summary": "Biotonne", "label": "Hohe Straße"},
            {"date": date(2026, 9, 7), "summary": "Biotonne", "label": "Zwirleinstraße"},
            {"date": date(2026, 9, 7), "summary": "Papiertonne", "label": "Hohe Straße"},
            {"date": date(2026, 9, 10), "summary": "Restmüll", "label": "Hohe Straße"},
        ]
        content = build_garbage_content(events, date(2026, 9, 3), 14)
        with override_runtime_config(display_theme="eink"):
            base = ModuleRenderServices.from_runtime()
            for height in (220, 300, 455, 760):
                services = ModuleRenderServices(render_width=1200, render_height=height,
                                                display_theme=base.display_theme, load_font=base.load_font)
                img = render_garbage_module(services, content, compact=True)
                self.assertEqual(img.size, (1200, height))

