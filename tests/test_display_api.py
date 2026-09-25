"""
Tests für die JSON-Schnittstelle der Oberfläche (Phase A des UI-Konzepts):
Anzeige-Zustand, Anzeige-Änderungen → Settings-Keys, Karten-Felder mit
Passwort-Schutz und Feldfehlern, Verbindungstest, Ereignis-Filter.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from PIL import Image

import app.config as config
import app.display_api as api
import app.server as server
from app.module_base import InkwallModule


class _Content(InkwallModule):
    MODULE_PRIORITY = 100
    SETTINGS_FIELDS = [
        {"name": "DEMO_URL", "label": "Adresse", "type": "text"},
        {"name": "DEMO_SECRET", "label": "Geheimnis", "type": "password"},
        {"name": "DEMO_DAYS", "label": "Tage", "type": "number", "min": 1, "max": 30},
        {"name": "DEMO_KINDS", "label": "Arten", "type": "checkbox_group", "options": [("a", "A"), ("b", "B")]},
    ]

    def __init__(self, module_id="demo", name="Demo", tile=True):
        self.MODULE_ID, self.MODULE_NAME = module_id, name
        self._tile = tile

    def fetch_content(self, env):
        return {"x": 1} if env.get("DEMO_URL") else None

    def render(self, env, content):
        return Image.new("RGB", (4, 4))

    def render_tile(self, env, content, width, height):
        return Image.new("RGB", (width, height)) if self._tile else None

    def describe_status(self, env):
        return {"state": "ready", "reason": ""} if env.get("DEMO_URL") else {"state": "missing", "reason": "Adresse fehlt"}

    def validate_settings(self, updates, env):
        return ["Adresse: Muss mit http beginnen."] if env.get("DEMO_URL", "").startswith("ftp") else []


class _NoTile(_Content):
    """Inhalt ohne Kachel: render_tile ist die Basis-Implementierung (supports_tile() → False)."""
    render_tile = InkwallModule.render_tile


class _Live(InkwallModule):
    MODULE_PRIORITY = 1
    ENABLED_KEY = "LIVE_MODULE_ENABLED"
    SETTINGS_FIELDS = [{"name": "LIVE_MODULE_ENABLED", "label": "Aktiv", "type": "select", "options": [("true", "an"), ("false", "aus")]}]

    def __init__(self):
        self.MODULE_ID, self.MODULE_NAME = "livedemo", "Live Demo"

    def is_enabled(self, env):
        return env.get("LIVE_MODULE_ENABLED", "false") == "true"

    def fetch_content(self, env):
        return None

    def render(self, env, content):
        return Image.new("RGB", (4, 4))


def _with_registry(content_mods, live_mods):
    all_mods = list(live_mods) + list(content_mods)
    return (
        patch.object(server._registry, "get_modules", return_value=all_mods),
        patch.object(server._registry, "get_idle_modules", return_value=list(content_mods)),
        patch.object(server._registry, "get_priority_modules", return_value=list(live_mods)),
        patch.object(server._registry, "get_module_by_id", side_effect=lambda mid: next((m for m in all_mods if m.MODULE_ID == mid), None)),
    )


class DisplayStateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.demo, self.other, self.live = _Content("demo", "Demo"), _NoTile("other", "Other"), _Live()
        self._patches = _with_registry([self.demo, self.other], [self.live])
        for p in self._patches:
            p.start()
        settings = dict(config.read_env_settings())
        settings.update({"IDLE_MODULES": "other,demo", "IDLE_LAYOUT": "dashboard", "DASHBOARD_TILES": "demo:60, other",
                         "DEMO_URL": "https://x", "LIVE_MODULE_ENABLED": "true"})
        config.apply_runtime_config(settings)
        server._esp32_state = {"hash": "abc", "format": "png", "state": "demo:1", "media_type": "demo", "rendered_at": "2026-09-03T05:00:00Z"}
        server._last_ack = {"device_id": "esp32-1", "hash": "abc", "ack_at": "2026-09-03T05:01:00Z", "remote": "10.0.0.2"}

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        config.apply_runtime_config()

    def test_state_lists_program_in_tile_order_with_status(self) -> None:
        state = server.app.test_client().get("/api/display").get_json()
        self.assertEqual(state["layout"], "dashboard")
        ids = [c["id"] for c in state["content"]]
        self.assertEqual(ids, ["demo", "other"], "Reihenfolge wie DASHBOARD_TILES")
        demo = state["content"][0]
        self.assertTrue(demo["enabled"])
        self.assertEqual(demo["height"], 60)
        self.assertTrue(demo["active_now"])
        self.assertEqual(demo["status"]["state"], "ready")
        self.assertTrue(demo["tile_supported"])
        self.assertFalse(state["content"][1]["tile_supported"])
        self.assertEqual(state["current"]["module_name"], "Demo")
        self.assertEqual(state["live"][0]["id"], "livedemo")
        self.assertTrue(state["live"][0]["enabled"])
        self.assertTrue(state["device"]["hash_matches"])
        self.assertIsInstance(state["device"]["seconds_since_ack"], int)
        self.assertIn("seconds", state["next"])

    def test_missing_status_when_unconfigured(self) -> None:
        config.apply_runtime_config({**config.get_settings_values(), "DEMO_URL": ""})
        state = server.app.test_client().get("/api/display").get_json()
        demo = next(c for c in state["content"] if c["id"] == "demo")
        self.assertEqual(demo["status"], {"state": "missing", "reason": "Adresse fehlt"})


class DisplayUpdateTest(unittest.TestCase):
    def setUp(self) -> None:
        self.demo, self.other, self.live = _Content("demo", "Demo"), _NoTile("other", "Other"), _Live()
        self._patches = _with_registry([self.demo, self.other], [self.live])
        for p in self._patches:
            p.start()
        config.apply_runtime_config({**config.read_env_settings(), "DEMO_URL": "https://x"})

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        config.apply_runtime_config()

    def test_payload_translates_to_settings_keys(self) -> None:
        updates = api.display_updates_from_payload({
            "layout": "dashboard", "rotation_seconds": 240,
            "content": [{"id": "other", "enabled": True, "height": "auto"}, {"id": "demo", "enabled": True, "height": 35},
                        {"id": "nope", "enabled": True}],
            "live": [{"id": "livedemo", "enabled": False}],
            "night": {"enabled": True, "start": "22:30", "end": "06:00", "behavior": "fixed", "fixed_module": "demo"},
        })
        self.assertEqual(updates["IDLE_LAYOUT"], "dashboard")
        self.assertEqual(updates["IDLE_MODULE_ROTATION_SECONDS"], "240")
        self.assertEqual(updates["IDLE_MODULES"], "other,demo")
        self.assertEqual(updates["DASHBOARD_TILES"], "other, demo:35")
        self.assertEqual(updates["LIVE_MODULE_ENABLED"], "false")
        self.assertEqual(updates["NIGHT_MODE_ENABLED"], "true")
        self.assertEqual(updates["NIGHT_MODE_START"], "22:30")
        self.assertEqual(updates["NIGHT_MODE_FIXED_MODULE"], "demo")

    def test_heights_over_100_are_scaled_and_reported(self) -> None:
        updates, notices = api.display_updates_and_notices({
            "content": [{"id": "demo", "enabled": True, "height": 90}, {"id": "other", "enabled": True, "height": 60}],
        })
        self.assertEqual(updates["DASHBOARD_TILES"], "demo:60, other:40")
        self.assertEqual(len(notices), 1)
        self.assertIn("150 %", notices[0])
        # Genau 100 oder darunter bleibt unangetastet
        updates, notices = api.display_updates_and_notices({
            "content": [{"id": "demo", "enabled": True, "height": 70}, {"id": "other", "enabled": True}],
        })
        self.assertEqual(updates["DASHBOARD_TILES"], "demo:70, other")
        self.assertEqual(notices, [])

    def test_put_returns_notices(self) -> None:
        with patch.object(server, "write_env_settings"), patch.object(server, "request_render"):
            response = server.app.test_client().put("/api/display", json={
                "layout": "rotation",
                "content": [{"id": "demo", "enabled": True, "height": 80}, {"id": "other", "enabled": False, "height": 80}],
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        self.assertEqual(response.get_json()["notices"], [], "ausgeschaltete Inhalte zählen nicht")

    def test_put_writes_and_reports_tile_warning(self) -> None:
        with patch.object(server, "write_env_settings") as write_env, \
             patch.object(server, "request_render") as rr:
            response = server.app.test_client().put("/api/display", json={
                "layout": "dashboard",
                "content": [{"id": "other", "enabled": True}, {"id": "demo", "enabled": True, "height": 50}],
            })
        self.assertEqual(response.status_code, 400)
        errors = response.get_json()["errors"]
        self.assertTrue(any("Other" in e and "Kachel" in e for e in errors["general"]))
        write_env.assert_not_called()
        rr.assert_not_called()

        with patch.object(server, "write_env_settings") as write_env, \
             patch.object(server, "request_render") as rr:
            response = server.app.test_client().put("/api/display", json={
                "layout": "dashboard",
                "content": [{"id": "demo", "enabled": True, "height": 50}, {"id": "other", "enabled": False}],
            })
        self.assertEqual(response.status_code, 200, response.get_json())
        written = write_env.call_args[0][0]
        self.assertEqual(written["IDLE_MODULES"], "demo")
        self.assertEqual(written["DASHBOARD_TILES"], "demo:50")
        rr.assert_called_once()
        self.assertEqual(response.get_json()["display"]["layout"], "dashboard")

    def test_night_validation_errors_map_to_fields(self) -> None:
        with patch.object(server, "write_env_settings"), patch.object(server, "request_render"):
            response = server.app.test_client().put("/api/display", json={
                "content": [{"id": "demo", "enabled": True}],
                "night": {"enabled": True, "start": "abc", "end": "07:00"},
            })
        self.assertEqual(response.status_code, 400)
        self.assertIn("NIGHT_MODE_START", response.get_json()["errors"]["fields"])


class ModuleSettingsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.demo = _Content("demo", "Demo")
        self._patches = _with_registry([self.demo], [])
        for p in self._patches:
            p.start()
        config.apply_runtime_config({**config.read_env_settings(), "DEMO_URL": "https://x", "DEMO_SECRET": "geheim",
                                     "DEMO_DAYS": "7", "DEMO_KINDS": "a,b", "IDLE_MODULES": "demo"})
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        config.apply_runtime_config()

    def test_get_blanks_passwords_and_splits_lists(self) -> None:
        payload = self.client.get("/api/settings/demo").get_json()
        by_name = {f["name"]: f for f in payload["fields"]}
        self.assertEqual(by_name["DEMO_SECRET"]["value"], "")
        self.assertTrue(by_name["DEMO_SECRET"]["is_set"])
        self.assertEqual(by_name["DEMO_KINDS"]["value"], ["a", "b"])
        self.assertEqual(by_name["DEMO_URL"]["value"], "https://x")
        self.assertEqual(payload["status"]["state"], "ready")
        self.assertTrue(payload["enabled"])
        self.assertNotIn("geheim", json.dumps(payload))

    def test_put_keeps_password_and_maps_field_errors(self) -> None:
        with patch.object(server, "write_env_settings") as write_env, patch.object(server, "request_render"):
            bad = self.client.put("/api/settings/demo", json={"values": {"DEMO_URL": "ftp://x", "DEMO_DAYS": "99", "DEMO_SECRET": ""}})
        self.assertEqual(bad.status_code, 400)
        fields = bad.get_json()["errors"]["fields"]
        self.assertIn("DEMO_URL", fields)
        self.assertIn("Maximalwert", fields["DEMO_DAYS"])
        write_env.assert_not_called()

        with patch.object(server, "write_env_settings") as write_env, patch.object(server, "request_render") as rr:
            ok = self.client.put("/api/settings/demo", json={"values": {"DEMO_URL": "https://y", "DEMO_SECRET": "", "DEMO_KINDS": ["b"]}})
        self.assertEqual(ok.status_code, 200, ok.get_json())
        written = write_env.call_args[0][0]
        self.assertEqual(written["DEMO_SECRET"], "geheim", "leeres Passwort behält den Wert")
        self.assertEqual(written["DEMO_URL"], "https://y")
        self.assertEqual(written["DEMO_KINDS"], "b")
        rr.assert_called_once()

    def test_saving_one_card_keeps_every_other_setting_in_memory(self) -> None:
        """Regression: ein Teil-Update darf die restliche Laufzeit-Konfiguration nicht löschen."""
        before = config.get_settings_values()
        self.assertEqual(before["DEMO_DAYS"], "7")
        self.assertEqual(before["IDLE_MODULES"], "demo")
        with patch.object(server, "write_env_settings"), patch.object(server, "request_render"):
            ok = self.client.put("/api/settings/demo", json={"values": {"DEMO_URL": "https://z"}})
        self.assertEqual(ok.status_code, 200, ok.get_json())
        after = config.get_settings_values()
        self.assertEqual(after["DEMO_URL"], "https://z")
        self.assertEqual(after["DEMO_DAYS"], "7", "unveränderte Modul-Werte bleiben")
        self.assertEqual(after["IDLE_MODULES"], "demo", "unveränderte Framework-Werte bleiben")
        self.assertEqual(after["DEMO_SECRET"], "geheim", "Passwörter bleiben")

    def test_framework_card_flags_display_managed_fields(self) -> None:
        payload = self.client.get("/api/settings/framework").get_json()
        by_name = {f["name"]: f for f in payload["fields"]}
        self.assertTrue(by_name["IDLE_MODULES"]["managed_by_display"])
        self.assertFalse(by_name["RENDER_WIDTH"]["managed_by_display"])

    def test_unknown_module_404(self) -> None:
        self.assertEqual(self.client.get("/api/settings/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/probe/nope").status_code, 404)

    def test_probe_default_uses_fetch_content(self) -> None:
        self.assertTrue(self.client.post("/api/probe/demo").get_json()["ok"])
        config.apply_runtime_config({**config.get_settings_values(), "DEMO_URL": ""})
        result = self.client.post("/api/probe/demo").get_json()
        self.assertFalse(result["ok"])
        self.assertIn("keine Daten", result["message"])


class _FormModule(_Content):
    """Mit Listen-Feldern und einer Prüfung, die Werte über env und über get_setting liest."""
    SETTINGS_FIELDS = _Content.SETTINGS_FIELDS + [
        {"name": "DEMO_SOURCES", "label": "Quellen", "type": "list",
         "item_fields": [{"name": "label", "label": "Name"}, {"name": "url", "label": "Adresse"}]},
        {"name": "DEMO_PATHS", "label": "Ordner", "type": "list", "item_fields": [{"name": "path", "label": "Ordner"}]},
        {"name": "DEMO_COLORS", "label": "Farben", "type": "mapping"},
    ]

    def probe(self, env):
        return {"ok": True, "message": "|".join([env.get("DEMO_URL", ""), config.get_setting("DEMO_URL"),
                                                 env.get("DEMO_SECRET", ""), config.get_setting("DEMO_SECRET")])}


class CardFormTest(unittest.TestCase):
    """Verbindung prüfen mit Formularwerten, Trennzeichen in Listen."""

    def setUp(self) -> None:
        self._patches = _with_registry([_FormModule("demo", "Demo")], [])
        for p in self._patches:
            p.start()
        config.apply_runtime_config({**config.read_env_settings(), "DEMO_URL": "https://gespeichert", "DEMO_SECRET": "geheim"})
        self.client = server.app.test_client()

    def tearDown(self) -> None:
        for p in self._patches:
            p.stop()
        config.apply_runtime_config()

    def test_probe_checks_unsaved_form_values_only_for_this_request(self) -> None:
        result = self.client.post("/api/probe/demo", json={"values": {"DEMO_URL": "https://neu", "DEMO_SECRET": ""}}).get_json()
        # Datenquellen lesen über get_setting – auch dort gelten die Formularwerte; leeres Passwort = gespeichertes
        self.assertEqual(result["message"], "https://neu|https://neu|geheim|geheim")
        self.assertEqual(config.get_settings_values()["DEMO_URL"], "https://gespeichert", "nichts gespeichert")
        # Ohne Werte: der gespeicherte Stand
        self.assertEqual(self.client.post("/api/probe/demo").get_json()["message"],
                         "https://gespeichert|https://gespeichert|geheim|geheim")

    def test_separator_inside_list_entry_is_rejected(self) -> None:
        cases = [
            ("DEMO_SOURCES", [{"label": "Arbeit|Privat", "url": "https://a"}], "„|“"),
            ("DEMO_SOURCES", [{"label": "", "url": "https://a"}, {"label": "B", "url": "https://b;c"}], "Zeile 2"),
            ("DEMO_COLORS", [{"key": "bio,rest", "value": "green"}], "„,“"),
            ("DEMO_COLORS", [{"key": "a=b", "value": "green"}], "„=“"),
        ]
        for name, value, expected in cases:
            with self.subTest(name=name, value=value), \
                 patch.object(server, "write_env_settings") as write_env, patch.object(server, "request_render"):
                response = self.client.put("/api/settings/demo", json={"values": {name: value}})
                self.assertEqual(response.status_code, 400)
                self.assertIn(expected, response.get_json()["errors"]["fields"][name])
                write_env.assert_not_called()

    def test_valid_lists_are_joined(self) -> None:
        with patch.object(server, "write_env_settings") as write_env, patch.object(server, "request_render"):
            response = self.client.put("/api/settings/demo", json={"values": {
                "DEMO_SOURCES": [{"label": "Familie", "url": "https://a"}, {"label": "", "url": "https://b"}],
                # Einspaltige Liste: nur das Semikolon trennt, ein | im Pfad ist erlaubt
                "DEMO_PATHS": [{"path": "/bilder|alt"}],
                "DEMO_COLORS": [{"key": "bio", "value": "green"}],
            }})
        self.assertEqual(response.status_code, 200, response.get_json())
        written = write_env.call_args[0][0]
        self.assertEqual(written["DEMO_SOURCES"], "Familie|https://a; https://b")
        self.assertEqual(written["DEMO_PATHS"], "/bilder|alt")
        self.assertEqual(written["DEMO_COLORS"], "bio=green")


class BaseHookDefaultsTest(unittest.TestCase):
    def test_default_status_from_health(self) -> None:
        class _M(InkwallModule):
            MODULE_ID = "m"

            def fetch_content(self, env):
                return None

            def render(self, env, content):
                return Image.new("RGB", (1, 1))

            def get_health_status(self, env):
                return {"ok": True, "configured": False}

        self.assertEqual(_M().describe_status({})["state"], "missing")
        self.assertFalse(_M().supports_tile())
        self.assertEqual(_M().summarize({}), "")

    def test_real_modules_expose_status_and_summary(self) -> None:
        import app.module_registry as registry
        env = config.get_settings_values()
        for mod in registry.get_modules():
            status = mod.describe_status(env)
            self.assertIn(status["state"], ("ready", "missing", "error"), mod.MODULE_ID)
            self.assertIsInstance(mod.summarize(env), str)


class DisplayPageTest(unittest.TestCase):
    def test_home_is_the_display_page_and_old_dashboard_redirects(self) -> None:
        client = server.app.test_client()
        home = client.get("/")
        self.assertEqual(home.status_code, 200)
        html = home.get_data(as_text=True)
        self.assertIn("Programm", html)
        self.assertIn("/api/display", html)
        self.assertIn("ui.js", html)
        old = client.get("/dashboard")
        self.assertEqual(old.status_code, 302)
        self.assertTrue(old.headers["Location"].endswith("/"))
        self.assertEqual(client.get("/static/ui.js").status_code, 200)


class EventsFilterTest(unittest.TestCase):
    def test_events_filter_keeps_events_and_warnings(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            logs_dir = Path(tmp)
            entries = [
                {"ts": "t", "level": "INFO", "name": "server", "msg": "Rendered [x]"},
                {"ts": "t", "level": "INFO", "name": "events", "msg": "Display zeigt jetzt: Wetter", "event": "switch"},
                {"ts": "t", "level": "WARNING", "name": "dwd", "msg": "nicht erreichbar"},
            ]
            (logs_dir / "app.jsonl").write_text("\n".join(json.dumps(e) for e in entries) + "\n", encoding="utf-8")
            with patch.object(server, "LOGS_DIR", logs_dir):
                data = server.app.test_client().get("/api/logs?events=1").get_json()
        self.assertEqual([e["msg"] for e in data], ["Display zeigt jetzt: Wetter", "nicht erreichbar"])


if __name__ == "__main__":
    unittest.main()
