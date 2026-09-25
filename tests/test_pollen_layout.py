"""
Tests für den Pollen-Strip des Wetter-Moduls: Wahl zwischen Raster und Chips
nach Höhenbudget, keine unsichtbaren Werte im E-Ink-Theme (hohler Kreis und
schwarzer Text für „0“), Kachel mit Pollen.
"""

from __future__ import annotations

import unittest

from PIL import Image

import app.config as config
from app.image_rendering import SPECTRA6_COLORS
from app.module_services import ModuleRenderServices
from modules.dwd_weather import renderer


def _days(today, tomorrow, after):
    return {"today": today, "tomorrow": tomorrow, "dayafter_to": after}


BUSY = {
    "Birke": _days(0.5, 0.5, 0.5), "Esche": _days(0.5, 1.0, 0.5), "Hasel": _days(0.5, 0.5, 0.5),
    "Erle": _days(0.5, 1.0, 0.5), "Graeser": _days(0.5, 0.5, 2.0), "Roggen": _days(0.5, 1.0, 0.5),
    "Beifuss": _days(0.5, 0.5, 0.5), "Ambrosia": _days(0.0, 0.0, 0.0),
}


class PlanTest(unittest.TestCase):
    def test_grid_when_budget_allows_chips_when_not(self) -> None:
        grid = renderer.plan_pollen_layout(BUSY, 1116, None)
        self.assertEqual(grid["mode"], "grid")
        self.assertEqual(grid["height"], renderer.pollen_strip_height(BUSY))
        chips = renderer.plan_pollen_layout(BUSY, 1116, grid["height"] - 1)
        self.assertEqual(chips["mode"], "chips")
        self.assertLess(chips["height"], grid["height"])
        shown = [a for row in chips["rows"] for a in row]
        self.assertEqual(shown[:7], [a for a in BUSY if a != "Ambrosia"], "aktive zuerst, in Reihenfolge")
        self.assertIn("Ambrosia", shown)
        self.assertEqual(chips["dropped"], [])

    def test_chips_drop_only_inactive_rows_when_too_tall(self) -> None:
        # Schmal: viele Zeilen. Budget für eine Zeile → inaktive Zeilen fallen weg, aktive bleiben
        plan = renderer.plan_pollen_layout(BUSY, 420, 120)
        self.assertEqual(plan["mode"], "chips")
        shown = [a for row in plan["rows"] for a in row]
        for a in (a for a in BUSY if a != "Ambrosia"):
            self.assertIn(a, shown, a)
        self.assertNotIn("Ambrosia", shown)
        self.assertEqual(plan["dropped"], ["Ambrosia"])

    def test_text_and_none_modes(self) -> None:
        self.assertEqual(renderer.plan_pollen_layout({}, 1116, None)["mode"], "none")
        quiet = {"Birke": _days(0.0, 0.0, None)}
        self.assertEqual(renderer.plan_pollen_layout(quiet, 1116, None)["mode"], "text")


class EinkVisibilityTest(unittest.TestCase):
    """Auf dem Panel muss eine „0“ sichtbar sein: früher weißer Punkt und weißer Text auf Weiß."""

    def _render(self, allergens: dict, mode_budget=None) -> Image.Image:
        pal = renderer.get_dwd_palette("eink")
        img = Image.new("RGBA", (1200, 400), (*SPECTRA6_COLORS["white"], 255))
        plan = renderer.plan_pollen_layout(allergens, 1116, mode_budget)
        fonts = (config.load_font(24, True), config.load_font(19, True), config.load_font(18, False))
        renderer.draw_pollen_strip(img, (42, 10, 1158, 10 + plan["height"]), {"allergens": allergens}, *fonts, pal, plan=plan)
        return img.convert("RGB")

    def _non_white_count(self, img: Image.Image, box) -> int:
        return _ink(img.crop(box))

    def test_zero_values_are_drawn_in_grid_and_chips(self) -> None:
        allergens = {"Birke": _days(1.0, 0.0, 0.0), "Ambrosia": _days(0.0, 0.0, 0.0)}
        for budget in (None, 120):
            img = self._render(allergens, budget)
            # Rechte Hälfte der Datenzeile(n): Tageswerte mit „0“ – muss Tinte enthalten
            ink = self._non_white_count(img, (300, 60, 1150, img.height - 1))
            self.assertGreater(ink, 200, f"Budget {budget}: Nullwerte unsichtbar")
            colors = {color for _, color in img.getcolors(1 << 20)}
            self.assertNotIn((255, 255, 255), colors, "nur Spectra-Farben")

    def test_value_text_is_black_in_flat_theme(self) -> None:
        pal = renderer.get_dwd_palette("eink")
        self.assertEqual(renderer._pollen_value_color(0.0, pal), pal["pollen_label"])
        self.assertEqual(renderer._pollen_value_color(2.0, pal), pal["pollen_label"])
        dark = renderer.get_dwd_palette("dark")
        self.assertNotEqual(renderer._pollen_value_color(2.0, dark), dark["pollen_label"])


class TileTest(unittest.TestCase):
    def test_tile_shows_pollen_only_when_it_fits(self) -> None:
        base = ModuleRenderServices.from_runtime()
        content = {"station_name": "Test", "current_temp_c": 20.0, "current_label": "Bedeckt",
                   "today": {"min_temp_c": 11.0, "max_temp_c": 22.0}, "days": [],
                   "pollen": {"allergens": BUSY}}
        tall = ModuleRenderServices(render_width=1200, render_height=760, display_theme="eink", load_font=base.load_font)
        img_tall = renderer.render_dwd_weather_tile(tall, content, 1200, 760)
        short = ModuleRenderServices(render_width=1200, render_height=330, display_theme="eink", load_font=base.load_font)
        img_short = renderer.render_dwd_weather_tile(short, content, 1200, 330)
        self.assertEqual(img_tall.size, (1200, 760))
        self.assertEqual(img_short.size, (1200, 330))
        # Unten in der hohen Kachel liegt der Pollen-Strip (Tinte), in der kurzen nicht
        self.assertGreater(_ink(img_tall.crop((40, 420, 1160, 740))), 500)


def _ink(img: Image.Image) -> int:
    """Anzahl der Pixel, die nicht Spectra-Weiß sind."""
    white = SPECTRA6_COLORS["white"]
    return sum(count for count, color in img.getcolors(1 << 20) if color != white)


if __name__ == "__main__":
    unittest.main()
