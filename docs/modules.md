# Module Guide

## Ziel

Neue Module sollen ohne Eingriffe an vielen zentralen Stellen eingebunden werden koennen.
Das Projekt nutzt dafuer eine modulare Registry unter `modules/`.

Ein Modul kann:

- Inhalte laden
- ein Bild rendern
- eigene Settings deklarieren
- eigene Settings validieren
- Runtime-Informationen fuer die Settings-Seite liefern
- dynamische Feldoptionen bereitstellen
- optionale API-Aktionen anbieten
- Health-Informationen melden
- eine eigene Wake-Empfehlung fuer `meta.json` liefern


## Verzeichnisstruktur

Ein Modul liegt in `modules/<module_id>/`.

Minimales Beispiel:

```text
modules/
  example/
    __init__.py
    renderer.py
    data_source.py
```


## Pflichtbestandteile

Jedes Modul exportiert in `__init__.py` ein Attribut `module`.

Dieses Objekt muss eine Instanz einer Klasse sein, die von `InkwallModule` erbt:

```python
from __future__ import annotations

from PIL import Image

from app.module_base import InkwallModule


class ExampleModule(InkwallModule):
    MODULE_ID = "example"
    MODULE_NAME = "Example"
    MODULE_DESCRIPTION = "Beispielmodul"
    MODULE_PRIORITY = 120

    SETTINGS_FIELDS = []
    SETTINGS_GROUPS = []

    def is_enabled(self, env: dict[str, str]) -> bool:
        return True

    def fetch_content(self, env: dict[str, str]):
        return {"text": "Hallo Welt"}

    def render(self, env: dict[str, str], content) -> Image.Image:
        from PIL import Image
        return Image.new("RGB", (1600, 1200), (255, 255, 255))


module = ExampleModule()
```


## Prioritaet

- `MODULE_PRIORITY < 10`: Prioritaetsmodul
  - Beispiel: Plex oder Steam
  - wird vor Idle-Modulen ausgewertet
- `MODULE_PRIORITY >= 10`: Idle-Modul
  - wird in die Idle-Rotation aufgenommen

Je kleiner der Wert, desto hoeher die Prioritaet.


## Registry

Die Registry scannt automatisch `modules/*/__init__.py`.

Wichtige Regeln:

- `MODULE_ID` muss eindeutig sein
- Settings-Feldnamen muessen global eindeutig sein
- kaputte Module werden geloggt und uebersprungen

Die zentrale Registry liegt in:

- [module_registry.py](/C:/Users/tobia/Documents/Inkwall/app/module_registry.py)


## Settings deklarieren

Module definieren ihre eigenen Felder in `SETTINGS_FIELDS`.
Framework-Felder bleiben in `app/config.py`.

Beispiel:

```python
SETTINGS_FIELDS = [
    {
        "name": "EXAMPLE_ENABLED",
        "label": "Beispiel aktiv",
        "type": "select",
        "default": "true",
        "wide": False,
        "options": [("true", "Aktiv"), ("false", "Inaktiv")],
        "help": "Aktiviert das Beispielmodul.",
    },
]
```

Typische Feldtypen im Projekt:

- `text`
- `password` (wird nie an die Oberflaeche zurueckgegeben; leer speichern = Wert behalten)
- `number` (endet der Name auf `_SECONDS`, zeigt die Oberflaeche das Feld als Dauer mit Einheit)
- `select`
- `checkbox_group`
- `priority_list` (Auswahl plus Reihenfolge per Ziehen)
- `list`: mehrere Zeilen mit Unterfeldern, gespeichert als `"a|b; c|d"`.
  `item_fields: [{"name": "label", "label": "Name"}, {"name": "url", "label": "Adresse", "wide": True}]`,
  optional `separator` (Standard `;`) und `joiner` (Standard `|`). Fehlt ein fuehrender Teil, wird nur
  der Rest gespeichert (`"URL"` statt `"|URL"`). Eine Spalte → einfache Liste `"a; b"`.
- `mapping`: Stichwort → Wert, gespeichert als `"k=v, k=v"`. Mit `value_options: [(wert, label), …]`
  wird der Wert zur Auswahl.

Optional koennen Felder `default`, `min`, `max`, `placeholder`, `help`, `options`, `datalist_url`,
`show_when` (`{"ANDERES_FELD": "wert"}`), `link_href`/`link_label` enthalten. Ein Modul liest die
Werte weiterhin als String ueber `get_setting()`; die Listen- und Zuordnungsformate parst es selbst
(siehe `modules/garbage/data_source.py` als Beispiel).


## Settings gruppieren

Mit `SETTINGS_GROUPS` lassen sich Felder in der Settings-Seite strukturieren:

```python
SETTINGS_GROUPS = [
    {
        "title": "Allgemein",
        "desc": "Grundkonfiguration des Moduls.",
        "fields": ["EXAMPLE_ENABLED"],
    },
]
```


## Laufzeit-Settings lesen

Module sollen ihre Werte generisch ueber `app.config` lesen, nicht ueber fest verdrahtete `cfg.<feldname>`-Attribute.

Empfohlene Helfer:

- `get_setting(name, default="")`
- `get_int_setting(name, default, min_val=None, max_val=None)`
- `get_bool_setting(name, default=False)`
- `get_csv_setting(name)`
- `get_cfg()` fuer Framework-Werte wie Rendergroesse, Theme, Rotation

Beispiel:

```python
from app.config import get_bool_setting, get_setting

enabled = get_bool_setting("EXAMPLE_ENABLED", True)
title = get_setting("EXAMPLE_TITLE", "Standardtitel")
```


## Render-Services

`ModuleRenderServices` (in `app/module_services.py`) buendelt das, was jeder Renderer braucht:
`render_width`, `render_height`, `display_theme` und `load_font`. Bewusst nicht mehr:
Module holen sich ihre eigenen Datenquellen selbst aus ihrem Paket.

```python
from app.module_services import ModuleRenderServices

def render(self, env, content):
    from .renderer import render_example
    return render_example(ModuleRenderServices.from_runtime(), content)
```

Gemeinsame Zeichen-Helfer fuer alle Module:

- `app/text_rendering.py`: `new_draw`, `page_scale`, `tile_scale`, `wrap_text` (kuerzt auch einzelne
  ueberlange Woerter mit „…“), `ellipsize`, `fit_wrapped_text`, `fit_optional_text_block`, `draw_lines`
- `app/image_rendering.py`: `resize_to_fit`, `fit_crop`, `create_blurred_cover_background`,
  `create_centered_cover_canvas`, `create_light_cover_canvas`, `create_rounded_thumbnail`,
  `draw_bottom_gradient`, `convert_to_spectra6`

Alles Modul-spezifische (Plex-Overlays, Tagesschau-Karten, Steam-Layout) liegt im jeweiligen Modulordner.

### E-Ink-Theme (`display_theme == "eink"`)

Das Panel kennt sechs Farben (`SPECTRA6_COLORS`). Alles, was nicht exakt eine davon ist, wird
beim Dithern zu bunten Punkten. Deshalb im E-Ink-Theme:

- Zeichenflaechen mit `new_draw(img, "RGBA")` statt `ImageDraw.Draw(...)` anlegen: Text wird dann
  ohne Kantenglaettung gesetzt (`fontmode = "1"`). Graue Kantenpixel wuerden sonst um jeden
  Buchstaben zu blauen und gruenen Punkten.
- Nur Spectra-Farben, alles deckend: keine halbtransparenten Toenungen, kein Blur, kein Verlauf,
  kein halbtransparentes Schwarz fuer „zurueckgenommenen“ Text (das ist Grau).
- Fotos bleiben Fotos: sie werden gedithert, am besten vorher mit `prepare_photo_for_eink()`.
- Groessen aus der 1200-px-Vorlage mit `page_scale(breite, hoehe)` (Vollbild) bzw.
  `tile_scale(breite)` (Kachel) umrechnen, damit auch 800 × 480 passt.

`tests/test_eink_rendering.py` prueft fuer die eingebauten Module, dass flache Seiten zu 100 %
auf der Palette liegen.

## Laufzeit-Status

`get_runtime_summary(self, env)` liefert `dict[str, str]` mit **menschenlesbaren Labels als Keys**,
z. B. `{"DWD-Station": "Giessen", "DWD-Cache": "900s"}`. Das Framework rendert daraus automatisch
Status-Karten auf der Settings-Seite und loggt sie beim Start. Der Wert `"Aktiv"` wird hervorgehoben.


## Optionale Hooks

`InkwallModule` bietet zusaetzlich optionale Hooks:

### `should_refresh(self, env)`

Erzwingt ein Re-Rendern trotz gleichem `state_key`.

### `get_state_key(self, content)`

Liefert einen Fingerprint fuer den aktuellen Inhalt.

### `validate_settings(self, updates, env)`

Modul-spezifische Validierung.
Rueckgabe: Liste von Fehlermeldungen.

### `get_runtime_summary(self, env)`

Status-Karten fuer die Settings-Seite und das Startlog. Rueckgabe `dict[str, str]`
mit lesbaren Labels als Keys (siehe Abschnitt "Laufzeit-Status").

### `get_field_options(self, field_name, env)`

Dynamische Optionen fuer Felder, z. B. API-basierte Vorschlagslisten.

### `handle_api_action(self, action, env)`

Optionale modulare API-Aktion.
Rueckgabe: `(payload, status_code)` oder `None`.

### `get_health_status(self, env)`

Optionale Moduldiagnose fuer `/health` und `/api/status`.

### `get_next_wake_seconds(self, env, state)`

Optionale modul-spezifische Wake-Empfehlung fuer `/meta.json`.

- Rueckgabe `None`: Framework-Standardlogik verwenden
- Rueckgabe `int`: Modul bestimmt `next_wake_sec` selbst

Das ist der richtige Hook, wenn ein Modul ein eigenes Anzeigetempo oder
Aktualisierungsintervall braucht, das vom allgemeinen Idle-Takt abweicht.

Beispiel:

```python
def get_next_wake_seconds(self, env: dict[str, str], state: str) -> int | None:
    if env.get("EXAMPLE_INTERVAL_MODE", "idle_rotation") == "custom":
        raw = env.get("EXAMPLE_INTERVAL_SECONDS", "300").strip()
        if raw.isdigit():
            return max(30, min(86400, int(raw)))
    return None
```

### `get_background_poll_seconds(self, env)`

Optionales Poll-Intervall fuer den Background-Worker.

- Rueckgabe `None`: Framework-Standardlogik verwenden
- Rueckgabe `int`: Modul kann haeufiger geprueft werden als das globale `REFRESH_INTERVAL`

Das ist sinnvoll, wenn ein Modul ein eigenes, kuerzeres Wechselintervall hat und
der Server deshalb auch haeufiger neu rendern koennen muss.

Beispiel:

```python
def get_background_poll_seconds(self, env: dict[str, str]) -> int | None:
    if env.get("EXAMPLE_INTERVAL_MODE", "idle_rotation") != "custom":
        return None
    raw = env.get("EXAMPLE_INTERVAL_SECONDS", "300").strip()
    if raw.isdigit():
        return max(30, min(86400, int(raw)))
    return None
```


### `render_tile(self, env, content, width, height)`

Kompakte Darstellung fuer den Dashboard-Modus (`IDLE_LAYOUT=dashboard`). Das Framework
ruft `fetch_content()` wie gewohnt auf und danach `render_tile()` mit der Kachelgroesse.
Mehrere Module teilen sich so ein Bild (Reihenfolge und Hoehen aus `DASHBOARD_TILES`).

- Rueckgabe `None` (Standard): Modul kann keine Kachel liefern und wird im Dashboard uebersprungen
- Rueckgabe `PIL.Image` in `width x height`: wird eingesetzt

Die Kachel sollte sich selbst benennen (kleine Titelzeile), aber kein Datum zeigen – das
steht in der Kopfzeile des Dashboards. Bewaehrt hat sich, den normalen Renderer mit einem
`compact=True`-Parameter wiederzuverwenden und die Skalierung an der Breite auszurichten.

```python
def render_tile(self, env, content, width, height):
    from .renderer import render_example
    base = ModuleRenderServices.from_runtime()
    services = ModuleRenderServices(render_width=width, render_height=height,
                                    display_theme=base.display_theme, load_font=base.load_font)
    return render_example(services, content, compact=True)
```

### `describe_status(self, env)`, `summarize(self, env)`, `probe(self, env)`

Drei kleine Hooks fuer die Oberflaeche. Mit ihnen bekommt ein Modul auf der Anzeige-Seite
einen Status-Chip, auf der Inhalte-Seite eine Zusammenfassung und einen Knopf "Pruefen".

- `describe_status(env)` → `{"state": "ready" | "missing" | "error", "reason": str}`.
  Bereitschaft unabhaengig vom Ein/Aus-Schalter. `missing` sagt, was fehlt ("ICS-Adresse fehlt").
  Standard: aus `get_health_status()` abgeleitet (`configured`, `ok`).
- `summarize(env)` → ein Satz fuer die eingeklappte Karte ("Giessen · UV Frankfurt").
  Standard: die Werte aus `get_runtime_summary()`.
- `describe_status` und `summarize` laufen innerhalb von `app.http_client.cache_only()`: die
  Anzeige-Seite fragt sie alle 30 s, `/metrics` bei jedem Scrape, auch fuer abgeschaltete Inhalte.
  Eine Datenquelle, die dafuer `fetch_*` aufruft, prueft vor dem Abruf `network_allowed()` und
  antwortet sonst mit dem, was sie schon hat (oder `None`) – ohne einen Fehlversuch zu vermerken.
- `probe(env)` → `{"ok": bool, "message": str, "details": [str, ...]}`, ruft die Quelle einmal ab.
  Standard: `fetch_content()` und "Daten vorhanden" / "keine Daten". `details` ist optional
  und erscheint als Kasten unter dem Pruef-Ergebnis; Zeilen mit Doppelpunkt am Ende werden
  als Ueberschrift gesetzt (die Muellabfuhr listet so die naechsten Termine und die erkannten Tonnen).

### `is_urgent(self, env)`

`True`, wenn das Modul gerade etwas Zeitkritisches zeigt (Muellabfuhr: morgen frueh ab der
eingestellten Abendstunde, oder heute bis zur "Erledigt"-Stunde). Dringende Idle-Module werden
in der Rotation vor jedes andere Modul geschoben (`[Muell, Wetter, Muell, News]`) und im
Dashboard nach oben sortiert; die Kachelhoehen bleiben. Standard: `False`. Der Hook wird bei
jedem Render-Durchlauf gefragt und muss deshalb aus dem Cache antworten.

Prioritaetsmodule setzen zusaetzlich `ENABLED_KEY` (z. B. `"PLEX_MODULE_ENABLED"`), damit die
Anzeige-Seite sie ueber denselben Schalter ein- und ausschalten kann wie Idle-Module.

## Dynamische Feldoptionen

Wenn ein Feld Optionen dynamisch laden soll:

1. Feld mit `datalist_url` oder passender Frontend-Logik definieren
2. `get_field_options()` im Modul implementieren
3. optional ueber `/api/module-field-options/<module_id>/<field_name>` abrufen

Beispiel aus dem DWD-Modul:

- Feld: `DWD_UV_CITY`
- Quelle: `get_field_options("DWD_UV_CITY", env)`


## Modul-API-Aktionen

Fuer modulare Hilfsendpunkte gibt es:

- `/api/module-action/<module_id>/<action>`

Beispiel:

```python
def handle_api_action(self, action: str, env: dict[str, str]):
    if action != "example-data":
        return None
    return {"items": ["a", "b", "c"]}, 200
```


## Health-Status

Module koennen Health-Infos liefern:

```python
def get_health_status(self, env: dict[str, str]) -> dict[str, object] | None:
    return {
        "ok": True,
        "enabled": self.is_enabled(env),
    }
```

Diese Infos fliessen in:

- `/health`
- `/api/status`


## Validierung

Framework-Validierung und Modul-Validierung laufen zusammen.

Das Modul sollte nur Regeln pruefen, die wirklich zu ihm gehoeren.

Beispiele:

- Pflichtkombinationen von Feldern
- gueltige Select-Werte
- Formatpruefungen fuer IDs oder URLs


## Tests

Fuer die Modularchitektur gibt es aktuell zwei Basistests:

- [test_module_registry.py](/C:/Users/tobia/Documents/Inkwall/tests/test_module_registry.py)
- [test_settings_validation.py](/C:/Users/tobia/Documents/Inkwall/tests/test_settings_validation.py)

Empfehlung fuer neue Module:

- mindestens ein Registry-/Lade-Szenario absichern
- mindestens eine Validierungsregel testen


## Praktische Checkliste fuer neue Module

1. Ordner unter `modules/<id>/` anlegen
2. `__init__.py` mit `module = ...` exportieren
3. `MODULE_ID`, `MODULE_NAME`, `MODULE_DESCRIPTION`, `MODULE_PRIORITY` setzen
4. `fetch_content()` und `render()` implementieren
5. Settings in `SETTINGS_FIELDS` deklarieren
6. bei Bedarf `SETTINGS_GROUPS` ergaenzen
7. Werte ueber `get_setting()` / `get_int_setting()` / `get_bool_setting()` lesen
8. optional `validate_settings()` ergaenzen
9. optional `get_runtime_summary()` ergaenzen
10. optional `get_field_options()` / `handle_api_action()` / `get_health_status()` ergaenzen
11. Tests ergaenzen


## Aktuelle Beispielmodule

- [plex](/C:/Users/tobia/Documents/Inkwall/modules/plex/__init__.py)
- [steam](/C:/Users/tobia/Documents/Inkwall/modules/steam/__init__.py)
- [dwd_weather](/C:/Users/tobia/Documents/Inkwall/modules/dwd_weather/__init__.py)
- [tagesschau](/C:/Users/tobia/Documents/Inkwall/modules/tagesschau/__init__.py)
