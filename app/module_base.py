"""
Basisklasse für alle Inkwall-Anzeigemodule.

Jedes Modul liegt in einem eigenen Unterordner unter modules/ und stellt
eine Datei __init__.py bereit, die ein `module`-Attribut exportiert
(Instanz einer InkwallModule-Unterklasse; der alte Name PlexInkModule bleibt als Alias).

Prioritäten:
  MODULE_PRIORITY < 10   → Prioritätsmodul  (z. B. Plex: wird angezeigt
                           sobald es aktiven Inhalt meldet, überschreibt Idle)
  MODULE_PRIORITY >= 10  → Idle-Modul (wird rotiert wenn kein Prioritäts-
                           modul aktiv ist)
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from PIL import Image


class InkwallModule(ABC):
    """Basisklasse für alle Anzeigemodule."""

    # ── Pflichtfelder ─────────────────────────────────────────────────────────
    MODULE_ID: str          = ""
    MODULE_NAME: str        = ""
    MODULE_DESCRIPTION: str = ""
    MODULE_PRIORITY: int    = 100   # kleiner = höhere Priorität

    # ── Settings-Deklaration ─────────────────────────────────────────────────
    # Gleiche Struktur wie SETTINGS_FIELDS in app/config.py.
    # Das Feld 'section' kann weggelassen werden – das Framework setzt es
    # automatisch auf MODULE_ID.
    SETTINGS_FIELDS: list[dict] = []

    # Optionale Untergruppen für die Settings-Seite
    # [{"title": "...", "desc": "...", "fields": ["KEY_A", "KEY_B"]}, ...]
    # Leere Liste → alle Felder werden ohne Untergruppen dargestellt.
    SETTINGS_GROUPS: list[dict] = []

    # Prioritätsmodule (Live-Inhalte) werden über einen eigenen Settings-Key
    # ein- und ausgeschaltet (z. B. "PLEX_MODULE_ENABLED"). Idle-Module lassen
    # das leer: sie werden über IDLE_MODULES geschaltet.
    ENABLED_KEY: str | None = None

    # ── Lifecycle-Methoden ────────────────────────────────────────────────────

    def is_enabled(self, env: dict[str, str]) -> bool:
        """
        Schnelle Konfigurationsprüfung ohne IO.
        Gibt False zurück → Framework überspringt dieses Modul vollständig.
        Standard: immer aktiv.
        """
        return True

    @abstractmethod
    def fetch_content(self, env: dict[str, str]) -> Any | None:
        """
        Daten abrufen. Darf IO machen (API-Calls, Cache-Zugriff, …).
        Rückgabe None  → Modul hat gerade keinen Inhalt, nächstes wird versucht.
        Rückgabe !None → Inhalt vorhanden; render() wird aufgerufen.
        """

    @abstractmethod
    def render(self, env: dict[str, str], content: Any) -> Image.Image:
        """
        Inhalt als PIL-Bild rendern.
        Wird nur aufgerufen wenn fetch_content() != None zurückgegeben hat.
        """

    def should_refresh(self, env: dict[str, str]) -> bool:
        """
        True  → Framework rendert neu, auch wenn get_state_key() gleich geblieben ist.
                Nützlich z. B. nach Cache-Ablauf bei Wetter-Daten.
        False → Nur neu rendern wenn get_state_key() sich geändert hat (Standard).
        """
        return False

    def get_state_key(self, content: Any) -> str:
        """
        Eindeutiger String für den aktuellen Inhalt.
        Ändert sich der Key zwischen zwei Ticks → Framework rendert neu.
        Standard: MODULE_ID (immer neu rendern wenn Modul wechselt).
        """
        return self.MODULE_ID

    def get_field_options(self, field_name: str, env: dict[str, str]) -> list | None:
        """
        Optionale dynamische Feldwerte für Settings-Formulare.
        Beispiel: Autocomplete- oder Select-Werte aus einer API.
        """
        return None

    def get_runtime_summary(self, env: dict[str, str]) -> dict[str, str]:
        """
        Optionale Laufzeit-Zusammenfassung für Dashboard / Settings.
        Rückgabe wird mit der Framework-Zusammenfassung zusammengeführt.
        """
        return {}

    def validate_settings(self, updates: dict[str, str], env: dict[str, str]) -> list[str]:
        """
        Optionale modul-spezifische Settings-Validierung.
        updates enthält die Formularwerte dieses Requests, env den daraus
        abgeleiteten Gesamtzustand.
        """
        return []

    def handle_api_action(self, action: str, env: dict[str, str]) -> tuple[Any, int] | None:
        """
        Optionale modul-spezifische API-Aktion.
        Rückgabe: (payload, status_code) oder None wenn die Aktion unbekannt ist.
        """
        return None

    def get_health_status(self, env: dict[str, str]) -> dict[str, Any] | None:
        """
        Optionale Health-Informationen des Moduls für Status-/Health-Endpunkte.
        """
        return None

    def get_next_wake_seconds(self, env: dict[str, str], state: str) -> int | None:
        """
        Optionale modul-spezifische Wake-Empfehlung für /meta.json.
        None -> Framework-Standardlogik verwenden.
        """
        return None

    def get_next_wake_info(self, env: dict[str, str], state: str) -> dict[str, Any] | None:
        """
        Optionale modul-spezifische Wake-Metadaten für /meta.json.
        Erwartete Keys:
        - seconds: int
        - reason: str (optional)
        """
        return None

    def get_background_poll_seconds(self, env: dict[str, str]) -> int | None:
        """
        Optionales Poll-Intervall für den Background-Worker.
        None -> Framework-Standardlogik verwenden.
        """
        return None

    # ── Oberfläche: Status, Zusammenfassung, Verbindungstest ────────────────

    def describe_status(self, env: dict[str, str]) -> dict[str, str]:
        """
        Bereitschaft des Moduls für die Oberfläche, unabhängig davon, ob es
        eingeschaltet ist. Rückgabe {"state": ..., "reason": ...} mit state:
          "ready"   – konfiguriert, kann laufen
          "missing" – Konfiguration unvollständig; reason sagt, was fehlt
          "error"   – konfiguriert, aber zuletzt fehlgeschlagen
        Standard: leitet aus get_health_status() ab ("configured"/"ok").
        """
        health = None
        try:
            health = self.get_health_status(env)
        except Exception:
            health = None
        if isinstance(health, dict):
            if health.get("configured") is False:
                return {"state": "missing", "reason": "Nicht konfiguriert"}
            if health.get("ok") is False:
                return {"state": "error", "reason": str(health.get("error") or "Fehler")}
        return {"state": "ready", "reason": ""}

    def summarize(self, env: dict[str, str]) -> str:
        """
        Ein Satz für die eingeklappte Karte, z. B. "Gießen · UV Frankfurt".
        Standard: die Werte aus get_runtime_summary(), mit · verbunden.
        """
        try:
            values = [str(v) for v in (self.get_runtime_summary(env) or {}).values() if str(v).strip()]
        except Exception:
            values = []
        return " · ".join(values)

    def probe(self, env: dict[str, str]) -> dict[str, Any]:
        """
        Verbindungstest für den Knopf "Prüfen": ruft die Quelle einmal ab.
        Rückgabe {"ok": bool, "message": str}. Standard: fetch_content().
        """
        try:
            content = self.fetch_content(env)
        except Exception as exc:
            return {"ok": False, "message": f"Fehler: {exc}"}
        if content is None:
            return {"ok": False, "message": "Quelle liefert gerade keine Daten"}
        return {"ok": True, "message": "Daten vorhanden"}

    def is_urgent(self, env: dict[str, str]) -> bool:
        """
        True, wenn das Modul gerade etwas Zeitkritisches zeigt (z. B. Müllabfuhr
        morgen früh). Dringende Idle-Module werden in der Rotation zwischen die
        anderen geschoben (jedes zweite Bild) und im Dashboard nach oben sortiert.
        Standard: False. Muss schnell sein – wird bei jedem Render-Durchlauf gefragt.
        """
        return False

    def get_alerts(self, env: dict[str, str]) -> list[dict]:
        """
        Gerade gültige Warnungen für die Benachrichtigung „Warnungen“:
        [{"id", "title", "text", "source", "severity", "until"}, …]. id bleibt
        über die Lebensdauer einer Warnung gleich (Aktualisierungen eingeschlossen),
        damit je Warnung genau eine Nachricht und eine Entwarnung kommt.
        severity: "minor", "moderate", "severe" oder "extreme". Nur aus dem
        Cache antworten – läuft nach jedem Worker-Durchlauf. Standard: keine.
        """
        return []

    def supports_tile(self) -> bool:
        """True, wenn das Modul render_tile() überschreibt (Dashboard-Kachel)."""
        return type(self).render_tile is not InkwallModule.render_tile

    # ── Dashboard-Modus ──────────────────────────────────────────────────────

    def render_tile(self, env: dict[str, str], content: Any, width: int, height: int) -> Image.Image | None:
        """
        Optionale kompakte Darstellung für den Dashboard-Modus (IDLE_LAYOUT=dashboard):
        mehrere Module teilen sich ein Bild, jedes bekommt eine Kachel der
        Größe width × height. Rückgabe None → Modul kann keine Kachel liefern
        und wird im Dashboard übersprungen.
        """
        return None


# Alter Name (bis zur Umbenennung in Inkwall) – externe Module dürfen ihn weiter nutzen
PlexInkModule = InkwallModule
