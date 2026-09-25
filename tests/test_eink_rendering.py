"""
Tests fürs Rendern auf das Panel:
- E-Ink-Theme: flache Seiten liegen zu 100 % auf den sechs Spectra-Farben
  (Text ohne Kantenglättung, Warnungen und Pollen deckend) – jeder andere
  Pixel würde beim Dithern zu einem bunten Punkt
- kleine Bilder: Tagesschau stürzt nicht ab, Warnungen verdrängen den Verlauf nicht
- Textsatz: Zeilenumbruch kürzt überlange Wörter, ellipsize, Maßstab
- Plex/Steam-Cover lassen im Querformat Platz für den Text
- Bildmaße sind immer gerade (kompaktes Panel-Format)
"""

from __future__ import annotations

import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch
from zoneinfo import ZoneInfo

from PIL import Image, ImageDraw

import app.config as config
import app.http_client as http_client
from app.image_rendering import COVER_TEXT_RESERVE, SPECTRA6_COLORS, create_flat_cover_canvas, create_light_cover_canvas
from app.text_rendering import ellipsize, new_draw, page_scale, wrap_text
from tests.test_render_smoke import _clear_module_caches, _fake_http_get, _pollen_region_key, _uv_city

PALETTE = set(SPECTRA6_COLORS.values())


def _off_palette(img: Image.Image) -> int:
    img = img.convert("RGB")
    total = img.width * img.height
    return total - sum(count for count, color in (img.getcolors(total) or []) if color in PALETTE)


def _warnings() -> list[dict]:
    now_ms = int(time.time() * 1000)
    return [
        {"level": 3, "event": "STURMBÖEN", "headline": "Amtliche WARNUNG vor STURMBÖEN mit einem sehr langen Titel, der nicht passt",
         "description": "Es treten Sturmböen mit Geschwindigkeiten um 70 km/h auf.", "start": "12:00", "end": "18:00",
         "_start_ms": now_ms, "_end_ms": now_ms + 7_200_000},
        {"level": 2, "event": "GLÄTTE", "headline": "Amtliche WARNUNG vor GLÄTTE", "description": "Glätte durch überfrierende Nässe.",
         "start": "18:00", "end": "08:00", "_start_ms": now_ms, "_end_ms": now_ms + 7_200_000},
        {"level": 1, "event": "FROST", "headline": "Amtliche WARNUNG vor FROST", "description": "Leichter Frost.",
         "start": "22:00", "end": "08:00", "_start_ms": now_ms, "_end_ms": now_ms + 7_200_000},
    ]


class _RenderBase(unittest.TestCase):
    width, height, theme = 1200, 1600, "eink"

    @classmethod
    def setUpClass(cls) -> None:
        settings = dict(config.read_env_settings())
        settings.update({
            "RENDER_WIDTH": str(cls.width), "RENDER_HEIGHT": str(cls.height), "DISPLAY_ROTATION": "0",
            "DISPLAY_THEME": cls.theme, "OUTPUT_FORMAT": "png", "TIMEZONE": "Europe/Berlin",
            "IDLE_MODULES": "dwd_weather,tagesschau,departures,fuel_prices",
            "DEPARTURES_STOPS": "Alex|900100003",
            "FUEL_API_KEY": "12345678-1234-1234-1234-123456789abc", "FUEL_LOCATION": "50.5556, 8.5045",
            "DWD_WEATHER_STATION_ID": "10532", "DWD_POLLEN_REGION": _pollen_region_key(),
            "DWD_POLLEN_ALLERGENS": "Graeser,Birke,Beifuss", "DWD_UV_CITY": _uv_city(),
        })
        config.apply_runtime_config(settings)
        cls.env = config.get_settings_values()
        cls._http = patch.object(http_client.HTTP_SESSION, "get", side_effect=_fake_http_get)
        cls._http.start()
        _clear_module_caches()

    @classmethod
    def tearDownClass(cls) -> None:
        cls._http.stop()
        _clear_module_caches()
        config.apply_runtime_config()

    def _weather(self, warnings: bool = False) -> dict:
        from modules.dwd_weather import module
        content = dict(module.fetch_content(self.env))
        if warnings:
            content["warnings"] = _warnings()
        return content


class EinkPaletteTest(_RenderBase):
    """1200 × 1600, das Format des 13,3-Zoll-Panels."""

    def test_weather_page_and_tile_are_pure_palette(self) -> None:
        from modules.dwd_weather import module
        for content in (self._weather(), self._weather(warnings=True)):
            self.assertEqual(_off_palette(module.render(self.env, content)), 0)
        self.assertEqual(_off_palette(module.render_tile(self.env, self._weather(True), 1200, 520)), 0)

    def test_departures_fuel_placeholder_and_stamp_are_pure_palette(self) -> None:
        import app.server as server
        from app.image_rendering import stamp_render_time
        from modules.departures import data_source as dep_ds
        from modules.departures import module as departures
        from modules.fuel_prices import module as fuel
        dep_ds.clear_cache()
        with patch.object(dep_ds, "now_local", return_value=datetime(2026, 9, 3, 23, 10, tzinfo=ZoneInfo("Europe/Berlin"))):
            dep_content = departures.fetch_content(self.env)
        images = {
            "departures": departures.render(self.env, dep_content),
            "departures_tile": departures.render_tile(self.env, dep_content, 1200, 500),
            "fuel": fuel.render(self.env, fuel.fetch_content(self.env)),
            "placeholder": server.render_no_content_image(),
            "stamp": stamp_render_time(Image.new("RGB", (1200, 1600), SPECTRA6_COLORS["white"]), "eink", "Stand 12:34"),
        }
        for name, img in images.items():
            self.assertEqual(_off_palette(img), 0, name)
        dep_ds.clear_cache()

    def test_news_text_is_pure_palette_outside_the_photos(self) -> None:
        from modules.tagesschau import module
        content = module.fetch_content(self.env)
        with patch("modules.tagesschau.data_source.fetch_tagesschau_image", return_value=None):
            img = module.render(self.env, content)
        self.assertEqual(_off_palette(img), 0, "ohne Fotos bleibt nichts zum Dithern")

    def test_eink_text_has_no_antialiasing(self) -> None:
        img = Image.new("RGB", (300, 80), SPECTRA6_COLORS["white"])
        new_draw(img, flat=True).text((10, 10), "Grüße 12:34", font=config.load_font(40, True), fill=SPECTRA6_COLORS["black"])
        self.assertEqual({color for _, color in img.getcolors(1000)}, {SPECTRA6_COLORS["white"], SPECTRA6_COLORS["black"]})
        soft = Image.new("RGB", (300, 80), SPECTRA6_COLORS["white"])
        new_draw(soft, flat=False).text((10, 10), "Grüße 12:34", font=config.load_font(40, True), fill=SPECTRA6_COLORS["black"])
        self.assertGreater(len(soft.getcolors(10000)), 2, "andere Themes behalten die Kantenglättung")


class SmallPanelTest(_RenderBase):
    width, height = 800, 480

    def test_tagesschau_renders_on_small_panels(self) -> None:
        from app.module_services import ModuleRenderServices
        from modules.tagesschau import module
        from modules.tagesschau.renderer import render_tagesschau_module
        content = module.fetch_content(self.env)
        for size in ((800, 480), (600, 448), (400, 300)):
            services = ModuleRenderServices(render_width=size[0], render_height=size[1],
                                            display_theme="eink", load_font=config.load_font)
            self.assertEqual(render_tagesschau_module(services, content).size, size)

    def test_warnings_leave_room_for_the_forecast(self) -> None:
        from modules.dwd_weather import module
        from modules.dwd_weather.renderer import warning_card_limit, warning_strip_height
        img = module.render(self.env, self._weather(warnings=True))
        self.assertEqual(img.size, (800, 480))
        self.assertEqual(_off_palette(img), 0)
        # Die untersten Zeilen tragen die Tagesvorschau: dort gibt es schwarze Konturen
        bottom = img.crop((0, 400, 800, 470))
        self.assertIn(SPECTRA6_COLORS["black"], {c for _, c in bottom.getcolors(100000)})
        self.assertLess(warning_strip_height(_warnings(), 700, config.load_font, 0.4),
                        warning_strip_height(_warnings(), 700, config.load_font, 1.0), "die Leiste skaliert mit")
        self.assertEqual(warning_card_limit(_warnings(), 700, config.load_font, 0.4, budget=60), 1)


class TextTest(unittest.TestCase):
    def setUp(self) -> None:
        self.draw = ImageDraw.Draw(Image.new("RGB", (10, 10)))
        self.font = config.load_font(24, False)

    def test_long_word_is_ellipsized(self) -> None:
        lines = wrap_text(self.draw, "S+U Mönchengladbach-Rheydt-Odenkirchen-Süd", self.font, 200)
        self.assertTrue(lines)
        for line in lines:
            self.assertLessEqual(self.draw.textlength(line, font=self.font), 200)
        self.assertTrue(any(line.endswith("…") for line in lines))

    def test_max_lines_ends_with_ellipsis_and_fits(self) -> None:
        text = "Ein ziemlich langer Satz, der auf keinen Fall in zwei kurze Zeilen passt, egal wie man ihn dreht"
        lines = wrap_text(self.draw, text, self.font, 180, max_lines=2)
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[-1].endswith("…"))
        self.assertTrue(all(self.draw.textlength(line, font=self.font) <= 180 for line in lines))

    def test_short_text_is_unchanged(self) -> None:
        self.assertEqual(wrap_text(self.draw, "Kurz und gut", self.font, 400), ["Kurz und gut"])
        self.assertEqual(ellipsize(self.draw, "Kurz", self.font, 400), "Kurz")
        cut = ellipsize(self.draw, "Hauptbahnhof Frankfurt am Main", self.font, 120)
        self.assertTrue(cut.endswith("…"))
        self.assertLessEqual(self.draw.textlength(cut, font=self.font), 120)

    def test_page_scale(self) -> None:
        self.assertEqual(page_scale(1200, 1600), 1.0)
        self.assertEqual(page_scale(1600, 1200), 1.0)
        self.assertAlmostEqual(page_scale(800, 480), 0.4)
        self.assertEqual(page_scale(200, 100), 0.35)


class CoverTest(unittest.TestCase):
    poster = Image.new("RGB", (400, 600), (120, 40, 40))

    def test_portrait_cover_is_unchanged(self) -> None:
        _, bottom = create_light_cover_canvas(self.poster, 1200, 1600)
        self.assertEqual(bottom, 1264)

    def test_landscape_leaves_room_for_the_text(self) -> None:
        for factory in (create_light_cover_canvas, create_flat_cover_canvas):
            canvas, bottom = factory(self.poster, 1600, 1200)
            self.assertEqual(canvas.size, (1600, 1200))
            self.assertLessEqual(bottom, 1200 - COVER_TEXT_RESERVE, factory.__name__)


class EvenSizeTest(unittest.TestCase):
    def tearDown(self) -> None:
        config.apply_runtime_config()

    def test_odd_render_size_is_rounded_down(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(), "RENDER_WIDTH": "1201", "RENDER_HEIGHT": "1599",
                                     "DISPLAY_ROTATION": "90"})
        cfg = config.get_cfg()
        self.assertEqual((cfg.base_render_width, cfg.base_render_height), (1200, 1598))
        self.assertEqual(cfg.render_width % 2, 0)


class GalleryDecodeTest(unittest.TestCase):
    def test_large_photo_is_decoded_small_and_fills_the_page(self) -> None:
        from app.module_services import ModuleRenderServices
        from modules.gallery.renderer import render_gallery_image
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "gross.jpg"
            Image.new("RGB", (4800, 3200), (30, 140, 200)).save(path, quality=80)
            services = ModuleRenderServices(render_width=600, render_height=800, display_theme="eink", load_font=config.load_font)
            opened: list = []
            real_open = Image.open

            def spy(fp, *a, **kw):
                img = real_open(fp, *a, **kw)
                opened.append(img)
                return img

            with patch("modules.gallery.renderer.Image.open", side_effect=spy):
                img = render_gallery_image(services, {"image_path": str(path), "filename": "gross.jpg", "folder": tmp}, "cover", "none")
        self.assertEqual(img.size, (600, 800))
        self.assertLessEqual(max(opened[0].size), 2400, "JPEG wird verkleinert dekodiert")


if __name__ == "__main__":
    unittest.main()
