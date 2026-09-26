"""
Tests für die zentrale Render-Pipeline in app/server.py:
- render_if_changed: Prioritäts- vs. Idle-Pfad, Retry nach Render-Fehler
- _save_image: atomares Schreiben, Hash aus Bytes, State-Update erst danach
- request_render: weckt den Worker und wartet auf den Abschluss
"""

from __future__ import annotations

import hashlib
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.server as server
from app.module_base import InkwallModule


# ---------------------------------------------------------------------------
# Fake-Module
# ---------------------------------------------------------------------------

class _FakeModule(InkwallModule):
    MODULE_ID = "fake"
    MODULE_NAME = "Fake"
    MODULE_DESCRIPTION = "test"
    MODULE_PRIORITY = 100

    def __init__(self, module_id: str, priority: int, content=None, fail_render: bool = False):
        self.MODULE_ID = module_id
        self.MODULE_PRIORITY = priority
        self._content = content
        self._fail_render = fail_render
        self.render_calls = 0

    def is_enabled(self, env):
        return True

    def fetch_content(self, env):
        return self._content

    def render(self, env, content):
        self.render_calls += 1
        if self._fail_render:
            raise RuntimeError("render kaputt")
        return Image.new("RGB", (4, 4), (255, 0, 0))

    def should_refresh(self, env):
        return False

    def get_state_key(self, content):
        return str(content)


def _paths(tmp: Path) -> dict:
    return {
        "CURRENT_IMAGE_PATH": tmp / "current.png",
        "CURRENT_BMP_PATH":   tmp / "current.bmp",
        "CURRENT_EPD_PATH":   tmp / "current.epd",
        "STATE_PATH":         tmp / "state.txt",
        "CONTENT_HASH_PATH":  tmp / "content_hash.txt",
    }


class _PipelineTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmpdir.name)
        self._patches = [patch.object(server, k, v) for k, v in _paths(self.tmp).items()]
        for p in self._patches:
            p.start()
        # Immer PNG-Ausgabe, keine Rotation – unabhängig von der lokalen Config
        self._cfg_patch = patch.object(server, "get_cfg", return_value=_FakeCfg())
        self._cfg_patch.start()
        server._esp32_state = {"hash": "", "format": "png", "state": "idle", "media_type": "idle", "rendered_at": ""}

    def tearDown(self) -> None:
        self._cfg_patch.stop()
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()


class _FakeCfg:
    output_format = "png"
    display_rotation = 0
    display_theme = "dark"
    show_render_time = False
    render_width = 400
    render_height = 300
    idle_module_rotation_seconds = 300
    idle_layout = "rotation"
    dashboard_tiles = ()
    refresh_interval = 60
    night_mode_enabled = False
    night_mode_start_minutes = 0
    night_mode_end_minutes = 0
    timezone = "Europe/Berlin"


def _with_modules(priority: list, idle: list):
    return (
        patch.object(server._registry, "get_priority_modules", return_value=priority),
        patch.object(server._registry, "get_idle_modules", return_value=idle),
        patch.object(server, "get_settings_values", return_value={}),
    )


# ---------------------------------------------------------------------------
# _save_image
# ---------------------------------------------------------------------------

class SaveImageTest(_PipelineTestBase):
    def test_render_time_stamp_is_optional_and_skips_dashboard(self) -> None:
        blank = Image.new("RGB", (600, 800), (30, 30, 30))
        server._save_image(blank, "fake:plain", "fake")
        plain = Image.open(self.tmp / "current.png").convert("RGB").copy()

        class _Stamped(_FakeCfg):
            show_render_time = True

        with patch.object(server, "get_cfg", return_value=_Stamped()):
            server._save_image(blank, "fake:stamped", "fake")
            stamped = Image.open(self.tmp / "current.png").convert("RGB").copy()
            server._save_image(blank, "dash:stamped", "dashboard")
            dashboard = Image.open(self.tmp / "current.png").convert("RGB").copy()
        # Oben rechts steht jetzt die Pille, der Rest ist unverändert
        self.assertNotEqual(plain.crop((400, 0, 600, 60)).tobytes(), stamped.crop((400, 0, 600, 60)).tobytes())
        self.assertEqual(plain.crop((0, 100, 600, 800)).tobytes(), stamped.crop((0, 100, 600, 800)).tobytes())
        self.assertEqual(dashboard.tobytes(), plain.tobytes(), "Dashboard bekommt keine zweite Uhrzeit")

    def test_stamp_alone_is_no_change(self) -> None:
        """Nur die Uhrzeit ist anders → gleicher Hash, altes Bild (mit alter Uhrzeit) bleibt."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        class _Stamped(_FakeCfg):
            show_render_time = True

        berlin = ZoneInfo("Europe/Berlin")
        img = Image.new("RGB", (600, 800), (30, 30, 30))
        with patch.object(server, "get_cfg", return_value=_Stamped()):
            with patch.object(server, "_get_local_now", return_value=_dt(2026, 9, 26, 10, 0, tzinfo=berlin)):
                server._save_image(img, "fake:a", "fake")
            first_hash = server._esp32_state["hash"]
            first_bytes = (self.tmp / "current.png").read_bytes()
            with patch.object(server, "_get_local_now", return_value=_dt(2026, 9, 26, 10, 7, tzinfo=berlin)):
                server._save_image(img.copy(), "fake:b", "fake")
            self.assertEqual(server._esp32_state["hash"], first_hash, "nur die Uhrzeit anders: kein neues Bild fürs Gerät")
            self.assertEqual((self.tmp / "current.png").read_bytes(), first_bytes, "das Bild mit der alten Uhrzeit bleibt")
            self.assertEqual(server._esp32_state["state"], "fake:b")
            with patch.object(server, "_get_local_now", return_value=_dt(2026, 9, 26, 10, 9, tzinfo=berlin)):
                server._save_image(Image.new("RGB", (600, 800), (200, 30, 30)), "fake:c", "fake")
            self.assertNotEqual(server._esp32_state["hash"], first_hash, "anderer Inhalt: neues Bild")

    def test_stamp_comparison_survives_a_restart(self) -> None:
        """Nach dem Neustart des Containers: gleicher Inhalt, andere Uhrzeit → kein neues Bild fürs Gerät."""
        from datetime import datetime as _dt
        from zoneinfo import ZoneInfo

        class _Stamped(_FakeCfg):
            show_render_time = True

        berlin = ZoneInfo("Europe/Berlin")
        img = Image.new("RGB", (600, 800), (30, 30, 30))
        with patch.object(server, "get_cfg", return_value=_Stamped()):
            with patch.object(server, "_get_local_now", return_value=_dt(2026, 9, 26, 8, 55, tzinfo=berlin)):
                server._save_image(img, "fake:a", "fake")
            before = server._esp32_state["hash"]
            png = (self.tmp / "current.png").read_bytes()
            # Neustart: Zustand weg, von der Platte übernehmen
            server._esp32_state = {}
            with patch.object(server._registry, "get_module_by_id", return_value=object()):
                server._restore_render_state()
            self.assertEqual(server._esp32_state["hash"], before)
            with patch.object(server, "_get_local_now", return_value=_dt(2026, 9, 26, 8, 57, tzinfo=berlin)):
                server._save_image(img.copy(), "fake:a", "fake")
        self.assertEqual(server._esp32_state["hash"], before, "nach dem Neustart kein neues Bild")
        self.assertEqual((self.tmp / "current.png").read_bytes(), png)

    def test_writes_png_atomically_and_hashes_bytes(self) -> None:
        img = Image.new("RGB", (4, 4), (0, 255, 0))
        server._save_image(img, "fake:1", "fake")

        png_path = self.tmp / "current.png"
        self.assertTrue(png_path.exists())
        self.assertFalse((self.tmp / "current.png.tmp").exists(), "tmp-Datei muss weg sein")

        expected_hash = hashlib.md5(png_path.read_bytes()).hexdigest()
        self.assertEqual(server._esp32_state["hash"], expected_hash)
        self.assertEqual(server._esp32_state["state"], "fake:1")
        self.assertEqual(server._esp32_state["media_type"], "fake")
        self.assertEqual((self.tmp / "state.txt").read_text(encoding="utf-8"), "fake:1")

    def test_state_is_not_updated_when_write_fails(self) -> None:
        img = Image.new("RGB", (4, 4), (0, 255, 0))
        with patch.object(server, "_atomic_write_bytes", side_effect=OSError("disk voll")):
            with self.assertRaises(OSError):
                server._save_image(img, "fake:2", "fake")
        self.assertEqual(server._esp32_state["state"], "idle", "State darf bei Schreibfehler nicht umschalten")


# ---------------------------------------------------------------------------
# render_if_changed
# ---------------------------------------------------------------------------

class RenderIfChangedTest(_PipelineTestBase):
    def test_priority_module_wins_over_idle(self) -> None:
        prio = _FakeModule("plexlike", 1, content="playing")
        idle = _FakeModule("idlelike", 100, content="news")
        p1, p2, p3 = _with_modules([prio], [idle])
        with p1, p2, p3:
            key = server.render_if_changed(None)
        self.assertEqual(key, "plexlike:playing")
        self.assertEqual(prio.render_calls, 1)
        self.assertEqual(idle.render_calls, 0)

    def test_priority_without_content_falls_back_to_idle(self) -> None:
        prio = _FakeModule("plexlike", 1, content=None)
        idle = _FakeModule("idlelike", 100, content="news")
        p1, p2, p3 = _with_modules([prio], [idle])
        with p1, p2, p3:
            key = server.render_if_changed(None)
        self.assertTrue(key.startswith("idlelike:news:"))
        self.assertEqual(idle.render_calls, 1)

    def test_unchanged_state_does_not_rerender(self) -> None:
        idle = _FakeModule("idlelike", 100, content="news")
        p1, p2, p3 = _with_modules([], [idle])
        with p1, p2, p3:
            key1 = server.render_if_changed(None)
            key2 = server.render_if_changed(key1)
        self.assertEqual(key1, key2)
        self.assertEqual(idle.render_calls, 1, "gleicher State-Key darf nicht erneut rendern")

    def test_failed_render_keeps_previous_key_so_next_tick_retries(self) -> None:
        broken = _FakeModule("broken", 1, content="x", fail_render=True)
        p1, p2, p3 = _with_modules([broken], [])
        with p1, p2, p3:
            key = server.render_if_changed("previous:key")
            self.assertEqual(key, "previous:key", "bei Render-Fehler alten Key behalten")
            key = server.render_if_changed(key)
        self.assertEqual(broken.render_calls, 2, "nächster Tick muss es erneut versuchen")

    def test_no_modules_renders_placeholder_once(self) -> None:
        p1, p2, p3 = _with_modules([], [])
        with p1, p2, p3:
            key1 = server.render_if_changed(None)
            key2 = server.render_if_changed(key1)
        self.assertEqual(key1, "__no_content__")
        self.assertEqual(key2, "__no_content__")
        self.assertTrue((self.tmp / "current.png").exists())

    def test_concurrent_renders_are_serialized(self) -> None:
        """Zwei Threads rendern gleichzeitig – der Lock muss sie nacheinander ausführen."""
        active = {"count": 0, "max": 0}
        lock = threading.Lock()

        class _SlowModule(_FakeModule):
            def render(self, env, content):
                with lock:
                    active["count"] += 1
                    active["max"] = max(active["max"], active["count"])
                import time
                time.sleep(0.05)
                with lock:
                    active["count"] -= 1
                return Image.new("RGB", (4, 4), (0, 0, 255))

        slow = _SlowModule("slow", 1, content="x")
        p1, p2, p3 = _with_modules([slow], [])
        with p1, p2, p3:
            threads = [threading.Thread(target=server.render_if_changed, args=(None,)) for _ in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(active["max"], 1, "Render-Lock muss parallele Renders verhindern")


# ---------------------------------------------------------------------------
# request_render + Worker-Zyklus
# ---------------------------------------------------------------------------

class RequestRenderTest(_PipelineTestBase):
    def test_request_render_wakes_worker_and_waits_for_completion(self) -> None:
        idle = _FakeModule("idlelike", 100, content="news")
        p1, p2, p3 = _with_modules([], [idle])

        # Simulierter Worker: wartet auf das Event, führt genau einen Zyklus aus
        def fake_worker():
            server._wake_event.wait(timeout=5)
            server._wake_event.clear()
            with p1, p2, p3:
                server._run_worker_cycle(None)

        server._wake_event.clear()
        t = threading.Thread(target=fake_worker, daemon=True)
        t.start()

        completed = server.request_render(wait_seconds=5)
        t.join(timeout=5)

        self.assertTrue(completed, "request_render muss auf den Worker-Abschluss warten")
        self.assertEqual(idle.render_calls, 1)
        self.assertTrue(server._esp32_state["state"].startswith("idlelike:news:"))

    def test_request_render_without_wait_returns_immediately(self) -> None:
        server._wake_event.clear()
        result = server.request_render()
        self.assertFalse(result)
        self.assertTrue(server._wake_event.is_set(), "Event muss gesetzt sein")
        with server._render_cond:
            self.assertTrue(server._force_render_requested)
        # Aufräumen
        with server._render_cond:
            server._force_render_requested = False
        server._wake_event.clear()


if __name__ == "__main__":
    unittest.main()
