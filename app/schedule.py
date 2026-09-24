"""
Zeitplan: Zeitfenster je Wochentag mit eigenen Inhalten, Layout und Takt.

Ein Fenster gilt an den gewählten Wochentagen von Start bis Ende (über
Mitternacht hinaus, wenn das Ende vor dem Start liegt – der Wochentag ist
dann der Tag, an dem das Fenster beginnt). Passt gerade ein Fenster, gelten
seine Inhalte, sein Layout und sein Takt statt des Programms; leere Felder
erben vom Programm. Mehrere passende Fenster: das erste in der Liste gewinnt.
Ohne passendes Fenster gilt das Programm.

Gespeichert wird alles in SCHEDULE_WINDOWS, ein Fenster je Eintrag:

    Name|Tage|HH:MM-HH:MM|layout|sekunden|inhalte; Name|…

    Tage:     * (täglich), Mo-Fr, Sa,So, Mo,Mi,Fr, Mo-Do,So
    layout:   rotation, dashboard oder leer (wie im Programm)
    sekunden: Takt in Sekunden oder leer (wie im Programm)
    inhalte:  modul:höhe,modul,… oder leer (alle Inhalte des Programms)

Der alte Nachtmodus (NIGHT_MODE_*) wird als ein Fenster „Nachts“ abgebildet,
solange kein Zeitplan gespeichert ist – so bleibt eine bestehende
Konfiguration ohne Migration gültig.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta

DAY_NAMES = ("Mo", "Di", "Mi", "Do", "Fr", "Sa", "So")
_DAY_INDEX = {name.lower(): i for i, name in enumerate(DAY_NAMES)}
ALL_DAYS = frozenset(range(7))
LAYOUTS = ("", "rotation", "dashboard")
MIN_INTERVAL_SECONDS = 30
MAX_INTERVAL_SECONDS = 24 * 3600
MAX_WINDOWS = 12
_TIME_RE = re.compile(r"^(?:[01]\d|2[0-3]):[0-5]\d$")


@dataclass(frozen=True)
class Window:
    name: str
    days: frozenset = ALL_DAYS
    start: int = 0                      # Minuten seit Mitternacht
    end: int = 0                        # end <= start: läuft über Mitternacht
    layout: str = ""                    # "" = wie im Programm
    interval_seconds: int = 0           # 0 = wie im Programm
    content: tuple = field(default_factory=tuple)   # ((modul, prozent), …), leer = alle Inhalte des Programms

    @property
    def start_text(self) -> str:
        return _minutes_to_text(self.start)

    @property
    def end_text(self) -> str:
        return _minutes_to_text(self.end)

    @property
    def label(self) -> str:
        return f"{self.start_text}–{self.end_text}"

    @property
    def module_ids(self) -> tuple:
        return tuple(mid for mid, _ in self.content)

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "days": sorted(self.days),
            "start": self.start_text,
            "end": self.end_text,
            "layout": self.layout,
            "interval_seconds": self.interval_seconds,
            "content": [{"id": mid, "height": pct or None} for mid, pct in self.content],
            "days_text": describe_days(self.days),
        }


# ---------------------------------------------------------------------------
# Text ↔ Fenster
# ---------------------------------------------------------------------------

def _minutes_to_text(minutes: int) -> str:
    minutes = max(0, min(24 * 60 - 1, int(minutes)))
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def parse_time(text: str) -> int | None:
    text = (text or "").strip()
    if not _TIME_RE.match(text):
        return None
    hh, mm = text.split(":")
    return int(hh) * 60 + int(mm)


def parse_days(text: str) -> frozenset | None:
    """'Mo-Fr', 'Sa,So', '*', 'Mo,Mi,Fr', 'Mo-Do,So' → Menge von Wochentagen (0 = Mo). None bei Fehler."""
    text = (text or "").strip().lower()
    if text in ("", "*", "täglich", "taeglich", "alle"):
        return ALL_DAYS
    days: set[int] = set()
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            a, b = (p.strip() for p in chunk.split("-", 1))
            if a not in _DAY_INDEX or b not in _DAY_INDEX:
                return None
            i, j = _DAY_INDEX[a], _DAY_INDEX[b]
            if i <= j:
                days.update(range(i, j + 1))
            else:                       # Fr-Mo: über das Wochenende
                days.update(range(i, 7))
                days.update(range(0, j + 1))
        elif chunk in _DAY_INDEX:
            days.add(_DAY_INDEX[chunk])
        else:
            return None
    return frozenset(days) if days else None


def describe_days(days) -> str:
    """Menge → 'täglich', 'Mo–Fr', 'Sa, So', 'Mo, Mi, Fr'."""
    days = sorted(set(days))
    if not days:
        return "nie"
    if len(days) == 7:
        return "täglich"
    if len(days) == 5 and days == [0, 1, 2, 3, 4]:
        return "Mo–Fr"
    if days == [5, 6]:
        return "Sa, So"
    # Zusammenhängende Bereiche ab 3 Tagen als Bereich schreiben
    parts: list[str] = []
    i = 0
    while i < len(days):
        j = i
        while j + 1 < len(days) and days[j + 1] == days[j] + 1:
            j += 1
        if j - i >= 2:
            parts.append(f"{DAY_NAMES[days[i]]}–{DAY_NAMES[days[j]]}")
        else:
            parts.extend(DAY_NAMES[d] for d in days[i:j + 1])
        i = j + 1
    return ", ".join(parts)


def _days_to_storage(days) -> str:
    days = sorted(set(days))
    if len(days) == 7:
        return "*"
    return ",".join(DAY_NAMES[d] for d in days)


def _parse_content(text: str) -> tuple:
    out: list[tuple[str, int]] = []
    seen: set[str] = set()
    for chunk in (text or "").split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        mid, _, pct_raw = chunk.partition(":")
        mid = mid.strip().lower()
        if not mid or mid in seen:
            continue
        try:
            pct = max(0, min(100, int(pct_raw.strip()))) if pct_raw.strip() else 0
        except ValueError:
            pct = 0
        out.append((mid, pct))
        seen.add(mid)
    return tuple(out)


def _clean_name(name: str) -> str:
    return re.sub(r"[|;]+", " ", (name or "")).strip()[:40]


def parse_windows(raw: str) -> list[Window]:
    """Tolerant: unbrauchbare Einträge werden übersprungen (validate_raw meldet sie)."""
    windows: list[Window] = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")]
        parts += [""] * (6 - len(parts))
        name, days_text, span, layout, seconds_text, content_text = parts[:6]
        days = parse_days(days_text)
        if days is None or "-" not in span:
            continue
        start, end = (parse_time(p) for p in span.split("-", 1))
        if start is None or end is None:
            continue
        layout = layout.lower()
        if layout not in LAYOUTS:
            layout = ""
        try:
            seconds = int(seconds_text) if seconds_text else 0
        except ValueError:
            seconds = 0
        if seconds and not MIN_INTERVAL_SECONDS <= seconds <= MAX_INTERVAL_SECONDS:
            seconds = max(MIN_INTERVAL_SECONDS, min(MAX_INTERVAL_SECONDS, seconds))
        windows.append(Window(
            name=_clean_name(name) or f"Fenster {len(windows) + 1}",
            days=days, start=start, end=end, layout=layout,
            interval_seconds=seconds, content=_parse_content(content_text),
        ))
    return windows[:MAX_WINDOWS]


def serialize_windows(windows: list[Window]) -> str:
    chunks: list[str] = []
    for w in windows:
        content = ",".join(f"{mid}:{pct}" if pct else mid for mid, pct in w.content)
        chunks.append("|".join([
            _clean_name(w.name), _days_to_storage(w.days), f"{w.start_text}-{w.end_text}",
            w.layout, str(w.interval_seconds) if w.interval_seconds else "", content,
        ]))
    return "; ".join(chunks)


def window_from_dict(data: dict, index: int = 0) -> Window:
    """JSON der Oberfläche → Fenster. Ungültige Teile werden auf sichere Werte gesetzt; validate_windows prüft."""
    days_raw = data.get("days")
    if isinstance(days_raw, str):
        days = parse_days(days_raw) or frozenset()
    else:
        days = frozenset(int(d) for d in (days_raw or []) if str(d).strip().lstrip("-").isdigit() and 0 <= int(d) <= 6)
    start = parse_time(str(data.get("start", "")))
    end = parse_time(str(data.get("end", "")))
    layout = str(data.get("layout", "") or "").strip().lower()
    try:
        seconds = int(data.get("interval_seconds") or 0)
    except (TypeError, ValueError):
        seconds = -1
    content: list[tuple[str, int]] = []
    for item in data.get("content") or []:
        if isinstance(item, dict):
            mid = str(item.get("id", "")).strip().lower()
            height = item.get("height")
        else:
            mid, height = str(item).strip().lower(), 0
        if not mid or any(mid == existing for existing, _ in content):
            continue
        try:
            pct = int(height) if height not in (None, "", "auto") else 0
        except (TypeError, ValueError):
            pct = 0
        content.append((mid, max(0, min(100, pct))))
    return Window(
        name=_clean_name(str(data.get("name", ""))) or f"Fenster {index + 1}",
        days=days,
        start=start if start is not None else -1,
        end=end if end is not None else -1,
        layout=layout,
        interval_seconds=seconds,
        content=tuple(content),
    )


def validate_windows(windows: list[Window], known_module_ids=None) -> list[str]:
    """Fehlermeldungen mit dem Präfix 'Zeitplan:' (Feld SCHEDULE_WINDOWS)."""
    errors: list[str] = []
    if len(windows) > MAX_WINDOWS:
        errors.append(f"Zeitplan: Höchstens {MAX_WINDOWS} Zeitfenster.")
    for w in windows:
        who = f"Zeitplan: Fenster „{w.name}“"
        if not w.days:
            errors.append(f"{who} hat keinen Wochentag.")
        if w.start < 0 or w.end < 0:
            errors.append(f"{who}: Bitte Von und Bis im Format HH:MM angeben.")
        elif w.start == w.end:
            errors.append(f"{who}: Von und Bis dürfen nicht gleich sein.")
        if w.layout not in LAYOUTS:
            errors.append(f"{who}: Unbekannte Darstellung.")
        if w.interval_seconds and not MIN_INTERVAL_SECONDS <= w.interval_seconds <= MAX_INTERVAL_SECONDS:
            errors.append(f"{who}: Takt zwischen {MIN_INTERVAL_SECONDS} s und 24 h wählen.")
        if w.interval_seconds < 0:
            errors.append(f"{who}: Takt muss eine Zahl in Sekunden sein.")
        if known_module_ids is not None:
            unknown = [mid for mid in w.module_ids if mid not in known_module_ids]
            if unknown:
                errors.append(f"{who}: Unbekannter Inhalt {', '.join(unknown)}.")
    return errors


def validate_raw(raw: str) -> list[str]:
    """Syntaxprüfung eines gespeicherten Werts (handgepflegte Datei, Import)."""
    errors: list[str] = []
    for chunk in (raw or "").split(";"):
        chunk = chunk.strip()
        if not chunk:
            continue
        parts = [p.strip() for p in chunk.split("|")] + [""] * 6
        name = parts[0] or "?"
        if parse_days(parts[1]) is None:
            errors.append(f"Zeitplan: Fenster „{name}“: Wochentage unlesbar ({parts[1]}).")
        span = parts[2]
        if "-" not in span or any(parse_time(p) is None for p in span.split("-", 1)):
            errors.append(f"Zeitplan: Fenster „{name}“: Zeitraum als HH:MM-HH:MM angeben.")
    return errors


# ---------------------------------------------------------------------------
# Auswertung
# ---------------------------------------------------------------------------

def _window_active_at(w: Window, now: datetime) -> bool:
    cur = now.hour * 60 + now.minute
    weekday = now.weekday()
    if w.start < w.end:
        return weekday in w.days and w.start <= cur < w.end
    # Über Mitternacht: der Wochentag ist der Tag, an dem das Fenster beginnt
    if cur >= w.start:
        return weekday in w.days
    if cur < w.end:
        return (weekday - 1) % 7 in w.days
    return False


def _after(dt: datetime, now: datetime) -> datetime | None:
    """
    dt, wenn es wirklich nach now liegt. Verglichen wird über den Zeitstempel:
    datetimes mit derselben ZoneInfo vergleicht Python nach Wanduhr, das geht
    in den Nächten der Zeitumstellung eine Stunde daneben. In der doppelten
    Stunde (Herbst) zählt notfalls das zweite Vorkommen der Uhrzeit.
    """
    if dt.timestamp() > now.timestamp():
        return dt
    later = dt.replace(fold=1)
    return later if later.timestamp() > now.timestamp() else None


def _end_datetime(w: Window, now: datetime) -> datetime:
    """Ende des gerade aktiven Fensters als Zeitpunkt."""
    base = now.replace(second=0, microsecond=0, fold=0)
    end = base.replace(hour=w.end // 60, minute=w.end % 60)
    if w.start >= w.end and (now.hour * 60 + now.minute) >= w.start:
        end += timedelta(days=1)
    return end


def _next_start(w: Window, now: datetime) -> datetime | None:
    """Nächster Beginn des Fensters ab jetzt (bis zu 8 Tage voraus)."""
    base = now.replace(second=0, microsecond=0, fold=0)
    for offset in range(0, 8):
        day = base + timedelta(days=offset)
        if day.weekday() not in w.days:
            continue
        start = _after(day.replace(hour=w.start // 60, minute=w.start % 60), now)
        if start is not None:
            return start
    return None


def active_window(windows: list[Window], now: datetime) -> tuple[Window | None, int, Window | None]:
    """
    (aktives Fenster oder None, Sekunden bis zur nächsten Änderung, nächstes beginnendes Fenster).
    Änderung = Ende des aktiven Fensters oder Beginn irgendeines Fensters, was
    zuerst kommt. 0 Sekunden = keine Änderung absehbar.
    """
    active = next((w for w in windows if _window_active_at(w, now)), None)
    candidates: list[tuple[datetime, Window | None]] = []
    if active is not None:
        end = _after(_end_datetime(active, now), now)
        if end is not None:
            candidates.append((end, None))
    for w in windows:
        if w is active:
            continue
        start = _next_start(w, now)
        if start is not None:
            candidates.append((start, w))
    if not candidates:
        return active, 0, None
    # Echte Sekunden (Zeitstempel), nicht die Differenz der Wanduhr – sonst weckt
    # der Zeitplan in den Nächten der Zeitumstellung eine Stunde zu früh oder zu spät
    when, upcoming = min(candidates, key=lambda c: c[0].timestamp())
    seconds = max(1, int(when.timestamp() - now.timestamp()))
    return active, seconds, upcoming


def legacy_night_window(cfg) -> Window | None:
    """Alter Nachtmodus als Fenster, damit beide Wege dieselbe Auswertung nutzen."""
    if not getattr(cfg, "night_mode_enabled", False):
        return None
    start, end = int(cfg.night_mode_start_minutes), int(cfg.night_mode_end_minutes)
    if start == end:
        return None
    content: tuple = ()
    if getattr(cfg, "night_mode_idle_behavior", "rotate") == "fixed" and getattr(cfg, "night_mode_fixed_module_id", ""):
        content = ((cfg.night_mode_fixed_module_id, 0),)
    return Window(name="Nachts", days=ALL_DAYS, start=start, end=end, layout="",
                  interval_seconds=int(getattr(cfg, "night_mode_interval_seconds", 900)), content=content)


def effective_windows(cfg) -> list[Window]:
    """Gespeicherter Zeitplan, sonst der alte Nachtmodus als einziges Fenster."""
    windows = list(getattr(cfg, "schedule_windows", ()) or ())
    if windows:
        return windows
    legacy = legacy_night_window(cfg)
    return [legacy] if legacy else []


def describe_window(w: Window, module_names: dict | None = None) -> str:
    """Ein Satz für die Oberfläche und das Log: 'Mo–Fr 06:00–09:00 · Rotation · Wetter, Müllabfuhr · alle 2 min'."""
    names = module_names or {}
    parts = [f"{describe_days(w.days)} {w.label}"]
    if w.layout:
        parts.append("Dashboard" if w.layout == "dashboard" else "Rotation")
    if w.content:
        parts.append(", ".join(names.get(mid, mid) + (f" {pct} %" if pct else "") for mid, pct in w.content))
    else:
        parts.append("alle Inhalte des Programms")
    if w.interval_seconds:
        s = w.interval_seconds
        parts.append("alle " + (f"{s // 3600} h" if s % 3600 == 0 else f"{s // 60} min" if s % 60 == 0 else f"{s} s"))
    return " · ".join(parts)
