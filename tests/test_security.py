"""
Sicherheit:
- Schreibende Anfragen fremder Webseiten werden abgelehnt (Sec-Fetch-Site, Origin)
- Geräte-Token schützt /firmware.bin, /firmware.json und /ack
- /health zeigt ohne Anmeldung keine Details der Inhalte, wenn ein UI-Passwort gesetzt ist
- Export „ohne Geheimnisse“ lässt Webhook- und Kalender-Links weg
- Geheimnisse verschwinden aus Logzeilen und Fehlermeldungen
- Upload-Grenze, ACK-Ergebnisse als Metrik-Label nur aus einer festen Liste
"""

from __future__ import annotations

import base64
import os
import unittest
from unittest.mock import patch

import app.config as config
from app import monitoring, server
from app.logger import redact_secrets, set_known_secrets


def _auth(pw: str) -> dict:
    return {"Authorization": "Basic " + base64.b64encode(f"user:{pw}".encode()).decode()}


class CrossSiteTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.app.test_client()
        self._banner = patch("app.device.request_test_banner")
        self._banner.start()

    def tearDown(self) -> None:
        self._banner.stop()

    def _post(self, **headers):
        return self.client.post("/api/device/test-banner", headers=headers)

    def test_browser_requests_from_other_sites_are_refused(self) -> None:
        self.assertEqual(self._post(**{"Sec-Fetch-Site": "cross-site"}).status_code, 403)
        self.assertEqual(self._post(**{"Sec-Fetch-Site": "same-site"}).status_code, 403, "anderer Dienst auf demselben Rechner")
        self.assertEqual(self._post(Origin="http://evil.example").status_code, 403)
        self.assertEqual(self._post(Origin="null").status_code, 403)

    def test_own_page_devices_and_tools_are_allowed(self) -> None:
        self.assertEqual(self._post(**{"Sec-Fetch-Site": "same-origin"}).status_code, 200)
        self.assertEqual(self._post(Origin="http://localhost").status_code, 200)
        self.assertEqual(self._post().status_code, 200, "curl, Gerät, Plex-Webhook schicken keinen dieser Header")
        self.assertEqual(self.client.get("/api/display", headers={"Sec-Fetch-Site": "cross-site"}).status_code, 200,
                         "lesende Anfragen sind nicht betroffen")

    def test_firmware_upload_from_foreign_site_is_refused(self) -> None:
        response = self.client.post("/api/device/firmware", data={}, content_type="multipart/form-data",
                                    headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(response.status_code, 403)


class DeviceTokenTest(unittest.TestCase):
    def setUp(self) -> None:
        self.client = server.app.test_client()
        self._env = patch.dict(os.environ, {"INKWALL_DEVICE_TOKEN": "geraete-token-123", "INKWALL_UI_PASSWORD": ""})
        self._env.start()
        self._ack = patch.object(server, "_last_ack", {})
        self._ack.start()

    def tearDown(self) -> None:
        self._ack.stop()
        self._env.stop()

    def test_firmware_and_ack_need_the_token(self) -> None:
        self.assertEqual(self.client.get("/firmware.bin").status_code, 401)
        self.assertEqual(self.client.get("/firmware.json").status_code, 401)
        self.assertEqual(self.client.post("/ack", json={"device_id": "x", "hash": "a"}).status_code, 401)
        self.assertEqual(self.client.post("/ack", json={}, headers={"X-Inkwall-Token": "falsch"}).status_code, 401)

        header = {"X-Inkwall-Token": "geraete-token-123"}
        self.assertNotEqual(self.client.get("/firmware.bin", headers=header).status_code, 401)
        self.assertEqual(self.client.post("/ack", json={"device_id": "x", "hash": "a"}, headers=header).status_code, 200)
        self.assertEqual(self.client.post("/ack?token=geraete-token-123", json={"device_id": "x", "hash": "a"}).status_code, 200)

    def test_images_and_meta_stay_open(self) -> None:
        self.assertEqual(self.client.get("/meta.json").status_code, 200)
        self.assertNotEqual(self.client.get("/hash").status_code, 401)

    def test_ui_password_also_opens_the_firmware(self) -> None:
        with patch.dict(os.environ, {"INKWALL_UI_PASSWORD": "pw123"}):
            self.assertEqual(self.client.get("/firmware.json").status_code, 401)
            self.assertNotEqual(self.client.get("/firmware.json", headers=_auth("pw123")).status_code, 401)

    def test_without_token_everything_works_as_before(self) -> None:
        with patch.dict(os.environ, {"INKWALL_DEVICE_TOKEN": ""}):
            self.assertEqual(self.client.post("/ack", json={"device_id": "x", "hash": "a"}).status_code, 200)


class HealthDetailsTest(unittest.TestCase):
    def test_details_only_when_signed_in(self) -> None:
        client = server.app.test_client()
        with patch.dict(os.environ, {"INKWALL_UI_PASSWORD": "pw123"}):
            open_data = client.get("/health").get_json()
            self.assertNotIn("modules", open_data)
            self.assertIn("worker", open_data)
            self.assertIn("modules", client.get("/health", headers=_auth("pw123")).get_json())
        with patch.dict(os.environ, {"INKWALL_UI_PASSWORD": ""}):
            self.assertIn("modules", client.get("/health").get_json())


class ExportTest(unittest.TestCase):
    def setUp(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(),
                                     "NOTIFY_URL": "https://discord.com/api/webhooks/1/geheim",
                                     "CALENDAR_ICS_URLS": "Familie|https://calendar.google.com/calendar/ical/x/private-abcdef0123/basic.ics",
                                     "DWD_WEATHER_STATION_ID": "10532"})

    def tearDown(self) -> None:
        config.apply_runtime_config()

    def test_export_without_secrets_drops_webhook_and_calendar_links(self) -> None:
        from app.display_api import export_settings
        plain = export_settings(include_secrets=False)["values"]
        self.assertNotIn("NOTIFY_URL", plain)
        self.assertNotIn("CALENDAR_ICS_URLS", plain)
        self.assertNotIn("GARBAGE_ICS_URLS", plain)
        self.assertEqual(plain["DWD_WEATHER_STATION_ID"], "10532")
        full = export_settings(include_secrets=True)["values"]
        self.assertTrue(full["NOTIFY_URL"].endswith("/geheim"))


class RedactionTest(unittest.TestCase):
    def tearDown(self) -> None:
        set_known_secrets([])

    def test_url_patterns(self) -> None:
        cases = {
            "POST https://discord.com/api/webhooks/123456/AbC-dEf_123 -> 403": "webhooks/123456/***",
            "https://hooks.slack.com/services/T0/B0/XyZ123": "services/T0/B0/***",
            "https://ntfy.sh/mein-geheimes-thema failed": "ntfy.sh/***",
            "404 for url: https://calendar.google.com/calendar/ical/a%40b/private-0123456789abcdef/basic.ics": "private-***",
            "https://cloud.example/remote.php/dav/public-calendars/AbCdEf123?export": "public-calendars/***",
            "http://admin:hunter2@router.local/": "admin:***@",
            "https://example/api?apikey=12345": "apikey=***",
        }
        for text, expected in cases.items():
            redacted = redact_secrets(text)
            self.assertIn(expected, redacted, text)
            self.assertNotIn("hunter2", redacted)

    def test_known_secret_values(self) -> None:
        set_known_secrets(["geraete-token-123", "kurz"])
        self.assertEqual(redact_secrets("Token geraete-token-123 abgelehnt"), "Token *** abgelehnt")
        self.assertEqual(redact_secrets("kurz bleibt"), "kurz bleibt", "zu kurze Werte würden normale Wörter zerschießen")

    def test_server_registers_configured_secrets(self) -> None:
        config.apply_runtime_config({**config.read_env_settings(), "NOTIFY_URL": "https://example.test/hook/very-secret-path"})
        try:
            with patch.dict(os.environ, {"INKWALL_DEVICE_TOKEN": "geraete-token-123"}):
                server._refresh_log_secrets()
            self.assertEqual(redact_secrets("an https://example.test/hook/very-secret-path gescheitert"), "an *** gescheitert")
            self.assertNotIn("geraete-token-123", redact_secrets("x geraete-token-123 y"))
        finally:
            config.apply_runtime_config()
            server._refresh_log_secrets()


class LimitsTest(unittest.TestCase):
    def test_oversized_request_is_rejected(self) -> None:
        client = server.app.test_client()
        response = client.post("/ack", data=b"x" * (5 * 1024 * 1024), content_type="application/json")
        self.assertEqual(response.status_code, 413)

    def test_unknown_ack_result_is_not_a_metric_label(self) -> None:
        client = server.app.test_client()
        monitoring.reset_counters()
        with patch.object(server, "_last_ack", {}):
            client.post("/ack", json={"device_id": "x", "hash": "a", "result": "irgendwas-langes-" + "z" * 50})
            client.post("/ack", json={"device_id": "x", "hash": "a", "result": "updated"})
        labels = {labels.get("result") for name, labels, _ in monitoring.counter_values() if name == "inkwall_acks_total"}
        self.assertEqual(labels, {"other", "updated"})
        monitoring.reset_counters()


if __name__ == "__main__":
    unittest.main()
