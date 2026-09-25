"""
Tests für das Kalender-Modul und den gemeinsamen ICS-Parser (app/ics.py):
Zeitzonen (TZID, UTC, Thunderbird, Exchange, Windows-Namen), ganztägig,
DURATION, Wiederholungsregeln (DAILY/WEEKLY/MONTHLY/YEARLY, INTERVAL, COUNT,
UNTIL, BYDAY auch „2TU“, EXDATE), verschobene und abgesagte Einzeltermine,
kaputte Serien, Inhaltsaufbau und Rendering.
"""

from __future__ import annotations

import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from PIL import Image

import app.config as config
import app.http_client as http_client
from app import ics
from app.module_services import ModuleRenderServices
from modules.calendar_ics import data_source as ds
from modules.calendar_ics import module as calendar
from modules.calendar_ics.renderer import render_calendar_module


def _ics(*events: str) -> str:
    body = "".join(f"BEGIN:VEVENT\r\n{e.strip()}\r\nEND:VEVENT\r\n" for e in events)
    return f"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:test\r\n{body}END:VCALENDAR\r\n"


def _local(y, m, d, hh=0, mm=0):
    return datetime(y, m, d, hh, mm, tzinfo=config.local_tz())


NOW = _local(2026, 9, 10, 9, 0)      # Donnerstag


def _occ(text: str, window_start=date(2026, 9, 10), window_end=date(2026, 9, 30)) -> list:
    return ics.occurrences(ics.parse_calendar(text), window_start, window_end)


class ParserTest(unittest.TestCase):
    def test_all_day_and_timed_with_tzid_and_utc(self) -> None:
        text = _ics(
            "UID:a\r\nDTSTART;VALUE=DATE:20260912\r\nDTEND;VALUE=DATE:20260913\r\nSUMMARY:Ganztag",
            "UID:b\r\nDTSTART;TZID=Europe/Berlin:20260912T093000\r\nDTEND;TZID=Europe/Berlin:20260912T103000\r\nSUMMARY:Lokal",
            "UID:c\r\nDTSTART:20260912T120000Z\r\nDURATION:PT45M\r\nSUMMARY:UTC mit Dauer\r\nLOCATION:Raum 1\\, Haus 2",
        )
        events = _occ(text, date(2026, 9, 12), date(2026, 9, 12))
        self.assertEqual(len(events), 3)
        a, b, c = events
        self.assertTrue(a["all_day"])
        self.assertEqual(a["start"], date(2026, 9, 12))
        self.assertFalse(b["all_day"])
        self.assertEqual(b["start"].hour, 9)
        self.assertEqual(b["end"] - b["start"], timedelta(hours=1))
        # 12:00 UTC ist im September 14:00 in Berlin
        self.assertEqual((c["start"].hour, c["start"].minute), (14, 0))
        self.assertEqual(c["end"] - c["start"], timedelta(minutes=45))
        self.assertEqual(c["location"], "Raum 1, Haus 2")

    def test_cancelled_and_untitled_events_are_dropped(self) -> None:
        text = _ics(
            "UID:x\r\nDTSTART;VALUE=DATE:20260912\r\nSUMMARY:Abgesagt\r\nSTATUS:CANCELLED",
            "UID:y\r\nDTSTART;VALUE=DATE:20260912",
            "UID:z\r\nDTSTART;VALUE=DATE:20260912\r\nSUMMARY:Bleibt",
        )
        self.assertEqual([e["summary"] for e in _occ(text)], ["Bleibt"])

    def test_sources_accept_webcal(self) -> None:
        self.assertEqual(ds.parse_sources("Privat|webcal://x.test/a.ics"), [("Privat", "https://x.test/a.ics")])

    def test_foreign_time_zone_names(self) -> None:
        text = _ics(
            # Thunderbird: TZID mit Präfix – brachte früher den ganzen Kalender zu Fall
            "UID:t\r\nDTSTART;TZID=/mozilla.org/20050126_1/Europe/Berlin:20260910T100000\r\nSUMMARY:Thunderbird",
            # Exchange: Doppelpunkt im TZID in Anführungszeichen – der Termin fiel früher still weg
            'UID:e\r\nDTSTART;TZID="(UTC+01:00) Amsterdam, Berlin, Bern, Rom, Stockholm, Wien":20260910T110000\r\nSUMMARY:Exchange',
            # Outlook: Windows-Name
            "UID:w\r\nDTSTART;TZID=W. Europe Standard Time:20260910T120000\r\nSUMMARY:Outlook",
        )
        found = {e["summary"]: e["start"] for e in _occ(text)}
        self.assertEqual(set(found), {"Thunderbird", "Exchange", "Outlook"})
        self.assertEqual([found[k].hour for k in ("Thunderbird", "Exchange", "Outlook")], [10, 11, 12])

    def test_broken_series_does_not_hide_the_calendar(self) -> None:
        text = _ics(
            "UID:k\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Kaputte Regel\r\nRRULE:FREQ=MANCHMAL",
            "UID:g\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Gut",
        )
        self.assertEqual([e["summary"] for e in _occ(text)], ["Gut"])

    def test_not_a_calendar(self) -> None:
        with self.assertRaises(ics.IcsError):
            ics.parse_calendar("<html><body>404 Not Found</body></html>")


class RecurrenceTest(unittest.TestCase):
    def _occ(self, event_text: str, window_start=date(2026, 9, 10), window_end=date(2026, 9, 30)) -> list:
        return _occ(_ics(event_text), window_start, window_end)

    def test_daily_with_count(self) -> None:
        occ = self._occ("UID:d\r\nDTSTART;VALUE=DATE:20260909\r\nSUMMARY:Täglich\r\nRRULE:FREQ=DAILY;COUNT=4")
        # 09., 10., 11., 12. – Fenster ab 10. → 3
        self.assertEqual([o["start"] for o in occ], [date(2026, 9, 10), date(2026, 9, 11), date(2026, 9, 12)])

    def test_weekly_byday_with_until_and_exdate(self) -> None:
        occ = self._occ(
            "UID:w\r\nDTSTART;TZID=Europe/Berlin:20260901T180000\r\nDTEND;TZID=Europe/Berlin:20260901T190000\r\n"
            "SUMMARY:Training\r\nRRULE:FREQ=WEEKLY;BYDAY=TU,TH;UNTIL=20260920T000000Z\r\n"
            "EXDATE;TZID=Europe/Berlin:20260915T180000"
        )
        dates = [o["start"].date() for o in occ]
        # Di/Do ab 10.09. bis 20.09.: 10., 15.(EXDATE), 17. → 10., 17.
        self.assertEqual(dates, [date(2026, 9, 10), date(2026, 9, 17)])
        self.assertTrue(all(o["start"].hour == 18 for o in occ))

    def test_weekly_interval_two(self) -> None:
        occ = self._occ("UID:w2\r\nDTSTART;VALUE=DATE:20260903\r\nSUMMARY:Alle 2 Wochen\r\nRRULE:FREQ=WEEKLY;INTERVAL=2")
        self.assertEqual([o["start"] for o in occ], [date(2026, 9, 17)])

    def test_monthly_and_yearly(self) -> None:
        monthly = self._occ("UID:m\r\nDTSTART;VALUE=DATE:20260115\r\nSUMMARY:Miete\r\nRRULE:FREQ=MONTHLY")
        self.assertEqual([o["start"] for o in monthly], [date(2026, 9, 15)])
        yearly = self._occ("UID:y\r\nDTSTART;VALUE=DATE:20200920\r\nSUMMARY:Geburtstag\r\nRRULE:FREQ=YEARLY")
        self.assertEqual([o["start"] for o in yearly], [date(2026, 9, 20)])

    def test_monthly_skips_invalid_days(self) -> None:
        occ = self._occ("UID:m31\r\nDTSTART;VALUE=DATE:20260131\r\nSUMMARY:31.\r\nRRULE:FREQ=MONTHLY",
                        window_start=date(2026, 9, 1), window_end=date(2026, 10, 31))
        self.assertEqual([o["start"] for o in occ], [date(2026, 10, 31)])

    def test_monthly_by_weekday_position(self) -> None:
        # „jeden 2. Dienstag“ – landete früher auf dem Tag von DTSTART (1. Oktober statt 13.)
        occ = self._occ("UID:2tu\r\nDTSTART;TZID=Europe/Berlin:20260113T190000\r\nSUMMARY:Stammtisch\r\nRRULE:FREQ=MONTHLY;BYDAY=2TU",
                        window_start=date(2026, 9, 1), window_end=date(2026, 10, 31))
        self.assertEqual([o["start"].date() for o in occ], [date(2026, 9, 8), date(2026, 10, 13)])

    def test_old_daily_series_continues(self) -> None:
        # Früher nach 500 Vorkommen ab DTSTART zu Ende – eine Serie von 2024 war 2026 weg
        occ = self._occ("UID:old\r\nDTSTART;VALUE=DATE:20240101\r\nSUMMARY:Tabletten\r\nRRULE:FREQ=DAILY",
                        window_start=date(2026, 9, 10), window_end=date(2026, 9, 11))
        self.assertEqual([o["start"] for o in occ], [date(2026, 9, 10), date(2026, 9, 11)])

    def test_moved_and_cancelled_instances(self) -> None:
        text = _ics(
            "UID:t\r\nDTSTART;TZID=Europe/Berlin:20260901T180000\r\nDTEND;TZID=Europe/Berlin:20260901T190000\r\n"
            "SUMMARY:Training\r\nRRULE:FREQ=WEEKLY;COUNT=10",
            "UID:t\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260908T180000\r\nDTSTART;TZID=Europe/Berlin:20260909T200000\r\n"
            "DTEND;TZID=Europe/Berlin:20260909T210000\r\nSUMMARY:Training (verschoben)",
            "UID:t\r\nRECURRENCE-ID;TZID=Europe/Berlin:20260915T180000\r\nDTSTART;TZID=Europe/Berlin:20260915T180000\r\n"
            "SUMMARY:Training\r\nSTATUS:CANCELLED",
        )
        found = [(o["start"].date(), o["start"].hour, o["summary"]) for o in _occ(text, date(2026, 9, 1), date(2026, 9, 23))]
        self.assertEqual(found, [
            (date(2026, 9, 1), 18, "Training"),
            (date(2026, 9, 9), 20, "Training (verschoben)"),     # nicht zusätzlich am 8.
            (date(2026, 9, 22), 18, "Training"),                  # 15. abgesagt
        ])

    def test_multi_day_all_day_event_touches_window(self) -> None:
        occ = self._occ("UID:u\r\nDTSTART;VALUE=DATE:20260908\r\nDTEND;VALUE=DATE:20260912\r\nSUMMARY:Urlaub")
        self.assertEqual(len(occ), 1)


class ContentBuildTest(unittest.TestCase):
    def _events(self):
        return ics.parse_calendar(_ics(
            "UID:1\r\nDTSTART;TZID=Europe/Berlin:20260910T080000\r\nDTEND;TZID=Europe/Berlin:20260910T083000\r\nSUMMARY:Vorbei",
            "UID:2\r\nDTSTART;TZID=Europe/Berlin:20260910T140000\r\nDTEND;TZID=Europe/Berlin:20260910T150000\r\nSUMMARY:Heute später",
            "UID:3\r\nDTSTART;VALUE=DATE:20260910\r\nSUMMARY:Ganztag heute",
            "UID:4\r\nDTSTART;VALUE=DATE:20260912\r\nDTEND;VALUE=DATE:20260914\r\nSUMMARY:Wochenende weg",
            "UID:5\r\nDTSTART;TZID=Europe/Berlin:20260925T100000\r\nSUMMARY:Außerhalb",
        ))

    def test_today_always_present_and_past_hidden(self) -> None:
        content = ds.build_calendar_content([("Privat", "blue", self._events())], NOW, 7, 14)
        self.assertEqual(content["today"], "2026-09-10")
        today = content["days"][0]
        self.assertEqual(today["in_days"], 0)
        titles = [e["summary"] for e in today["events"]]
        self.assertNotIn("Vorbei", titles)
        self.assertEqual(titles, ["Ganztag heute", "Heute später"], "ganztägig zuerst, dann nach Uhrzeit")
        weekend_days = [d for d in content["days"] if d["events"] and d["events"][0]["summary"] == "Wochenende weg"]
        self.assertEqual([d["date"] for d in weekend_days], [date(2026, 9, 12), date(2026, 9, 13)])
        self.assertTrue(weekend_days[1]["events"][0]["continues"])
        self.assertNotIn("Außerhalb", [e["summary"] for d in content["days"] for e in d["events"]])

    def test_show_past_today_when_configured(self) -> None:
        content = ds.build_calendar_content([("P", "blue", self._events())], NOW, 7, 14, hide_past_today=False)
        self.assertIn("Vorbei", [e["summary"] for e in content["days"][0]["events"]])

    def test_max_events_caps_and_reports_hidden(self) -> None:
        many = ics.parse_calendar(_ics(*[
            f"UID:{i}\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Termin {i}" for i in range(6)
        ]))
        content = ds.build_calendar_content([("P", "blue", many)], NOW, 7, 3)
        day = [d for d in content["days"] if d["in_days"] == 1][0]
        self.assertEqual(len(day["events"]), 3)
        self.assertEqual(day["hidden"], 3)


class LifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._cache_patch = patch.object(ds, "CACHE_FILE", Path(self._tmp.name) / "calendar_cache.json")
        self._cache_patch.start()
        ds.clear_cache()
        settings = dict(config.read_env_settings())
        settings.update({"IDLE_MODULES": "calendar",
                         "CALENDAR_ICS_URLS": "Familie|https://cal.test/a.ics; Arbeit|https://cal.test/b.ics"})
        config.apply_runtime_config(settings)
        self.env = config.get_settings_values()

    def tearDown(self) -> None:
        ds.clear_cache()
        self._cache_patch.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def test_fetch_merges_sources_with_colours_and_caches(self) -> None:
        calls: list[str] = []

        def fake_get(url, **kw):
            calls.append(url)
            text = _ics(f"UID:{url}\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Aus {url[-5:]}")
            return SimpleNamespace(status_code=200, text=text, raise_for_status=lambda: None)

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=fake_get), \
             patch.object(ds, "now_local", return_value=NOW):
            content = calendar.fetch_content(self.env)
            calendar.fetch_content(self.env)
        self.assertEqual(len(calls), 2, "jede Quelle einmal, dann Cache")
        self.assertEqual([s["label"] for s in content["sources"]], ["Familie", "Arbeit"])
        colours = {e["color"] for d in content["days"] for e in d["events"]}
        self.assertEqual(colours, {"blue", "green"})
        self.assertTrue(calendar.get_state_key(content).startswith("2026-09-10:"))

    def test_is_enabled_and_validation(self) -> None:
        self.assertTrue(calendar.is_enabled(self.env))
        self.assertFalse(calendar.is_enabled({**self.env, "CALENDAR_ICS_URLS": ""}))
        errors = calendar.validate_settings({}, {**self.env, "CALENDAR_ICS_URLS": ""})
        self.assertTrue(any("ICS-Adresse" in e for e in errors))
        self.assertEqual(calendar.validate_settings({}, self.env), [])


class ResilienceAndProbeTest(LifecycleTest):
    """Platten-Cache, Fehler je Quelle, Zusammenfassung und Prüfen-Details."""

    @staticmethod
    def _ok_get(url, **kw):
        text = _ics(
            f"UID:{url}-1\r\nDTSTART;TZID=Europe/Berlin:20260910T140000\r\nDTEND;TZID=Europe/Berlin:20260910T150000\r\nSUMMARY:Zahnarzt {url[-5:]}",
            f"UID:{url}-2\r\nDTSTART;VALUE=DATE:20260912\r\nSUMMARY:Ausflug {url[-5:]}",
        )
        return SimpleNamespace(status_code=200, text=text, raise_for_status=lambda: None)

    @staticmethod
    def _failing_get(url, **kw):
        if url.endswith("b.ics"):
            raise RuntimeError("Verbindung abgelehnt")
        return ResilienceAndProbeTest._ok_get(url)

    def test_last_good_state_survives_restart_and_is_marked_stale(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._ok_get), \
             patch.object(ds, "now_local", return_value=NOW):
            content = calendar.fetch_content(self.env)
        self.assertEqual(content["stale_since"], "")
        self.assertEqual([s["count"] for s in content["sources"]], [2, 2])
        cache_file = ds.CACHE_FILE
        self.assertTrue(cache_file.exists(), "Stand wird auf Platte gesichert")
        raw = json.loads(cache_file.read_text(encoding="utf-8"))
        for entry in raw.values():
            entry["fetched_at"] = time.time() - 3 * ds.DEFAULT_CACHE_SECONDS
        cache_file.write_text(json.dumps(raw), encoding="utf-8")
        ds.clear_cache()      # „Neustart“

        def offline(url, **kw):
            raise RuntimeError("Kalender-Server offline")

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=offline), \
             patch.object(ds, "now_local", return_value=NOW):
            content = calendar.fetch_content(self.env)
        self.assertIsNotNone(content, "alter Stand statt leerer Seite")
        self.assertTrue(content["stale_since"], "Stand vom … wird gemeldet")
        self.assertEqual([e["label"] for e in content["source_errors"]], ["Familie", "Arbeit"])
        titles = [e["summary"] for d in content["days"] for e in d["events"]]
        self.assertIn("Zahnarzt a.ics", titles)
        img = render_calendar_module(ModuleRenderServices(render_width=600, render_height=800, display_theme="eink",
                                                          load_font=config.load_font), content)
        self.assertEqual(img.size, (600, 800))

    def test_status_reports_error_only_when_nothing_was_ever_loaded(self) -> None:
        def offline(url, **kw):
            raise RuntimeError("DNS kaputt")

        self.assertEqual(calendar.describe_status(self.env)["state"], "ready", "vor dem ersten Abruf: bereit")
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=offline), \
             patch.object(ds, "now_local", return_value=NOW):
            self.assertIsNone(calendar.fetch_content(self.env))
        status = calendar.describe_status(self.env)
        self.assertEqual(status["state"], "error")
        self.assertIn("DNS kaputt", status["reason"])
        # Prüfen lädt neu – ebenfalls ohne echtes Netz (sonst DNS-Anfrage an cal.test)
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=offline), \
             patch.object(ds, "now_local", return_value=NOW):
            probe = calendar.probe(self.env)
        self.assertFalse(probe["ok"])
        self.assertTrue(any("Familie: Fehler" in d for d in probe["details"]))

    def test_probe_details_and_summary_with_one_failing_source(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._failing_get), \
             patch.object(ds, "now_local", return_value=NOW):
            probe = calendar.probe(self.env)
            summary = calendar.summarize(self.env)
        self.assertTrue(probe["ok"], "eine Quelle reicht")
        self.assertIn("Arbeit nicht erreichbar", probe["message"])
        details = probe["details"]
        self.assertEqual(details[0], "Quellen:")
        self.assertIn("Familie: 2 Termine in den nächsten 7 Tagen", details)
        self.assertTrue(any(d.startswith("Arbeit: Fehler – Verbindung abgelehnt") for d in details))
        self.assertIn("Nächste Termine:", details)
        self.assertTrue(any(d.startswith("heute 14:00 · Zahnarzt a.ics (Familie)") for d in details), details)
        self.assertTrue(any("Sa 12.09. ganztägig · Ausflug" in d for d in details), details)
        self.assertIn("Familie, Arbeit", summary)
        self.assertIn("nächster: heute 14:00 Zahnarzt a.ics", summary)
        self.assertIn("Arbeit nicht erreichbar", summary)


class RenderTest(unittest.TestCase):
    def _content(self) -> dict:
        events = _occ(_ics(
            "UID:1\r\nDTSTART;TZID=Europe/Berlin:20260910T140000\r\nDTEND;TZID=Europe/Berlin:20260910T150000\r\nSUMMARY:Zahnarzt\r\nLOCATION:Praxis Dr. Müller",
            "UID:2\r\nDTSTART;VALUE=DATE:20260910\r\nSUMMARY:Geburtstag Oma",
            "UID:3\r\nDTSTART;TZID=Europe/Berlin:20260911T093000\r\nDTEND;TZID=Europe/Berlin:20260911T113000\r\nSUMMARY:Teammeeting mit einem sehr langen Titel der gekürzt werden muss",
            "UID:4\r\nDTSTART;VALUE=DATE:20260912\r\nDTEND;VALUE=DATE:20260914\r\nSUMMARY:Wochenende in Hamburg",
        ))
        content = ds.build_calendar_content([("Familie", "blue", events[:2]), ("Arbeit", "green", events[2:])], NOW, 7, 14)
        content["sources"] = [{"label": "Familie", "color": "blue"}, {"label": "Arbeit", "color": "green"}]
        return content

    def test_renders_in_all_themes_and_sizes(self) -> None:
        content = self._content()
        for theme in config.AVAILABLE_THEMES:
            for (w, h) in ((1200, 1600), (1600, 1200), (600, 800)):
                services = ModuleRenderServices(render_width=w, render_height=h, display_theme=theme, load_font=config.load_font)
                img = render_calendar_module(services, content)
                self.assertIsInstance(img, Image.Image)
                self.assertEqual(img.size, (w, h))

    def test_renders_empty(self) -> None:
        services = ModuleRenderServices(render_width=800, render_height=600, display_theme="eink", load_font=config.load_font)
        self.assertEqual(render_calendar_module(services, {"days": [], "sources": []}).size, (800, 600))


if __name__ == "__main__":
    unittest.main()
