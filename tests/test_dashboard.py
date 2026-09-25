"""
Tests für den Dashboard-Modus: Kachel-Layout, Komposition mit Fake-Modulen,
Einbindung in render_if_changed, Vorschau und ein Smoke-Test mit den echten
Modulen und API-Fixtures.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.config as config
import app.dashboard as dashboard
import app.http_client as http_client
import app.server as server
from app.module_base import InkwallModule


class _Tile(InkwallModule):
    MODULE_PRIORITY = 100

    def __init__(self, module_id: str, content="x", supports_tile: bool = True, color=(200, 40, 40)):
        self.MODULE_ID = module_id
        self.MODULE_NAME = module_id.title()
        self._content = content
        self._supports = supports_tile
        self._color = color
        self.tile_sizes: list[tuple[int, int]] = []

    def is_enabled(self, env):
        return True

    def fetch_content(self, env):
        return self._content

    def render(self, env, content):
        return Image.new("RGB", (config.get_cfg().render_width, config.get_cfg().render_height), self._color)

    def render_tile(self, env, content, width, height):
        if not self._supports:
            return None
        self.tile_sizes.append((width, height))
        return Image.new("RGB", (width, height), self._color)

    def get_state_key(self, content):
        return str(content)


def _cfg(**over):
    settings = dict(config.read_env_settings())
    settings.update({"RENDER_WIDTH": "600", "RENDER_HEIGHT": "900", "DISPLAY_ROTATION": "0", "DISPLAY_THEME": "eink"})
    settings.update(over)
    config.apply_runtime_config(settings)
    return config.get_cfg()


class TileLayoutTest(unittest.TestCase):
    def test_parse_tiles(self) -> None:
        self.assertEqual(config.parse_dashboard_tiles("dwd_weather:45, calendar:30, garbage"),
                         (("dwd_weather", 45), ("calendar", 30), ("garbage", 0)))
        self.assertEqual(config.parse_dashboard_tiles(" ,x:abc, x:10"), (("x", 0),))

    def test_layout_fills_to_100(self) -> None:
        r = dashboard.resolve_tile_layout((("a", 50), ("b", 0), ("c", 0)), ["a", "b", "c"])
        self.assertEqual(r, [("a", 50), ("b", 25), ("c", 25)])
        r = dashboard.resolve_tile_layout((("a", 30), ("b", 30)), ["a", "b"])
        self.assertEqual(r, [("a", 30), ("b", 70)], "Rest geht an die letzte Kachel")
        r = dashboard.resolve_tile_layout((), ["a", "b", "c"])
        self.assertEqual(r, [("a", 33), ("b", 33), ("c", 33)])
        r = dashboard.resolve_tile_layout((("a", 80), ("zzz", 20)), ["a", "b"])
        self.assertEqual(r, [("a", 100)], "unbekannte Module fallen raus")
        r = dashboard.resolve_tile_layout((("a", 90), ("b", 90)), ["a", "b"])
        self.assertEqual(sum(p for _, p in r), 100)


class ComposeTest(unittest.TestCase):
    def tearDown(self) -> None:
        config.apply_runtime_config()

    def test_compose_stacks_tiles_with_header_and_state_key(self) -> None:
        cfg = _cfg(DASHBOARD_TILES="a:60, b:40", IDLE_LAYOUT="dashboard")
        a, b = _Tile("a", "c1", color=(200, 0, 0)), _Tile("b", "c2", color=(0, 0, 200))
        result = dashboard.compose_dashboard({}, cfg, [a, b])
        self.assertIsNotNone(result)
        img, key = result
        self.assertEqual(img.size, (600, 900))
        self.assertTrue(key.startswith("dashboard:"))
        self.assertIn("a=c1", key)
        self.assertIn("b=c2", key)
        # Kachelhöhen: 60/40 des Bereichs unter der Kopfzeile
        (aw, ah), = a.tile_sizes
        (bw, bh), = b.tile_sizes
        self.assertEqual(aw, 600)
        self.assertAlmostEqual(ah / (ah + bh), 0.6, delta=0.02)
        # Pixel in der oberen Kachel rot, in der unteren blau
        self.assertEqual(img.getpixel((300, 200)), (200, 0, 0))
        self.assertEqual(img.getpixel((300, 850)), (0, 0, 200))

    def test_module_without_content_gets_placeholder_tile(self) -> None:
        cfg = _cfg(DASHBOARD_TILES="a:50, b:50", IDLE_LAYOUT="dashboard")
        a, b = _Tile("a", None), _Tile("b", "ok", color=(0, 120, 0))
        img, key = dashboard.compose_dashboard({}, cfg, [a, b])
        self.assertIn("a=none", key)
        self.assertEqual(img.getpixel((300, 850)), (0, 120, 0))

    def test_all_empty_returns_none(self) -> None:
        cfg = _cfg(DASHBOARD_TILES="a, b", IDLE_LAYOUT="dashboard")
        self.assertIsNone(dashboard.compose_dashboard({}, cfg, [_Tile("a", None), _Tile("b", None)]))

    def test_module_without_tile_support_is_skipped(self) -> None:
        cfg = _cfg(DASHBOARD_TILES="a, b", IDLE_LAYOUT="dashboard")
        a, b = _Tile("a", "x", supports_tile=False), _Tile("b", "y", color=(9, 9, 9))
        img, key = dashboard.compose_dashboard({}, cfg, [a, b])
        self.assertNotIn("a=", key)
        self.assertIn("b=y", key)


class ServerIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self._tmpdir = tempfile.TemporaryDirectory()
        tmp = Path(self._tmpdir.name)
        self._patches = [
            patch.object(server, "CURRENT_IMAGE_PATH", tmp / "current.png"),
            patch.object(server, "CURRENT_BMP_PATH", tmp / "current.bmp"),
            patch.object(server, "STATE_PATH", tmp / "state.txt"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        self._tmpdir.cleanup()
        config.apply_runtime_config()

    def test_render_if_changed_uses_dashboard_when_configured(self) -> None:
        _cfg(IDLE_LAYOUT="dashboard", DASHBOARD_TILES="a:50, b:50", OUTPUT_FORMAT="png")
        a, b = _Tile("a", "c1"), _Tile("b", "c2", color=(0, 0, 200))
        with patch.object(server._registry, "get_priority_modules", return_value=[]), \
             patch.object(server._registry, "get_idle_modules", return_value=[a, b]):
            key = server.render_if_changed(None)
            key2 = server.render_if_changed(key)
        self.assertTrue(key.startswith("dashboard:"))
        self.assertEqual(key, key2)
        self.assertEqual(server._esp32_state["media_type"], "dashboard")
        self.assertEqual(len(a.tile_sizes), 1, "zweiter Tick holt nur die Inhalte, die Kacheln werden nicht neu gerendert")

    def test_rotation_when_layout_is_rotation(self) -> None:
        _cfg(IDLE_LAYOUT="rotation", OUTPUT_FORMAT="png")
        a = _Tile("a", "c1")
        with patch.object(server._registry, "get_priority_modules", return_value=[]), \
             patch.object(server._registry, "get_idle_modules", return_value=[a]):
            key = server.render_if_changed(None)
        self.assertTrue(key.startswith("a:c1:"))
        self.assertEqual(a.tile_sizes, [])

    def test_preview_endpoint_renders_dashboard(self) -> None:
        _cfg(IDLE_LAYOUT="dashboard", DASHBOARD_TILES="a, b")
        a, b = _Tile("a", "c1"), _Tile("b", "c2")
        with patch.object(server._registry, "get_idle_modules", return_value=[a, b]), \
             patch.object(server._registry, "get_module_by_id", return_value=None):
            response = server.app.test_client().get("/api/preview/dashboard.png?theme=light")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "image/png")


class RealModulesSmokeTest(unittest.TestCase):
    """Alle echten Module mit Kachel-Support in einem Dashboard, drei Themes."""

    def setUp(self) -> None:
        from tests.test_render_smoke import _fake_http_get, _pollen_region_key, _uv_city, _clear_module_caches
        self._tmp = tempfile.TemporaryDirectory()
        gallery = Path(self._tmp.name) / "g"
        gallery.mkdir()
        Image.new("RGB", (640, 480), (120, 60, 30)).save(gallery / "a.jpg")
        self.settings = {
            "RENDER_WIDTH": "1200", "RENDER_HEIGHT": "1600",
            "IDLE_LAYOUT": "dashboard",
            "DASHBOARD_TILES": "dwd_weather:40, tagesschau:35, gallery:25",
            "IDLE_MODULES": "dwd_weather,tagesschau,gallery",
            "DWD_POLLEN_REGION": _pollen_region_key(), "DWD_POLLEN_ALLERGENS": "Graeser,Birke",
            "DWD_UV_CITY": _uv_city(), "GALLERY_PATHS": str(gallery),
        }
        self._http = patch.object(http_client.HTTP_SESSION, "get", side_effect=_fake_http_get)
        self._http.start()
        _clear_module_caches()

    def tearDown(self) -> None:
        self._http.stop()
        self._tmp.cleanup()
        config.apply_runtime_config()

    def test_dashboard_with_real_modules(self) -> None:
        import app.module_registry as registry
        for theme in config.AVAILABLE_THEMES:
            cfg = _cfg(DISPLAY_THEME=theme, **self.settings)
            env = config.get_settings_values()
            modules = [m for m in registry.get_idle_modules() if m.is_enabled(env)]
            self.assertEqual({m.MODULE_ID for m in modules}, {"dwd_weather", "tagesschau", "gallery"})
            result = dashboard.compose_dashboard(env, cfg, server._dashboard_modules(modules, cfg))
            self.assertIsNotNone(result, theme)
            img, key = result
            self.assertEqual(img.size, (1200, 1600))
            for mid in ("dwd_weather", "tagesschau", "gallery"):
                self.assertIn(f"{mid}=", key)
                self.assertNotIn(f"{mid}=none", key)


if __name__ == "__main__":
    unittest.main()
