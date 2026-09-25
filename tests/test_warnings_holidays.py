"""
0.4.0 „Warnungen und Termine“: Feiertage und Ferien (app/holidays.py) im
Kalender, in der Müllabfuhr und im Zeitplan; Unwetter als dringend; das
NINA-Modul; die Benachrichtigung „Warnungen“.
"""

from __future__ import annotations

import base64
import json
import tempfile
import time
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import app.config as config
import app.device as device
import app.holidays as holidays
import app.http_client as http_client
import app.notifications as nf
from app import schedule

BERLIN = ZoneInfo("Europe/Berlin")
PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")

PUBLIC_2026 = [
    {"start": "2026-05-25", "end": "2026-05-25", "name": "Pfingstmontag"},
    {"start": "2026-10-03", "end": "2026-10-03", "name": "Tag der Deutschen Einheit"},
]
SCHOOL_2026 = [
    {"start": "2025-12-22", "end": "2026-01-10", "name": "Weihnachtsferien"},
    {"start": "2026-10-05", "end": "2026-10-17", "name": "Herbstferien"},
]


def _fake_fetch(region, kind, year):
    if year != 2026:
        return []
    return [dict(i) for i in (PUBLIC_2026 if kind == holidays.PUBLIC else SCHOOL_2026)]


class _HolidayBase(unittest.TestCase):
    """Feiertage aus festen Daten statt aus dem Netz; Bundesland Hessen."""

    def setUp(self) -> None:
        holidays.clear_cache()
        self._fetch = patch.object(holidays, "_fetch", side_effect=_fake_fetch)
        self._fetch.start()
        self._tmp = tempfile.TemporaryDirectory()
        self._file = patch.object(holidays, "CACHE_FILE", Path(self._tmp.name) / "holidays.json")
        self._file.start()
        config.apply_runtime_config({**config.read_env_settings(), "HOLIDAY_REGION": "DE-HE"})

    def tearDown(self) -> None:
        self._fetch.stop()
        self._file.stop()
        self._tmp.cleanup()
        holidays.clear_cache()
        config.apply_runtime_config()


# ---------------------------------------------------------------------------
# app/holidays.py
# ---------------------------------------------------------------------------

class HolidaysTest(_HolidayBase):
    def test_parse_keeps_statewide_days_only(self) -> None:
        data = [
            {"startDate": "2026-06-04", "endDate": "2026-06-04", "name": [{"language": "DE", "text": "Fronleichnam"}],
             "regionalScope": "Regional", "nationwide": False, "subdivisions": [{"code": "DE-HE"}, {"code": "DE-BY"}]},
            {"startDate": "2026-08-08", "endDate": "2026-08-08", "name": [{"language": "DE", "text": "Friedensfest"}],
             "regionalScope": "Local", "nationwide": False, "subdivisions": [{"code": "DE-BY-AU"}]},
            {"startDate": "2026-11-01", "endDate": "2026-11-01", "name": [{"language": "DE", "text": "Allerheiligen"}],
             "regionalScope": "Regional", "nationwide": False, "subdivisions": [{"code": "DE-NW"}]},
            {"startDate": "2026-10-03", "endDate": "2026-10-03", "name": [{"language": "EN", "text": "Unity"}, {"language": "DE", "text": "Tag der Deutschen Einheit"}],
             "regionalScope": "National", "nationwide": True},
        ]
        names = [i["name"] for i in holidays.parse_items(data, "DE-HE")]
        self.assertEqual(names, ["Fronleichnam", "Tag der Deutschen Einheit"])

    def test_between_and_lookups(self) -> None:
        found = holidays.between(holidays.SCHOOL, date(2026, 10, 1), date(2026, 10, 10))
        self.assertEqual([(h["name"], h["start"], h["end"]) for h in found], [("Herbstferien", date(2026, 10, 5), date(2026, 10, 17))])
        self.assertEqual(holidays.school_holiday_on(date(2026, 10, 12))["name"], "Herbstferien")
        self.assertIsNone(holidays.school_holiday_on(date(2026, 10, 18)))
        self.assertEqual(holidays.public_holiday_on(date(2026, 10, 3))["name"], "Tag der Deutschen Einheit")
        # Einmal geladen je Jahr und Art, danach aus dem Speicher
        holidays.between(holidays.SCHOOL, date(2026, 1, 1), date(2026, 12, 31))
        self.assertEqual(sum(1 for c in holidays._fetch.call_args_list if c.args[1] == holidays.SCHOOL), 1)

    def test_no_region_means_nothing(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(), "HOLIDAY_REGION": ""})
        self.assertEqual(holidays.between(holidays.PUBLIC, date(2026, 1, 1), date(2026, 12, 31)), [])
        self.assertIsNone(holidays.school_holiday_checker())

    def test_cache_only_does_not_fetch(self) -> None:
        with http_client.cache_only():
            self.assertEqual(holidays.between(holidays.PUBLIC, date(2026, 1, 1), date(2026, 12, 31)), [])
        holidays._fetch.assert_not_called()

    def test_shift_reason_is_the_holiday_of_that_week(self) -> None:
        pub = holidays.between(holidays.PUBLIC, date(2026, 5, 1), date(2026, 10, 31))
        self.assertEqual(holidays.shift_reason(date(2026, 5, 26), pub), "Pfingstmontag")
        self.assertEqual(holidays.shift_reason(date(2026, 6, 2), pub), "", "andere Woche")


# ---------------------------------------------------------------------------
# Zeitplan: nur in den Ferien / nur außerhalb
# ---------------------------------------------------------------------------

class ScheduleSchoolTest(unittest.TestCase):
    def _holiday(self, day: date) -> bool:
        return date(2026, 10, 5) <= day <= date(2026, 10, 17)

    def test_roundtrip_keeps_old_format_without_school(self) -> None:
        raw = "Schulweg|Mo-Fr|07:00-08:00|||departures|schule; Morgens|*|06:00-09:00|rotation|120|"
        windows = schedule.parse_windows(raw)
        self.assertEqual([w.school for w in windows], ["schule", ""])
        text = schedule.serialize_windows(windows)
        self.assertIn("departures|schule", text)
        self.assertTrue(text.endswith("|rotation|120|"), "ohne Ferien-Bedingung kein 7. Teil")

    def test_windows_follow_school_holidays(self) -> None:
        ferien = schedule.Window(name="Ferien", start=8 * 60, end=12 * 60, school="ferien")
        schule = schedule.Window(name="Schule", start=8 * 60, end=12 * 60, school="schule")
        in_holidays = datetime(2026, 10, 7, 9, 0, tzinfo=BERLIN)
        normal = datetime(2026, 10, 21, 9, 0, tzinfo=BERLIN)
        self.assertIs(schedule.active_window([ferien, schule], in_holidays, self._holiday)[0], ferien)
        self.assertIs(schedule.active_window([ferien, schule], normal, self._holiday)[0], schule)
        # Ohne Ferien-Daten gilt jeder Tag als Schultag
        self.assertIs(schedule.active_window([ferien, schule], in_holidays)[0], schule)

    def test_overnight_window_uses_the_start_day(self) -> None:
        night = schedule.Window(name="Ferienabend", start=22 * 60, end=2 * 60, school="ferien")
        # 18.10. 01:00: der Abend davor (17.10.) war noch Ferien
        self.assertTrue(schedule._window_active_at(night, datetime(2026, 10, 18, 1, 0, tzinfo=BERLIN), self._holiday))
        self.assertFalse(schedule._window_active_at(night, datetime(2026, 10, 18, 23, 0, tzinfo=BERLIN), self._holiday))

    def test_next_start_skips_days_outside_the_condition(self) -> None:
        ferien = schedule.Window(name="Ferien", start=8 * 60, end=9 * 60, school="ferien")
        _, seconds, upcoming = schedule.active_window([ferien], datetime(2026, 10, 1, 12, 0, tzinfo=BERLIN), self._holiday)
        self.assertIs(upcoming, ferien)
        self.assertEqual(seconds, int((datetime(2026, 10, 5, 8, 0, tzinfo=BERLIN) - datetime(2026, 10, 1, 12, 0, tzinfo=BERLIN)).total_seconds()))

    def test_validation(self) -> None:
        bad = schedule.window_from_dict({"name": "X", "days": [0], "start": "08:00", "end": "09:00", "school": "irgendwann"})
        self.assertTrue(any("Ferien" in e for e in schedule.validate_windows([bad])))
        from app import display_api
        payload = {"schedule": {"windows": [{"name": "Ferien", "days": [0], "start": "08:00", "end": "09:00", "school": "ferien"}]}}
        with patch("app.holidays.configured_region", return_value=""):
            self.assertTrue(any("Bundesland" in e for e in display_api.schedule_errors(payload)))
        with patch("app.holidays.configured_region", return_value="DE-HE"):
            self.assertFalse(any("Bundesland" in e for e in display_api.schedule_errors(payload)))


# ---------------------------------------------------------------------------
# Kalender: Feiertage und Ferien als Quelle
# ---------------------------------------------------------------------------

class CalendarHolidaysTest(_HolidayBase):
    def test_holidays_only_calendar(self) -> None:
        from modules.calendar_ics import data_source as cal
        from modules.calendar_ics import module as cal_module
        config.apply_runtime_config({**config.get_settings_values(), "CALENDAR_ICS_URLS": "", "CALENDAR_HOLIDAYS": "both",
                                     "CALENDAR_DAYS_AHEAD": "7"})
        with patch.object(cal, "now_local", return_value=datetime(2026, 10, 1, 9, 0, tzinfo=BERLIN)):
            content = cal.fetch_calendar_content()
        days = {d["date"]: [e["summary"] for e in d["events"]] for d in content["days"]}
        self.assertEqual(days[date(2026, 10, 3)], ["Tag der Deutschen Einheit"])
        self.assertEqual(days[date(2026, 10, 5)], ["Herbstferien (bis Sa 17.10.)"])
        self.assertNotIn(date(2026, 10, 6), days, "Ferien stehen einmal im Kalender, nicht an jedem Tag")
        self.assertEqual(content["sources"][-1]["label"], cal.HOLIDAY_LABEL)
        env = {**config.get_settings_values(), "IDLE_MODULES": "calendar"}
        self.assertTrue(cal_module.is_enabled(env), "ohne ICS-Adresse, nur mit Feiertagen")
        self.assertEqual(cal_module.describe_status(env)["state"], "ready")
        self.assertEqual(cal_module.validate_settings({}, env), [])

    def test_ongoing_holidays_show_their_end_and_public_only_mode(self) -> None:
        from modules.calendar_ics import data_source as cal
        config.apply_runtime_config({**config.get_settings_values(), "CALENDAR_HOLIDAYS": "both"})
        occ = cal.holiday_occurrences(date(2026, 10, 12), date(2026, 10, 19))
        self.assertEqual([(o["start"], o["summary"]) for o in occ], [(date(2026, 10, 12), "Herbstferien (bis Sa 17.10.)")])
        config.apply_runtime_config({**config.get_settings_values(), "CALENDAR_HOLIDAYS": "public"})
        self.assertEqual(cal.holiday_occurrences(date(2026, 10, 12), date(2026, 10, 19)), [])
        config.apply_runtime_config({**config.get_settings_values(), "CALENDAR_HOLIDAYS": "off"})
        self.assertFalse(cal.holidays_active())

    def test_no_holidays_in_window_and_no_calendar_means_no_content(self) -> None:
        from modules.calendar_ics import data_source as cal
        config.apply_runtime_config({**config.get_settings_values(), "CALENDAR_ICS_URLS": "", "CALENDAR_DAYS_AHEAD": "7"})
        with patch.object(cal, "now_local", return_value=datetime(2026, 11, 10, 9, 0, tzinfo=BERLIN)):
            self.assertIsNone(cal.fetch_calendar_content())


# ---------------------------------------------------------------------------
# Müllabfuhr: Grund einer Verschiebung
# ---------------------------------------------------------------------------

class GarbageShiftReasonTest(unittest.TestCase):
    def test_shifted_date_names_the_holiday(self) -> None:
        from modules.garbage import data_source as ds
        from modules.garbage.renderer import _group_events, _group_note
        mondays = [date(2026, 4, 27) + timedelta(weeks=i) for i in range(4)]       # 27.4. … 18.5.
        events = [{"date": d, "summary": "Biotonne", "label": ""} for d in mondays]
        events.append({"date": date(2026, 5, 26), "summary": "Biotonne", "label": ""})  # Di nach Pfingstmontag
        events.append({"date": date(2026, 6, 1), "summary": "Biotonne", "label": ""})
        pub = [{"start": date(2026, 5, 25), "end": date(2026, 5, 25), "name": "Pfingstmontag"}]
        content = ds.build_garbage_content(events, date(2026, 5, 20), 14, public_holidays=pub)
        shifted = [e for d in content["days"] for e in d["events"] if e["date"] == date(2026, 5, 26)][0]
        self.assertEqual((shifted["shifted_from"], shifted["shifted_reason"]), ("Montag", "Pfingstmontag"))
        self.assertEqual(_group_note(_group_events([shifted])[0]), "wegen Pfingstmontag verschoben, sonst Montag")
        regular = [e for d in content["days"] for e in d["events"] if e["date"] == date(2026, 6, 1)][0]
        self.assertEqual(regular["shifted_reason"], "")
        # Ohne Feiertage wie bisher
        plain = ds.build_garbage_content(events, date(2026, 5, 20), 14)
        shifted_plain = [e for d in plain["days"] for e in d["events"] if e["date"] == date(2026, 5, 26)][0]
        self.assertEqual(_group_note(_group_events([shifted_plain])[0]), "verschoben, sonst Montag")


# ---------------------------------------------------------------------------
# Wetter: Unwetter ist dringend
# ---------------------------------------------------------------------------

class WeatherUrgentTest(unittest.TestCase):
    def setUp(self) -> None:
        from modules.dwd_weather import dwd
        self.dwd = dwd
        self._saved = dict(dwd.DWD_WEATHER_CACHE)

    def tearDown(self) -> None:
        self.dwd.DWD_WEATHER_CACHE.clear()
        self.dwd.DWD_WEATHER_CACHE.update(self._saved)

    def _warnings(self, *items) -> None:
        self.dwd.DWD_WEATHER_CACHE["data"] = {"warnings": list(items)}

    def test_level_three_now_or_soon_is_urgent(self) -> None:
        from modules.dwd_weather import module as weather
        now = time.time() * 1000
        hour = 3600 * 1000
        self._warnings({"level": 2, "headline": "Markant", "_start_ms": now - hour, "_end_ms": now + hour})
        self.assertFalse(weather.is_urgent({}))
        self._warnings({"level": 3, "headline": "Unwetter", "warn_id": "w1", "_start_ms": now + 2 * hour, "_end_ms": now + 5 * hour})
        self.assertTrue(weather.is_urgent({}), "beginnt in 2 h")
        self._warnings({"level": 3, "headline": "Später", "_start_ms": now + 5 * hour, "_end_ms": now + 8 * hour})
        self.assertFalse(weather.is_urgent({}), "beginnt erst in 5 h")
        self._warnings({"level": 4, "headline": "Vorbei", "_start_ms": now - 5 * hour, "_end_ms": now - hour})
        self.assertFalse(weather.is_urgent({}))

    def test_alerts_have_stable_ids(self) -> None:
        from modules.dwd_weather import module as weather
        now = time.time() * 1000
        self._warnings({"level": 4, "headline": "Extremes Unwetter", "event": "ORKAN", "warn_id": "w9",
                        "_start_ms": now - 1000, "_end_ms": now + 3600 * 1000, "description": "Orkanböen", "end": "18:00"})
        alerts = weather.get_alerts({})
        self.assertEqual(len(alerts), 1)
        self.assertEqual((alerts[0]["severity"], alerts[0]["title"]), ("extreme", "Extremes Unwetter"))
        self.assertEqual(alerts[0]["id"], weather.get_alerts({})[0]["id"])


# ---------------------------------------------------------------------------
# NINA
# ---------------------------------------------------------------------------

def _dash_item(wid: str, provider: str = "MOWAS", severity: str = "Severe", msg_type: str = "Alert",
               title: str = "Warnung", sent: str = "2026-09-26T08:00:00+02:00", version: int = 1) -> dict:
    return {"id": wid, "sent": sent, "i18nTitle": {"de": title},
            "payload": {"id": wid, "version": version, "data": {"provider": provider, "severity": severity, "msgType": msg_type, "valid": True}}}


class NinaTest(unittest.TestCase):
    def setUp(self) -> None:
        from modules.nina import data_source as ds
        self.ds = ds
        ds.clear_cache()
        ds._REGIONS = [("065320023023", "Wetzlar, Stadt"), ("065320001001", "Aßlar, Stadt"), ("064120000000", "Frankfurt am Main, Stadt")]

    def tearDown(self) -> None:
        self.ds.clear_cache()

    def test_resolve_region(self) -> None:
        self.assertEqual(self.ds.resolve_region("Wetzlar")["ars"], "065320000000")
        self.assertEqual(self.ds.resolve_region("wetz")["name"], "Wetzlar, Stadt")
        self.assertEqual(self.ds.resolve_region("065320023023")["ars"], "065320000000")
        self.assertEqual(self.ds.resolve_region("06532")["ars"], "065320000000")
        self.assertEqual(self.ds.resolve_region("Gibtsnicht")["ars"], "")

    def test_parse_dashboard_filters_and_sorts(self) -> None:
        items = [
            _dash_item("dwd.1", provider="DWD", title="Sturm"),
            _dash_item("mow.cancel", msg_type="Cancel", title="Entwarnung"),
            _dash_item("mow.minor", severity="Minor", title="Trinkwasser", sent="2026-09-26T09:00:00+02:00"),
            _dash_item("lhp.1", provider="LHP", severity="Severe", title="Hochwasser"),
            _dash_item("mow.old", severity="Minor", title="Abgelaufen"),
        ]
        details = {"mow.old": {"expires": "2026-09-01T00:00:00+02:00"}}
        now = datetime(2026, 9, 26, 12, 0, tzinfo=BERLIN)
        warnings, excluded = self.ds.parse_dashboard(items, "minor", lambda i, v: details.get(i, {}), now=now)
        self.assertEqual(excluded, 1, "Wetterwarnungen kommen vom DWD-Modul")
        self.assertEqual([w["headline"] for w in warnings], ["Hochwasser", "Trinkwasser"])
        self.assertEqual(warnings[0]["provider_label"], "Hochwasser")
        severe_only, _ = self.ds.parse_dashboard(items, "severe", lambda i, v: {}, now=now)
        self.assertEqual([w["headline"] for w in severe_only], ["Hochwasser"])

    def test_text_joins_hard_line_breaks(self) -> None:
        raw = "Schutzchlorung in der Gemeinde Rehlingen<br/>Siersburg aufgehoben.<br/><br/>Hinweise:<br/>- Wasser abkochen<br/>- Nachbarn informieren"
        self.assertEqual(self.ds._text(raw),
                         "Schutzchlorung in der Gemeinde Rehlingen Siersburg aufgehoben.\nHinweise:\n- Wasser abkochen\n- Nachbarn informieren")

    def test_module_only_has_content_with_warnings_and_is_urgent(self) -> None:
        from modules.nina import module as nina
        env = {"IDLE_MODULES": "nina", "NINA_REGION": "Wetzlar"}
        with patch.object(self.ds, "fetch_warnings", return_value={"region": "Wetzlar", "warnings": []}):
            self.assertIsNone(nina.fetch_content(env))
        self.assertFalse(nina.is_urgent(env))
        warning = {"id": "lhp.1", "version": 2, "headline": "Hochwasser", "severity": "severe", "provider_label": "Hochwasser",
                   "sender": "Kreis", "description": "Pegel steigt", "expires": "2026-09-27T18:00:00+02:00", "area": "Wetzlar"}
        self.ds._CACHE.update(ars="065320000000", fetched_at=time.time(), warnings=[warning])
        self.assertTrue(nina.is_urgent(env))
        alerts = nina.get_alerts(env)
        self.assertEqual(alerts[0]["id"], "nina:lhp.1", "ohne Version: Aktualisierungen sind keine neue Warnung")
        self.assertEqual(alerts[0]["until"], "27.09. 18:00")
        self.assertFalse(nina.is_urgent({"IDLE_MODULES": "", "NINA_REGION": "Wetzlar"}), "nicht im Programm")

    def test_render_all_themes_and_sizes(self) -> None:
        from app.module_services import ModuleRenderServices
        from modules.nina.renderer import render_nina_module
        base = ModuleRenderServices.from_runtime()
        long_text = " ".join(["Der Pegel steigt weiter an."] * 60)
        content = {"region": "Wetzlar, Stadt", "warnings": [
            {"id": "a", "severity": "extreme", "severity_label": "Extreme Gefahr", "provider_label": "Katastrophenschutz",
             "headline": "Großbrand in der Innenstadt " * 4, "description": long_text, "instruction": "Fenster schließen.\n- Lüftung aus",
             "area": "Wetzlar", "sender": "Kreis", "sent": "2026-09-26T08:00:00+02:00", "expires": ""},
            {"id": "b", "severity": "minor", "severity_label": "Information", "provider_label": "Polizei", "headline": "Sperrung",
             "description": "", "instruction": "", "area": "", "sender": "", "sent": "", "expires": ""},
        ]}
        for theme in ("eink", "dark", "light"):
            for w, h, compact in ((1200, 1600, False), (800, 480, False), (1200, 400, True)):
                with self.subTest(theme=theme, size=(w, h), compact=compact):
                    svc = ModuleRenderServices(render_width=w, render_height=h, display_theme=theme, load_font=base.load_font)
                    img = render_nina_module(svc, content, compact=compact)
                    self.assertEqual(img.size, (w, h))
                    if theme == "eink":
                        from app.image_rendering import SPECTRA6_COLORS
                        palette = set(SPECTRA6_COLORS.values())
                        off = sum(n for n, c in img.getcolors(w * h) if c not in palette)
                        self.assertEqual(off, 0, "E-Ink: nur die sechs Panelfarben")


# ---------------------------------------------------------------------------
# Benachrichtigung „Warnungen“
# ---------------------------------------------------------------------------

class _Capture:
    def __init__(self):
        self.calls: list[dict] = []

    def __call__(self, url, **kw):
        self.calls.append({"url": url, **kw})
        return SimpleNamespace(status_code=200, text="")


class WarningNotificationsTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._state = patch.object(device, "DEVICE_STATE_PATH", Path(self._tmp.name) / "device_state.json")
        self._state.start()
        self.cap = _Capture()
        self._post = patch.object(http_client.HTTP_SESSION, "post", side_effect=self.cap)
        self._post.start()

    def tearDown(self) -> None:
        self._post.stop()
        self._state.stop()
        self._tmp.cleanup()

    def _embed(self, index: int) -> dict:
        call = self.cap.calls[index]
        payload = call["json"] if "json" in call else json.loads(call["data"]["payload_json"])
        return payload["embeds"][0]

    def test_one_message_per_warning_and_all_clear(self) -> None:
        n = nf.Notifier("https://discord.com/api/webhooks/1/abc", ("warnings",), 30, 7, "http://srv:8787", "", snapshot=lambda: PNG)
        now = datetime(2026, 9, 26, 12, 0, tzinfo=BERLIN)
        alert = {"id": "nina:lhp.1", "title": "Hochwasser an der Lahn", "text": "Pegel steigt", "source": "Hochwasser · Kreis",
                 "severity": "severe", "until": "27.09. 18:00", "area": "Wetzlar"}
        self.assertEqual(n.on_cycle({}, now, alerts=[alert]), ["alert"])
        embed = self._embed(0)
        self.assertEqual((embed["title"], embed["color"]), ("Hochwasser an der Lahn", nf.COLORS["alert"]))
        self.assertIn(("Gilt bis", "27.09. 18:00"), [(f["name"], f["value"]) for f in embed["fields"]])
        self.assertEqual(n.on_cycle({}, now, alerts=[dict(alert, text="Update")]), [], "Aktualisierung: keine neue Nachricht")
        self.assertEqual(n.on_cycle({}, now, alerts=None), [], "nicht abgefragt: nichts entscheiden")
        self.assertEqual(n.on_cycle({}, now, alerts=[]), ["alert_end"])
        self.assertEqual(self._embed(1)["title"], "Entwarnung: Hochwasser an der Lahn")
        self.assertEqual(n.on_cycle({}, now, alerts=[]), [])

    def test_disabled_event_sends_nothing(self) -> None:
        n = nf.Notifier("https://discord.com/api/webhooks/1/abc", ("offline",), 30, 7, "", "", snapshot=lambda: PNG)
        alert = {"id": "dwd:x", "title": "Unwetter", "severity": "severe"}
        self.assertEqual(n.on_cycle({}, datetime(2026, 9, 26, 12, 0, tzinfo=BERLIN), alerts=[alert]), [])
        self.assertEqual(self.cap.calls, [])

    def test_server_collects_alerts_of_enabled_modules(self) -> None:
        import app.server as server

        class _Mod:
            def __init__(self, mid, enabled, alerts):
                self.MODULE_ID, self._enabled, self._alerts = mid, enabled, alerts

            def is_enabled(self, env):
                return self._enabled

            def get_alerts(self, env):
                if self._alerts is None:
                    raise RuntimeError("kaputt")
                return self._alerts

        mods = [_Mod("a", True, [{"id": "a:1", "title": "A"}]), _Mod("b", False, [{"id": "b:1"}]), _Mod("c", True, None)]
        with patch.object(server._registry, "get_modules", return_value=mods):
            self.assertEqual(server._active_alerts({}), [{"id": "a:1", "title": "A"}])

    def test_event_is_offered_in_the_ui(self) -> None:
        self.assertIn("warnings", [key for key, _ in nf.EVENT_OPTIONS])


if __name__ == "__main__":
    unittest.main()
