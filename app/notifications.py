"""
Reiche Benachrichtigungen: eine Nachricht mit Titel, Text, Farbe, Feldern,
Link und optionalem Bild, zugestellt je nach Ziel:

- Discord-Webhook: Embed mit Farbstreifen, Feldern, Fußzeile und Zeitstempel,
  Name und Profilbild pro Nachricht, Bild als Anhang (Multipart).
- Slack-Webhook: Blocks (Kopfzeile, Text, Felder), Bild nur mit erreichbarer
  Serveradresse (NOTIFY_BASE_URL), weil Slack keine Anhänge per Webhook nimmt.
- ntfy: Title/Tags/Priority/Icon/Click-Header, Bild als Anhang (Body = Datei,
  Text im Message-Header, RFC 2047 bei Umlauten).
- Alles andere: Text-POST.

Ereignisse (NOTIFY_EVENTS): Ausfall und Entwarnung, Firmware (Update und
Rollback), wiederholte Fehler des Geräts, Quelle länger nicht erreichbar,
Tagesbild am Morgen, Wochenbericht montags. Merker liegen in
device_state.json unter "notify", damit ein Neustart nichts wiederholt.
"""

from __future__ import annotations

import base64
import io
import json
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.config import CURRENT_IMAGE_PATH, WEEKDAYS_DE_LONG, local_tz
from app.device import load_device_state, update_device_state
from app.http_client import HTTP_SESSION
from app.logger import get_logger

log = get_logger(__name__)

BOT_NAME = "Inkwall"
DEFAULT_AVATAR_URL = "https://raw.githubusercontent.com/N0cky/inkwall/main/logo.png"
ERROR_STREAK = 3                      # so viele Fehlerzyklen in Folge, bis gemeldet wird
SOURCE_STALE_HOURS = 6                # so lange darf eine Quelle aus dem Cache leben, bis gemeldet wird
SNAPSHOT_MAX_EDGE = 900

# on_ack (ACK) und on_cycle (Worker) lesen und schreiben dieselben Merker –
# nacheinander, sonst meldet einer ein Ereignis, das der andere gerade abgehakt hat
_NOTIFY_LOCK = threading.Lock()

EVENT_OPTIONS = (
    ("offline",  "Ausfall und Entwarnung"),
    ("firmware", "Firmware: Update und Rollback"),
    ("errors",   f"Wiederholte Fehler des Geräts ({ERROR_STREAK} Zyklen in Folge)"),
    ("sources",  f"Quelle länger als {SOURCE_STALE_HOURS} h nicht erreichbar"),
    ("daily",    "Tagesbild am Morgen"),
    ("weekly",   "Wochenbericht montags"),
)
DEFAULT_EVENTS = "offline,firmware"

COLORS = {
    "offline": 0xE5484D, "online": 0x30A46C, "firmware": 0x3B82F6, "rollback": 0xF59E0B, "errors": 0xF59E0B,
    "source_down": 0xF59E0B, "source_up": 0x30A46C, "daily": 0x8B5CF6, "weekly": 0x0EA5E9, "test": 0x14B8A6,
}
TAGS = {
    "offline": "warning", "online": "white_check_mark", "firmware": "arrow_up", "rollback": "rewind", "errors": "x",
    "source_down": "cloud", "source_up": "white_check_mark", "daily": "sunrise", "weekly": "bar_chart", "test": "bell",
}


@dataclass
class Notification:
    kind: str
    title: str
    message: str
    fields: list = field(default_factory=list)      # [(Name, Wert), …]
    image_png: bytes | None = None
    image_name: str = "display.png"
    link: str = ""
    priority: str = ""

    @property
    def color(self) -> int:
        return COLORS.get(self.kind, 0x2E7CF6)

    @property
    def tags(self) -> str:
        return TAGS.get(self.kind, "")

    def as_text(self) -> str:
        lines = [self.message]
        lines.extend(f"{name}: {value}" for name, value in self.fields)
        if self.link:
            lines.append(self.link)
        return "\n".join(line for line in lines if line)


# ---------------------------------------------------------------------------
# Hilfen
# ---------------------------------------------------------------------------

def page_link(base_url: str, path: str = "/geraet") -> str:
    base = (base_url or "").strip().rstrip("/")
    return f"{base}{path}" if base else ""


def display_snapshot(max_edge: int = SNAPSHOT_MAX_EDGE) -> bytes | None:
    """Aktuelles Display-Bild verkleinert als PNG-Bytes, None ohne Bild."""
    try:
        from PIL import Image
        if not CURRENT_IMAGE_PATH.exists():
            return None
        with Image.open(CURRENT_IMAGE_PATH) as img:
            rgb = img.convert("RGB")
            longest = max(rgb.size)
            if longest > max_edge:
                scale = max_edge / float(longest)
                rgb = rgb.resize((max(1, int(rgb.width * scale)), max(1, int(rgb.height * scale))), Image.LANCZOS)
            buf = io.BytesIO()
            rgb.save(buf, "PNG", optimize=True)
            return buf.getvalue()
    except Exception as exc:
        log.warning(f"Display-Bild für die Nachricht nicht lesbar: {exc}")
        return None


def _rfc2047(text: str) -> str:
    """ntfy-Header dürfen nur ASCII – Umlaute per RFC 2047 (Base64) kodieren."""
    if text.isascii():
        return text
    return "=?UTF-8?B?" + base64.b64encode(text.encode("utf-8")).decode("ascii") + "?="


def _target(url: str) -> str:
    lower = url.lower()
    if "discord.com/api/webhooks" in lower or "discordapp.com/api/webhooks" in lower:
        return "discord"
    if "hooks.slack.com" in lower:
        return "slack"
    if "ntfy" in lower:
        return "ntfy"
    return "text"


def _format_minutes(seconds: int) -> str:
    minutes = max(1, int(seconds) // 60)
    if minutes >= 60:
        return f"{minutes // 60} h {minutes % 60} min" if minutes % 60 else f"{minutes // 60} h"
    return f"{minutes} min"


def _local(dt: datetime) -> datetime:
    """In die eingestellte Zeitzone – ohne Argument nähme astimezone die des Containers, meist UTC."""
    return dt.astimezone(local_tz())


def _parse_ts(value: str) -> datetime | None:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Zustellung
# ---------------------------------------------------------------------------

def deliver(url: str, note: Notification, avatar_url: str = "", base_url: str = "") -> bool:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return False
    avatar = (avatar_url or "").strip() or DEFAULT_AVATAR_URL
    target = _target(url)
    try:
        if target == "discord":
            response = _post_discord(url, note, avatar)
        elif target == "slack":
            response = _post_slack(url, note, base_url)
        elif target == "ntfy":
            response = _post_ntfy(url, note, avatar)
        else:
            response = HTTP_SESSION.post(url, data=f"{note.title}\n{note.as_text()}".encode("utf-8"),
                                         headers={"Content-Type": "text/plain; charset=utf-8", "Title": _rfc2047(note.title)}, timeout=15)
        status = getattr(response, "status_code", 0)
        if status >= 400:
            log.warning(f"Benachrichtigung ({target}) abgelehnt: HTTP {status} {getattr(response, 'text', '')[:200]}")
            return False
        return True
    except Exception as exc:
        log.warning(f"Benachrichtigung ({target}) nicht zustellbar: {exc}")
        return False


def discord_payload(note: Notification, avatar: str) -> dict:
    embed: dict = {
        "title": note.title[:256],
        "description": note.message[:4000],
        "color": note.color,
        "fields": [{"name": str(n)[:256], "value": str(v)[:1024] or "–", "inline": True} for n, v in note.fields[:25]],
        "footer": {"text": BOT_NAME},
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if note.link:
        embed["url"] = note.link
    if note.image_png:
        embed["image"] = {"url": f"attachment://{note.image_name}"}
    return {"username": BOT_NAME, "avatar_url": avatar, "embeds": [embed]}


def _post_discord(url: str, note: Notification, avatar: str):
    payload = discord_payload(note, avatar)
    if note.image_png:
        return HTTP_SESSION.post(url, data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                                 files={"files[0]": (note.image_name, note.image_png, "image/png")}, timeout=20)
    return HTTP_SESSION.post(url, json=payload, timeout=15)


def slack_payload(note: Notification, base_url: str) -> dict:
    blocks: list[dict] = [
        {"type": "header", "text": {"type": "plain_text", "text": note.title[:150]}},
        {"type": "section", "text": {"type": "mrkdwn", "text": note.message[:3000] or " "}},
    ]
    if note.fields:
        blocks.append({"type": "section", "fields": [{"type": "mrkdwn", "text": f"*{n}*\n{v}"[:2000]} for n, v in note.fields[:10]]})
    if note.image_png and base_url:
        blocks.append({"type": "image", "image_url": page_link(base_url, "/current.png"), "alt_text": "Display"})
    if note.link:
        blocks.append({"type": "context", "elements": [{"type": "mrkdwn", "text": f"<{note.link}|Gerät-Seite öffnen>"}]})
    return {"text": f"{note.title} – {note.message}"[:3000], "blocks": blocks}


def _post_slack(url: str, note: Notification, base_url: str):
    return HTTP_SESSION.post(url, json=slack_payload(note, base_url), timeout=15)


def ntfy_headers(note: Notification, avatar: str) -> dict:
    headers = {"Title": _rfc2047(note.title), "Icon": avatar}
    if note.tags:
        headers["Tags"] = note.tags
    if note.priority:
        headers["Priority"] = note.priority
    if note.link:
        headers["Click"] = note.link
    return headers


def _post_ntfy(url: str, note: Notification, avatar: str):
    headers = ntfy_headers(note, avatar)
    if note.image_png:
        headers["Filename"] = note.image_name
        headers["Message"] = _rfc2047(note.as_text())
        return HTTP_SESSION.post(url, data=note.image_png, headers=headers, timeout=20)
    headers["Content-Type"] = "text/plain; charset=utf-8"
    return HTTP_SESSION.post(url, data=note.as_text().encode("utf-8"), headers=headers, timeout=15)


# ---------------------------------------------------------------------------
# Ereignisse
# ---------------------------------------------------------------------------

class Notifier:
    """
    Kennt die Einstellungen und die Merker. Der Server ruft on_ack() bei jeder
    Rückmeldung und on_cycle() nach jedem Worker-Durchlauf; beide liefern die
    Arten der verschickten Nachrichten zurück (fürs Log und die Zähler).
    """

    def __init__(self, url: str, events, offline_minutes: int, daily_hour: int = 7,
                 base_url: str = "", avatar_url: str = "", snapshot=display_snapshot):
        self.url = (url or "").strip()
        self.events = set(events or ())
        self.offline_minutes = int(offline_minutes or 0)
        self.daily_hour = int(daily_hour)
        self.base_url = (base_url or "").strip()
        self.avatar_url = (avatar_url or "").strip()
        self._snapshot = snapshot

    # ── Merker ──────────────────────────────────────────────────────────────
    @staticmethod
    def _markers() -> tuple[dict, dict]:
        state = load_device_state()
        markers = state.get("notify")
        if not isinstance(markers, dict):
            markers = {}
        # Alter Merker des Ausfalls (vor den Ereignissen) weiter beachten
        if state.get("offline_notified_at") and "offline_at" not in markers:
            markers["offline_at"] = state["offline_notified_at"]
        return state, markers

    @staticmethod
    def _save(state: dict, markers: dict) -> None:
        """Nur die Merker zurückschreiben – in den frisch gelesenen Zustand, andere Felder bleiben unberührt."""
        def change(current: dict) -> None:
            current["notify"] = markers
            current.pop("offline_notified_at", None)
            current.pop("offline_notified_sent", None)
        update_device_state(change)

    def _send(self, note: Notification) -> bool:
        if not self.url:
            return False
        if not note.link:
            note.link = page_link(self.base_url)
        return deliver(self.url, note, self.avatar_url, self.base_url)

    def _device_fields(self, ack: dict) -> list:
        fields = []
        if ack.get("fw_version"):
            fields.append(("Firmware", ack["fw_version"]))
        if isinstance(ack.get("rssi"), int):
            fields.append(("WLAN", f"{ack['rssi']} dBm"))
        if isinstance(ack.get("cycle_ms"), int):
            fields.append(("Zyklus", f"{ack['cycle_ms'] / 1000:.1f} s"))
        return fields

    # ── Rückmeldung ─────────────────────────────────────────────────────────
    def on_ack(self, ack: dict, previous: dict, now: datetime | None = None) -> list[str]:
        with _NOTIFY_LOCK:
            return self._on_ack(ack, previous, now)

    def _on_ack(self, ack: dict, previous: dict, now: datetime | None) -> list[str]:
        now = now or datetime.now(timezone.utc)
        device = ack.get("device_id") or "Das Display"
        sent: list[str] = []
        state, markers = self._markers()
        changed = False

        # Entwarnung nach gemeldetem Ausfall (immer, der Merker muss weg)
        if markers.get("offline_at"):
            since = _parse_ts(markers["offline_at"])
            markers.pop("offline_at", None)
            markers.pop("offline_sent", None)
            changed = True
            if "offline" in self.events:
                gone = f" – war rund {_format_minutes(int((now - since).total_seconds()))} länger weg" if since else ""
                note = Notification("online", f"{device} ist wieder da", f"{device} meldet sich wieder{gone}.",
                                    fields=[("Zuletzt", f"{_local(now):%H:%M}")] + self._device_fields(ack),
                                    image_png=self._snapshot())
                if self._send(note):
                    sent.append("online")

        # Firmware: neue Version läuft, oder Rollback gemeldet
        if "firmware" in self.events:
            fw_now, fw_prev = ack.get("fw_version", ""), previous.get("fw_version", "")
            if fw_now and fw_prev and fw_now != fw_prev:
                note = Notification("firmware", f"{device} läuft jetzt Firmware {fw_now}",
                                    f"Das Update von {fw_prev} auf {fw_now} ist eingespielt, das Gerät hat sich mit der neuen Version gemeldet.",
                                    fields=[("Vorher", fw_prev), ("Jetzt", fw_now)] + self._device_fields(ack)[1:])
                if self._send(note):
                    sent.append("firmware")
            error = str(ack.get("error", "") or "")
            if "zurueckgerollt" in error.lower() or "zurückgerollt" in error.lower():
                if markers.get("rollback_error") != error:
                    markers["rollback_error"] = error
                    changed = True
                    note = Notification("rollback", f"{device}: Firmware zurückgerollt", error,
                                        fields=[("Läuft wieder", fw_now or "?")], priority="high")
                    if self._send(note):
                        sent.append("rollback")
            elif markers.get("rollback_error"):
                markers.pop("rollback_error", None)
                changed = True

        # Wiederholte Fehler
        streak = int(markers.get("error_streak", 0) or 0)
        if ack.get("result") == "error":
            streak += 1
            markers["error_streak"] = streak
            changed = True
            if streak == ERROR_STREAK and "errors" in self.events:
                note = Notification("errors", f"{device} meldet {streak} Fehler in Folge",
                                    str(ack.get("error", "") or "Fehler ohne Beschreibung"),
                                    fields=self._device_fields(ack), priority="high")
                if self._send(note):
                    sent.append("errors")
        elif streak:
            markers["error_streak"] = 0
            changed = True

        if changed:
            self._save(state, markers)
        return sent

    # ── Worker-Durchlauf ────────────────────────────────────────────────────
    def on_cycle(self, last_ack: dict, now_local: datetime, stale_sources: list[tuple[str, str, datetime]] | None = None,
                 current: dict | None = None, weekly_stats=None) -> list[str]:
        """
        stale_sources: [(module_id, Name, seit wann aus dem Cache), …]
        current: {"module_name", "rendered_at"} fürs Tagesbild
        weekly_stats: Callable → dict (ack_stats über 7 Tage) für den Wochenbericht
        """
        if not self.url:
            return []
        with _NOTIFY_LOCK:
            return self._on_cycle(last_ack, now_local, stale_sources, current, weekly_stats)

    def _on_cycle(self, last_ack: dict, now_local: datetime, stale_sources, current, weekly_stats) -> list[str]:
        now_utc = now_local.astimezone(timezone.utc) if now_local.tzinfo else now_local.replace(tzinfo=timezone.utc)
        sent: list[str] = []
        state, markers = self._markers()
        changed = False
        device = last_ack.get("device_id") or "Das Display"

        # Ausfall
        if "offline" in self.events and self.offline_minutes > 0 and not markers.get("offline_at"):
            ack_at = _parse_ts(str(last_ack.get("ack_at", "")))
            if ack_at is not None:
                age = int((now_utc - ack_at).total_seconds())
                if age >= self.offline_minutes * 60:
                    note = Notification("offline", f"{device} ausgefallen",
                                        f"{device} hat sich seit {_format_minutes(age)} nicht gemeldet.",
                                        fields=[("Zuletzt gemeldet", f"{_local(ack_at):%d.%m. %H:%M}")] + self._device_fields(last_ack),
                                        priority="high")
                    markers["offline_at"] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    markers["offline_sent"] = self._send(note)
                    changed = True
                    if markers["offline_sent"]:
                        sent.append("offline")

        # Quellen
        if "sources" in self.events:
            notified: dict = markers.get("sources") or {}
            stale_ids = set()
            for module_id, name, since in (stale_sources or []):
                stale_ids.add(module_id)
                since_utc = since.astimezone(timezone.utc) if since.tzinfo else since.replace(tzinfo=timezone.utc)
                hours = (now_utc - since_utc).total_seconds() / 3600
                if hours >= SOURCE_STALE_HOURS and module_id not in notified:
                    note = Notification("source_down", f"{name}: Quelle nicht erreichbar",
                                        f"{name} zeigt seit {hours:.0f} h den gespeicherten Stand vom {_local(since_utc):%d.%m. %H:%M}. "
                                        "Das Display läuft weiter, die Daten werden aber nicht mehr aktualisiert.",
                                        fields=[("Inhalt", name), ("Stand", f"{_local(since_utc):%d.%m. %H:%M}")])
                    notified[module_id] = now_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
                    markers["sources"] = notified
                    changed = True
                    if self._send(note):
                        sent.append("source_down")
            for module_id in list(notified):
                if module_id not in stale_ids:
                    notified.pop(module_id)
                    markers["sources"] = notified
                    changed = True
                    name = next((n for m, n, _ in (stale_sources or []) if m == module_id), module_id)
                    if self._send(Notification("source_up", f"{name}: Quelle wieder erreichbar", f"{name} liefert wieder aktuelle Daten.")):
                        sent.append("source_up")

        # Tagesbild
        today = now_local.strftime("%Y-%m-%d")
        if "daily" in self.events and now_local.hour >= self.daily_hour and markers.get("daily_date") != today:
            markers["daily_date"] = today
            changed = True
            cur = current or {}
            fields = [("Inhalt", cur.get("module_name") or "–")]
            rendered = _parse_ts(str(cur.get("rendered_at", "")))
            if rendered:
                fields.append(("Erzeugt", f"{_local(rendered):%H:%M}"))
            ack_at = _parse_ts(str(last_ack.get("ack_at", "")))
            if ack_at:
                fields.append(("Gerät zuletzt", f"{_local(ack_at):%H:%M}"))
            note = Notification("daily", f"Guten Morgen – das Display am {WEEKDAYS_DE_LONG[now_local.weekday()]}, {now_local:%d.%m.}",
                                "So sieht das Display gerade aus.", fields=fields, image_png=self._snapshot())
            if self._send(note):
                sent.append("daily")

        # Wochenbericht montags
        week = now_local.strftime("%G-W%V")
        if "weekly" in self.events and now_local.weekday() == 0 and now_local.hour >= self.daily_hour and markers.get("weekly_week") != week:
            markers["weekly_week"] = week
            changed = True
            stats = weekly_stats() if callable(weekly_stats) else (weekly_stats or {})
            fields = [
                ("Rückmeldungen", str(stats.get("count", 0))),
                ("Längste Pause", _format_minutes(stats.get("longest_gap_s") or 0) if stats.get("longest_gap_s") else "–"),
                ("Pausen über Takt", str(stats.get("gaps_over_threshold", 0))),
                ("Fehler", str(stats.get("errors", 0))),
                ("Ø Zyklus", f"{stats['avg_cycle_ms'] / 1000:.1f} s" if stats.get("avg_cycle_ms") else "–"),
                ("WLAN min / Ø", f"{stats['rssi_min']} / {stats.get('rssi_avg', '–')} dBm" if stats.get("rssi_min") is not None else "–"),
            ]
            note = Notification("weekly", f"Wochenbericht KW {now_local:%V}",
                                f"{device} in den letzten 7 Tagen.", fields=fields, image_png=self._snapshot())
            if self._send(note):
                sent.append("weekly")

        if changed:
            self._save(state, markers)
        return sent

    def test_note(self, last_ack: dict, current: dict | None = None) -> Notification:
        device = last_ack.get("device_id") or "Das Display"
        fields = [("Gerät", device), ("Ereignisse", ", ".join(sorted(self.events)) or "keine"),
                  ("Ausfall nach", f"{self.offline_minutes} min" if self.offline_minutes else "nie")]
        if current and current.get("module_name"):
            fields.append(("Inhalt", current["module_name"]))
        return Notification("test", "Testnachricht", "Benachrichtigungen funktionieren. So sehen die Nachrichten aus, inklusive dem aktuellen Display-Bild.",
                            fields=fields, image_png=self._snapshot())

    def send_test(self, last_ack: dict, current: dict | None = None) -> bool:
        return self._send(self.test_note(last_ack, current))
