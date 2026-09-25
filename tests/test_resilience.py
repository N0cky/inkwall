"""
Tests für die Robustheit von Server und Datenquellen:
- Zeitplan in den Nächten der Zeitumstellung (echte Sekunden statt Wanduhr)
- Render-Worker: überlebt Fehler, /health meldet einen toten oder hängenden Worker
- ein Modul, das beim State-Key oder is_enabled wirft, stoppt den Render nicht
- Warmstart: Hash des letzten Bilds von der Platte, kein Render beim Import
- Test-Auftrag, Benachrichtigungen im Hintergrund, device_state ohne verlorene Felder
- Konfiguration: Vorschau-Theme nur im eigenen Thread, eingefrorener Stand im
  Render, gleichzeitige Speichervorgänge
- Benachrichtigungen in Ortszeit mit deutschen Wochentagen
- HTTP: kein Retry-After-Schlaf, keine Lese-Wiederholung, kurzer Verbindungsaufbau
- Müll/Kalender/Abfahrten: nur eingetragene Quellen zählen für den Neu-Render,
  alte Stände werden aufgeräumt, 404 und „nicht gefunden“ werden gemerkt
- Tankpreise: Prüfen schickt list.php einmal, fehlende Stationen werden gemerkt
- Pollen: Teilregionen, ganzes Land als Höchstwert, alte Werte, Region fehlt
- Zusammenfassungen und /metrics gehen nie ins Netz
"""

from __future__ import annotations

import hashlib
import json
import tempfile
import threading
import time
import unittest
from datetime import date, datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import requests

import app.config as config
import app.device as device
import app.http_client as http_client
import app.notifications as nf
import app.server as server
from app import ics, schedule
from tests.test_render_pipeline import _FakeModule, _PipelineTestBase, _with_modules

BERLIN = ZoneInfo("Europe/Berlin")


class _Stop(BaseException):
    """Beendet die Endlosschleife des Workers im Test (kein Exception – die fängt der Worker)."""


# ---------------------------------------------------------------------------
# Zeitplan und Zeitumstellung
# ---------------------------------------------------------------------------

class ScheduleDstTest(unittest.TestCase):
    def setUp(self) -> None:
        self.night = schedule.parse_windows("Nachts|*|22:00-07:00||900|")

    def test_normal_night(self) -> None:
        active, seconds, _ = schedule.active_window(self.night, datetime(2026, 9, 25, 1, 30, tzinfo=BERLIN))
        self.assertIs(active, self.night[0])
        self.assertEqual(seconds, 5 * 3600 + 1800)

    def test_autumn_night_has_one_hour_more(self) -> None:
        # 25.10.2026: 03:00 MESZ → 02:00 MEZ. Von 01:30 bis 07:00 vergehen 6,5 h
        _, seconds, _ = schedule.active_window(self.night, datetime(2026, 10, 25, 1, 30, tzinfo=BERLIN))
        self.assertEqual(seconds, 6 * 3600 + 1800)

    def test_spring_night_has_one_hour_less(self) -> None:
        # 29.03.2026: 02:00 MEZ → 03:00 MESZ. Von 01:30 bis 07:00 vergehen 4,5 h
        _, seconds, _ = schedule.active_window(self.night, datetime(2026, 3, 29, 1, 30, tzinfo=BERLIN))
        self.assertEqual(seconds, 4 * 3600 + 1800)

    def test_start_in_the_repeated_hour_is_not_in_the_past(self) -> None:
        windows = schedule.parse_windows("Früh|*|02:45-05:00||900|")
        # Zweites 02:30 (MEZ, fold=1): das erste 02:45 (MESZ) liegt schon zurück, das zweite in 15 min
        now = datetime(2026, 10, 25, 2, 30, fold=1, tzinfo=BERLIN)
        active, seconds, upcoming = schedule.active_window(windows, now)
        self.assertIsNone(active)
        self.assertIs(upcoming, windows[0])
        self.assertEqual(seconds, 15 * 60)


# ---------------------------------------------------------------------------
# Worker und /health
# ---------------------------------------------------------------------------

class WorkerTest(unittest.TestCase):
    def test_loop_survives_an_error_and_pauses(self) -> None:
        waits: list = []

        def fake_wait(timeout=None):
            waits.append(timeout)
            raise _Stop()

        with patch.object(server, "_run_worker_cycle", return_value="k"), \
             patch.object(server, "_check_device_offline"), \
             patch.object(server, "_get_background_poll_seconds", side_effect=RuntimeError("Modul kaputt")), \
             patch.object(server._wake_event, "wait", side_effect=fake_wait), \
             patch.object(server, "_worker_heartbeat", 0.0):
            with self.assertRaises(_Stop):
                server.periodic_worker()
            self.assertGreater(server._worker_heartbeat, 0.0, "Lebenszeichen gesetzt")
        self.assertEqual(waits, [server.WORKER_ERROR_PAUSE_S])

    def test_health_reports_dead_or_stuck_worker(self) -> None:
        client = server.app.test_client()
        with patch.object(server, "_worker_thread", SimpleNamespace(is_alive=lambda: False)):
            response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["ok"])

        alive = SimpleNamespace(is_alive=lambda: True)
        with patch.object(server, "_worker_thread", alive), patch.object(server, "_worker_heartbeat", time.time() - 5):
            self.assertEqual(client.get("/health").status_code, 200)
        with patch.object(server, "_worker_thread", alive), patch.object(server, "_worker_heartbeat", time.time() - 10 * 3600):
            response = client.get("/health")
        self.assertEqual(response.status_code, 503)
        self.assertFalse(response.get_json()["worker"]["ok"])

    def test_health_is_ok_without_worker(self) -> None:
        with patch.object(server, "_worker_thread", None):
            self.assertEqual(server.app.test_client().get("/health").status_code, 200)


class _BrokenKey(_FakeModule):
    def get_state_key(self, content):
        raise KeyError("state")


class _BrokenEnabled(_FakeModule):
    def is_enabled(self, env):
        raise RuntimeError("is_enabled kaputt")


class RenderGuardTest(_PipelineTestBase):
    def _render(self, idle: list) -> str | None:
        patches = _with_modules([], idle)
        with patches[0], patches[1], patches[2]:
            return server.render_if_changed(None)

    def test_state_key_error_skips_only_that_module(self) -> None:
        broken, good = _BrokenKey("broken", 100, content="a"), _FakeModule("good", 100, content="b")
        for order in ([broken, good], [good, broken]):
            with patch.object(server, "_rotation_sequence", return_value=order):
                key = self._render(order)
            self.assertTrue(key and key.startswith("good:"), key)
        self.assertEqual(broken.render_calls, 0)

    def test_is_enabled_error_counts_as_off(self) -> None:
        broken, good = _BrokenEnabled("broken", 100, content="a"), _FakeModule("good", 100, content="b")
        key = self._render([broken, good])
        self.assertTrue(key and key.startswith("good:"), key)


# ---------------------------------------------------------------------------
# Warmstart, ACK, Benachrichtigungen im Hintergrund
# ---------------------------------------------------------------------------

class WarmStartTest(_PipelineTestBase):
    def test_hash_and_state_come_from_disk(self) -> None:
        (self.tmp / "current.png").write_bytes(b"PNGDATA")
        (self.tmp / "state.txt").write_text("fake:abc:5", encoding="utf-8")
        with patch.object(server._registry, "get_module_by_id", return_value=_FakeModule("fake", 100)):
            server._restore_render_state()
        self.assertEqual(server._esp32_state["hash"], hashlib.md5(b"PNGDATA").hexdigest())
        self.assertEqual(server._esp32_state["state"], "fake:abc:5")
        self.assertEqual(server._esp32_state["media_type"], "fake")
        self.assertTrue(server._esp32_state["rendered_at"].endswith("Z"))

    def test_nothing_on_disk_keeps_empty_state(self) -> None:
        server._restore_render_state()
        self.assertEqual(server._esp32_state["hash"], "")


class _TmpDeviceState(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._state = patch.object(device, "DEVICE_STATE_PATH", Path(self._tmp.name) / "device_state.json")
        self._state.start()

    def tearDown(self) -> None:
        self._state.stop()
        self._tmp.cleanup()


class AckTest(_TmpDeviceState):
    def test_other_acks_keep_a_fresh_test_request(self) -> None:
        client = server.app.test_client()
        device.request_test_banner()
        try:
            with patch.object(server, "_last_ack", {}):
                response = client.post("/ack", json={"device_id": "d", "hash": "x", "result": "updated"})
            self.assertEqual(response.status_code, 200)
            self.assertTrue(device.test_banner_pending(), "ein laufender Zyklus verschluckt den Auftrag nicht")
        finally:
            device.consume_test_banner()

    def test_notifications_run_in_the_background_with_a_target(self) -> None:
        on_ack = MagicMock(return_value=[])
        with patch.object(server, "_notifier", return_value=SimpleNamespace(url="https://ntfy.sh/x", on_ack=on_ack)), \
             patch.object(server._notify_pool, "submit") as submit:
            server._dispatch_ack_notifications({"device_id": "d"}, {})
        submit.assert_called_once()
        on_ack.assert_not_called()

    def test_without_target_markers_are_updated_right_away(self) -> None:
        on_ack = MagicMock(return_value=[])
        with patch.object(server, "_notifier", return_value=SimpleNamespace(url="", on_ack=on_ack)), \
             patch.object(server._notify_pool, "submit") as submit:
            server._dispatch_ack_notifications({"device_id": "d"}, {})
        submit.assert_not_called()
        on_ack.assert_called_once()


class DeviceStateMergeTest(_TmpDeviceState):
    def test_notifier_does_not_overwrite_fields_written_meanwhile(self) -> None:
        notifier = nf.Notifier("", ("offline",), 30)
        state, markers = notifier._markers()
        device.mark_cleaned(datetime(2026, 9, 25, 3, 0, tzinfo=BERLIN))     # schreibt ein anderer, während die Nachricht läuft
        markers["error_streak"] = 2
        notifier._save(state, markers)
        saved = device.load_device_state()
        self.assertTrue(saved.get("last_clean_at"), "die Reinigung bleibt vermerkt")
        self.assertEqual(saved["notify"]["error_streak"], 2)


class NotificationTimeTest(_TmpDeviceState):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list = []
        self._post = patch.object(http_client.HTTP_SESSION, "post",
                                  side_effect=lambda url, **kw: self.calls.append(kw) or SimpleNamespace(status_code=204, text=""))
        self._post.start()
        self._tz = patch.object(nf, "local_tz", return_value=BERLIN)
        self._tz.start()

    def tearDown(self) -> None:
        self._tz.stop()
        self._post.stop()
        super().tearDown()

    def _embed(self, index: int) -> dict:
        call = self.calls[index]
        payload = call["json"] if "json" in call else json.loads(call["data"]["payload_json"])
        return payload["embeds"][0]

    def test_offline_time_is_local(self) -> None:
        n = nf.Notifier("https://discord.com/api/webhooks/1/abc", ("offline",), 30, snapshot=lambda: None)
        ack = {"device_id": "esp", "ack_at": "2026-09-03T10:00:00Z"}
        self.assertEqual(n.on_cycle(ack, datetime(2026, 9, 3, 13, 0, tzinfo=BERLIN)), ["offline"])
        fields = {f["name"]: f["value"] for f in self._embed(0)["fields"]}
        self.assertEqual(fields["Zuletzt gemeldet"], "03.09. 12:00", "Berliner Zeit, nicht UTC")

    def test_daily_title_has_german_weekday(self) -> None:
        n = nf.Notifier("https://discord.com/api/webhooks/1/abc", ("daily",), 30, 7, snapshot=lambda: None)
        self.assertEqual(n.on_cycle({}, datetime(2026, 9, 8, 7, 30, tzinfo=BERLIN)), ["daily"])
        self.assertIn("Dienstag, 08.09.", self._embed(0)["title"])


# ---------------------------------------------------------------------------
# Konfiguration
# ---------------------------------------------------------------------------

class ConfigIsolationTest(unittest.TestCase):
    def test_override_is_invisible_to_other_threads(self) -> None:
        before = config.get_cfg()
        entered, release, seen = threading.Event(), threading.Event(), {}

        def preview():
            with config.override_runtime_config(display_theme="eink"):
                seen["inside"] = config.get_cfg().display_theme
                entered.set()
                release.wait(5)

        thread = threading.Thread(target=preview)
        thread.start()
        self.assertTrue(entered.wait(5))
        try:
            self.assertIs(config.get_cfg(), before, "der Rest des Servers sieht das Vorschau-Theme nicht")
        finally:
            release.set()
            thread.join(5)
        self.assertEqual(seen["inside"], "eink")

    def test_render_keeps_its_config_while_someone_saves(self) -> None:
        seen: dict = {}

        class _Saver(_FakeModule):
            def render(self, env, content):
                seen["before"] = config.get_cfg()
                config.apply_runtime_config({**config.get_settings_values(), "RENDER_WIDTH": "1234"})
                seen["after"] = config.get_cfg()
                return super().render(env, content)

        mod = _Saver("saver", 5, content="x")
        patches = _with_modules([mod], [])
        try:
            with patches[0], patches[1], patches[2], patch.object(server, "_save_image"):
                server.render_if_changed(None)
            self.assertIs(seen["after"], seen["before"], "mitten im Render ändert sich nichts")
            self.assertEqual(config.get_cfg().base_render_width, 1234, "danach gilt der neue Stand")
        finally:
            config.apply_runtime_config()

    def test_concurrent_writes_keep_every_change(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            env_file = Path(tmp) / "settings.env"
            env_file.write_text("DISPLAY_THEME=dark\n", encoding="utf-8")
            with patch.object(config, "ENV_FILE_PATH", env_file):
                threads = [threading.Thread(target=config.write_env_settings, args=({f"ZZ_KEY_{i}": str(i)},)) for i in range(12)]
                for t in threads:
                    t.start()
                for t in threads:
                    t.join(10)
                values = config.read_env_settings()
            self.assertEqual({k: v for k, v in values.items() if k.startswith("ZZ_KEY_")}, {f"ZZ_KEY_{i}": str(i) for i in range(12)})
            self.assertEqual(values["DISPLAY_THEME"], "dark")
            self.assertEqual([p.name for p in Path(tmp).iterdir()], ["settings.env"], "keine Temp-Dateien übrig")


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

class HttpClientTest(unittest.TestCase):
    def test_retry_policy(self) -> None:
        retry = http_client.HTTP_SESSION.get_adapter("https://example.invalid").max_retries
        self.assertEqual(retry.read, 0, "ein hängender Server kostet ein Timeout, nicht drei")
        self.assertFalse(retry.respect_retry_after_header, "kein ungedeckelter Schlaf unter dem Render-Lock")
        self.assertNotIn(429, retry.status_forcelist)

    def test_connect_timeout_is_short(self) -> None:
        captured: dict = {}

        def fake_send(self, request, timeout=None, **kw):
            captured["timeout"] = timeout
            raise requests.ConnectionError("stop")

        with patch("requests.adapters.HTTPAdapter.send", fake_send):
            with self.assertRaises(requests.ConnectionError):
                http_client.HTTP_SESSION.get("http://example.invalid/", timeout=30)
        self.assertEqual(captured["timeout"], (http_client.CONNECT_TIMEOUT_SECONDS, 30))


def _no_network(*_a, **_k):
    raise AssertionError("kein Netzzugriff erwartet")


# ---------------------------------------------------------------------------
# Müllabfuhr
# ---------------------------------------------------------------------------

from modules.garbage import data_source as garbage_ds            # noqa: E402
from modules.garbage import module as garbage                    # noqa: E402

GARBAGE_ICS = (Path(__file__).resolve().parent / "fixtures" / "garbage_wetzlar.ics").read_text(encoding="utf-8")
URL_2026 = "https://kommune.test/abfuhr-2026.ics"
URL_2027 = "https://kommune.test/abfuhr-2027.ics"


class GarbageCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        garbage_ds.clear_cache()
        self._tmp = tempfile.TemporaryDirectory()
        self._file = patch.object(garbage_ds, "CACHE_FILE", Path(self._tmp.name) / "garbage_cache.json")
        self._file.start()
        config.apply_runtime_config({**config.read_env_settings(), "IDLE_MODULES": "garbage",
                                     "GARBAGE_ICS_URLS": "Zuhause|https://kommune.test/abfuhr-{year}.ics"})
        self.env = config.get_settings_values()

    def tearDown(self) -> None:
        garbage_ds.clear_cache()
        self._file.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def test_last_years_calendar_no_longer_forces_renders(self) -> None:
        now = time.time()
        events = garbage_ds.parse_ics_events(GARBAGE_ICS, around=date(2026, 9, 10))
        garbage_ds._CACHE[URL_2026] = {"fetched_at": now - 30 * 86400, "last_attempt_at": now - 30 * 86400, "events": events, "missing": False}
        garbage_ds._CACHE[URL_2027] = {"fetched_at": now, "last_attempt_at": now, "events": events, "missing": False}
        january = datetime(2027, 1, 5, 8, 0, tzinfo=BERLIN)
        with patch.object(garbage_ds, "now_local", return_value=january), \
             patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network):
            self.assertFalse(garbage_ds.should_refresh_garbage(), "der Vorjahres-Kalender zählt nicht mehr")
            garbage.fetch_content(self.env)
        self.assertNotIn(URL_2026, garbage_ds._CACHE, "und wird aufgeräumt")
        self.assertIn(URL_2027, garbage_ds._CACHE)
        self.assertNotIn("2026.ics", garbage_ds.CACHE_FILE.read_text(encoding="utf-8"))

    def test_stale_current_calendar_still_refreshes(self) -> None:
        now = time.time()
        garbage_ds._CACHE[URL_2026] = {"fetched_at": now - 30 * 86400, "last_attempt_at": now - 30 * 86400,
                                       "events": [], "missing": False}
        with patch.object(garbage_ds, "now_local", return_value=datetime(2026, 9, 10, 8, 0, tzinfo=BERLIN)):
            self.assertTrue(garbage_ds.should_refresh_garbage())

    def test_missing_year_is_not_asked_every_five_minutes(self) -> None:
        garbage_ds._CACHE[URL_2027] = {"fetched_at": 0.0, "last_attempt_at": time.time() - 600, "events": None, "missing": True}
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network):
            self.assertIsNone(garbage_ds.fetch_ics_events(URL_2027))

    def test_cache_only_never_fetches(self) -> None:
        with http_client.cache_only(), patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network):
            self.assertIsNone(garbage_ds.fetch_ics_events(URL_2026))
        self.assertNotIn(URL_2026, garbage_ds._CACHE, "kein Versuch vermerkt")


# ---------------------------------------------------------------------------
# Kalender
# ---------------------------------------------------------------------------

from modules.calendar_ics import data_source as calendar_ds      # noqa: E402
from modules.calendar_ics import module as calendar              # noqa: E402


class CalendarCacheTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._file = patch.object(calendar_ds, "CACHE_FILE", Path(self._tmp.name) / "calendar_cache.json")
        self._file.start()
        calendar_ds.clear_cache()
        config.apply_runtime_config({**config.read_env_settings(), "IDLE_MODULES": "calendar",
                                     "CALENDAR_ICS_URLS": "Familie|https://cal.test/a.ics"})
        self.env = config.get_settings_values()

    def tearDown(self) -> None:
        calendar_ds.clear_cache()
        self._file.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def test_removed_calendar_is_ignored_and_forgotten(self) -> None:
        now = time.time()
        text = "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:1\r\nDTSTART;VALUE=DATE:20260911\r\nSUMMARY:Alt\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n"
        parsed = ics.parse_calendar(text)
        with calendar_ds._LOCK:
            calendar_ds._load_disk_cache()
            calendar_ds._CACHE["https://cal.test/old.ics"] = {"fetched_at": 0.0, "last_attempt_at": 0.0, "calendar": parsed, "error": "", "text": text}
            calendar_ds._CACHE["https://cal.test/a.ics"] = {"fetched_at": now, "last_attempt_at": now, "calendar": parsed, "error": "", "text": text}
        self.assertFalse(calendar_ds.should_refresh_calendar(), "der entfernte Kalender löst keinen Neu-Render aus")
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network), \
             patch.object(calendar_ds, "now_local", return_value=datetime(2026, 9, 10, 8, 0, tzinfo=BERLIN)):
            self.assertIsNotNone(calendar.fetch_content(self.env))
        self.assertEqual(list(calendar_ds._CACHE), ["https://cal.test/a.ics"])
        self.assertNotIn("old.ics", calendar_ds.CACHE_FILE.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Abfahrten
# ---------------------------------------------------------------------------

from modules.departures import data_source as departures_ds      # noqa: E402
from tests.test_departures_module import _fake_get as departures_get   # noqa: E402


class DeparturesResolveTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self._file = patch.object(departures_ds, "STOPS_FILE", Path(self._tmp.name) / "stops.json")
        self._file.start()
        departures_ds.clear_cache()
        self.settings = {**config.read_env_settings(), "IDLE_MODULES": "departures", "DEPARTURES_STOPS": "Alex|Alexanderplatz",
                         "DEPARTURES_API_URL": "https://v6.vbb.transport.rest"}
        config.apply_runtime_config(self.settings)
        self.calls: list[str] = []

    def tearDown(self) -> None:
        departures_ds.clear_cache()
        self._file.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def _counting(self, url, params=None, **kw):
        self.calls.append(url)
        return departures_get(url, params=params, **kw)

    def _failing(self, url, params=None, **kw):
        self.calls.append(url)
        raise requests.ConnectionError("down")

    def test_unknown_name_is_not_searched_again_right_away(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._counting):
            self.assertIsNone(departures_ds.resolve_stop("Nirgendwo"))
            self.assertIsNone(departures_ds.resolve_stop("Nirgendwo"))
            departures_ds.resolve_stop("Nirgendwo", force=True)       # „Verbindung prüfen“ fragt trotzdem
        self.assertEqual(sum("/locations" in u for u in self.calls), 2, "einmal normal, einmal erzwungen")

    def test_network_error_is_remembered(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._failing):
            self.assertIsNone(departures_ds.resolve_stop("Alexanderplatz"))
            self.assertIsNone(departures_ds.resolve_stop("Alexanderplatz"))
        self.assertEqual(len(self.calls), 1)

    def test_resolution_is_per_api(self) -> None:
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._counting):
            self.assertEqual(departures_ds.resolve_stop("Alexanderplatz")["id"], "900100003")
            config.apply_runtime_config({**self.settings, "DEPARTURES_API_URL": "https://v6.other.test"})
            departures_ds.resolve_stop("Alexanderplatz")
        self.assertEqual([u.split("/locations")[0] for u in self.calls if "/locations" in u],
                         ["https://v6.vbb.transport.rest", "https://v6.other.test"])

    def test_old_entry_helps_while_the_api_is_down(self) -> None:
        with departures_ds._LOCK:
            departures_ds._load_disk()
            departures_ds._RESOLVED["alexanderplatz"] = {"id": "123", "name": "Alt"}
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._failing):
            self.assertEqual(departures_ds.resolve_stop("Alexanderplatz")["id"], "123")

    def test_cache_only_does_not_resolve(self) -> None:
        with http_client.cache_only(), patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network):
            self.assertIsNone(departures_ds.resolve_stop("Alexanderplatz"))
            self.assertIsNone(departures_ds.fetch_stop_departures("900100003", 60)["departures"])

    def test_removed_stop_does_not_force_renders(self) -> None:
        config.apply_runtime_config({**self.settings, "DEPARTURES_STOPS": "900100003"})
        now = time.time()
        with departures_ds._LOCK:
            departures_ds._CACHE["900100003"] = {"fetched_at": now, "last_attempt_at": now, "departures": [], "error": "", "name": ""}
            departures_ds._CACHE["111"] = {"fetched_at": 0.0, "last_attempt_at": 0.0, "departures": [], "error": "", "name": ""}
        self.assertFalse(departures_ds.should_refresh_departures())


# ---------------------------------------------------------------------------
# Tankpreise
# ---------------------------------------------------------------------------

from modules.fuel_prices import data_source as fuel_ds           # noqa: E402
from modules.fuel_prices import module as fuel                   # noqa: E402
from tests.test_fuel_module import NOW as FUEL_NOW, _TempHistory, _fake_get as fuel_get, _settings as fuel_settings   # noqa: E402


class FuelRequestsTest(_TempHistory):
    def setUp(self) -> None:
        super().setUp()
        self.calls: list[str] = []

    def tearDown(self) -> None:
        config.apply_runtime_config()
        super().tearDown()

    def _counting(self, url, params=None, **kw):
        self.calls.append(url)
        return fuel_get(url, params=params, **kw)

    def test_probe_sends_the_radius_search_once(self) -> None:
        config.apply_runtime_config(fuel_settings())
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._counting), \
             patch.object(fuel_ds, "now_local", return_value=FUEL_NOW):
            result = fuel.probe(config.get_settings_values())
            self.assertTrue(result["ok"], result)
            self.assertIsNotNone(fuel_ds.fetch_fuel_content(), "der Stand des Prüfens gilt fürs Display")
        self.assertEqual(sum("list.php" in u for u in self.calls), 1, self.calls)

    def test_unknown_station_detail_is_remembered(self) -> None:
        config.apply_runtime_config(fuel_settings())
        unknown = "00000000-0000-0000-0000-000000000000"
        with patch.object(http_client.HTTP_SESSION, "get", side_effect=self._counting):
            self.assertIsNone(fuel_ds.station_detail(unknown))
            self.assertIsNone(fuel_ds.station_detail(unknown))
        self.assertEqual(sum("detail.php" in u for u in self.calls), 1)

    def test_cache_only_does_not_poll_tankerkoenig(self) -> None:
        config.apply_runtime_config(fuel_settings())
        with http_client.cache_only(), patch.object(http_client.HTTP_SESSION, "get", side_effect=_no_network):
            self.assertIsNone(fuel_ds.fetch_fuel_content())
            self.assertEqual(fuel.summarize(config.get_settings_values()), "Preise noch nicht geladen")


# ---------------------------------------------------------------------------
# Pollen
# ---------------------------------------------------------------------------

from modules.dwd_weather import dwd_pollen                        # noqa: E402

POLLEN_FEED = {
    "last_update": "2026-09-25 11:00 Uhr",
    "next_update": "2026-09-26 11:00 Uhr",
    "content": [
        {"region_id": 90, "partregion_id": 91, "region_name": "Hessen", "partregion_name": "Nordhessen und hess. Mittelgebirge",
         "Pollen": {"Birke": {"today": "1", "tomorrow": "0", "dayafter_to": "0-1"},
                    "Graeser": {"today": "2", "tomorrow": "2-3", "dayafter_to": "1"}}},
        {"region_id": 90, "partregion_id": 92, "region_name": "Hessen", "partregion_name": "Rhein-Main",
         "Pollen": {"Birke": {"today": "2", "tomorrow": "0-1", "dayafter_to": "0"},
                    "Graeser": {"today": "1", "tomorrow": "3", "dayafter_to": "1"}}},
        {"region_id": 20, "partregion_id": -1, "region_name": "Mecklenburg-Vorpommern", "partregion_name": "",
         "Pollen": {"Birke": {"today": "0", "tomorrow": "0", "dayafter_to": "0"}}},
    ],
}


class PollenRegionTest(unittest.TestCase):
    def setUp(self) -> None:
        self.calls = 0
        self._reset()
        self._get = patch.object(http_client.HTTP_SESSION, "get", side_effect=self._feed)
        self._get.start()
        self._shift = patch.object(dwd_pollen, "_compute_day_shift", return_value=0)
        self._shift.start()

    def tearDown(self) -> None:
        self._shift.stop()
        self._get.stop()
        self._reset()
        config.apply_runtime_config()

    @staticmethod
    def _reset() -> None:
        dwd_pollen._POLLEN_CACHE.update({"fetched_at": 0.0, "last_attempt_at": 0.0, "region_key": "", "data": None,
                                         "next_refresh_at": 0.0, "not_found_at": 0.0})

    def _feed(self, url, **kw):
        self.calls += 1
        return SimpleNamespace(status_code=200, json=lambda: POLLEN_FEED, raise_for_status=lambda: None)

    def _fetch(self, region: str) -> dict | None:
        self._reset()
        config.apply_runtime_config({**config.read_env_settings(), "DWD_POLLEN_REGION": region, "DWD_POLLEN_ALLERGENS": "Birke,Graeser"})
        return dwd_pollen.fetch_dwd_pollen()

    def test_sub_region_is_found(self) -> None:
        data = self._fetch("90:92")
        self.assertEqual(data["partregion_name"], "Rhein-Main")
        self.assertEqual(data["allergens"]["Birke"]["today"], 2.0)
        self.assertEqual(data["allergens"]["Birke"]["dayafter_to"], 0.0)

    def test_whole_state_takes_the_highest_value(self) -> None:
        data = self._fetch("90:-1")
        self.assertEqual(data["partregion_name"], "")
        self.assertEqual(data["allergens"]["Birke"], {"today": 2.0, "tomorrow": 0.5, "dayafter_to": 0.5})
        self.assertEqual(data["allergens"]["Graeser"]["tomorrow"], 3.0)

    def test_old_values_still_mean_the_sub_region(self) -> None:
        data = self._fetch("92:-1")
        self.assertEqual(data["partregion_name"], "Rhein-Main")
        self.assertEqual(data["allergens"]["Birke"]["dayafter_to"], 0.0)

    def test_state_without_sub_regions(self) -> None:
        self.assertEqual(self._fetch("20:-1")["region_name"], "Mecklenburg-Vorpommern")

    def test_unknown_region_is_not_downloaded_every_five_minutes(self) -> None:
        self.assertIsNone(self._fetch("77:-1"))
        dwd_pollen._POLLEN_CACHE["last_attempt_at"] -= 3600          # der normale Backoff wäre vorbei
        self.assertIsNone(dwd_pollen.fetch_dwd_pollen())
        self.assertFalse(dwd_pollen.should_refresh_dwd_pollen())
        self.assertEqual(self.calls, 1)

    def test_every_option_matches_the_feed_format(self) -> None:
        from modules.dwd_weather import SETTINGS_FIELDS
        options = next(f["options"] for f in SETTINGS_FIELDS if f["name"] == "DWD_POLLEN_REGION")
        for value, _ in options[1:]:
            self.assertIsNotNone(dwd_pollen.parse_region_key(value), value)
        self.assertIn("90:92", [v for v, _ in options])


# ---------------------------------------------------------------------------
# Anzeige-Seite und /metrics: nur aus dem Cache
# ---------------------------------------------------------------------------

class DisplayStateCacheOnlyTest(unittest.TestCase):
    def setUp(self) -> None:
        garbage_ds.clear_cache()
        fuel_ds.clear_cache()
        config.apply_runtime_config({**fuel_settings(), "IDLE_MODULES": "garbage",
                                     "GARBAGE_ICS_URLS": "Zuhause|https://kommune.test/abfuhr-{year}.ics"})

    def tearDown(self) -> None:
        garbage_ds.clear_cache()
        fuel_ds.clear_cache()
        config.apply_runtime_config()

    def test_summaries_do_not_fetch_even_for_switched_off_content(self) -> None:
        from app.display_api import build_display_state
        calls: list = []

        def recording(url, *a, **kw):
            calls.append(url)
            raise AssertionError("kein Netzzugriff erwartet")

        with patch.object(http_client.HTTP_SESSION, "get", side_effect=recording):
            state = build_display_state({}, {}, (60, "Test"))
            response = server.app.test_client().get("/metrics")
        self.assertEqual(calls, [])
        self.assertEqual(response.status_code, 200)
        by_id = {m["id"]: m for m in state["content"]}
        self.assertIn("1 Adresse", by_id["garbage"]["summary"])
        self.assertEqual(by_id["fuel_prices"]["summary"], "Preise noch nicht geladen", "abgeschaltet und ohne Stand: kein Abruf")


if __name__ == "__main__":
    unittest.main()
