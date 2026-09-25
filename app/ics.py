"""
ICS-Kalender lesen – gemeinsam für Kalender und Müllabfuhr.

Grundlage sind icalendar und recurring-ical-events. Damit stimmt, was ein
eigener kleiner Parser nicht sauber hinbekam:

- Wiederholungen mit allen Regeln (RRULE inkl. BYDAY „2TU“, BYMONTHDAY,
  BYSETPOS, RDATE, EXDATE), ohne Obergrenze ab DTSTART – eine tägliche Serie
  von 2024 hört nicht nach 500 Terminen auf
- verschobene und abgesagte Einzeltermine (RECURRENCE-ID, STATUS:CANCELLED)
- Zeitzonen aus VTIMEZONE, TZID mit Präfix (Thunderbird), Outlook-/Windows-
  Namen, TZID mit Doppelpunkt in Anführungszeichen (Exchange), X-WR-TIMEZONE
- eine kaputte Serie wird übersprungen, nicht der ganze Kalender

Termine kommen als einfache dicts zurück, Zeiten in der eingestellten Zeitzone:

    {"start", "end", "all_day", "summary", "location", "description", "uid"}

start/end sind bei ganztägigen Terminen date, sonst tz-aware datetime; end ist
exklusiv (ganztägig: der Tag nach dem letzten Tag).
"""

from __future__ import annotations

from datetime import date, datetime, time, timedelta

import icalendar
import recurring_ical_events

from app.config import local_tz
from app.logger import get_logger

log = get_logger(__name__)


class IcsError(ValueError):
    """Der Text ist kein lesbarer Kalender (z. B. eine HTML-Fehlerseite)."""


def parse_calendar(text: str):
    """ICS-Text → icalendar.Calendar. Wirft IcsError, wenn es kein Kalender ist."""
    if not text or "BEGIN:VCALENDAR" not in text[:4096].upper():
        raise IcsError("Kein Kalender (BEGIN:VCALENDAR fehlt)")
    try:
        calendar = icalendar.Calendar.from_ical(text)
    except Exception as exc:
        raise IcsError(f"Kalender nicht lesbar: {str(exc)[:120]}") from exc
    # Termine ohne Beginn kann niemand anzeigen – und recurring-ical-events
    # bricht an ihnen für den ganzen Kalender ab
    broken = [c for c in calendar.subcomponents if c.name == "VEVENT" and c.get("DTSTART") is None]
    for component in broken:
        calendar.subcomponents.remove(component)
    return calendar


def _series_calendars(calendar):
    """Je UID ein eigener Kalender (Serie samt Ausnahmen, Zeitzonen des Originals) – zum Auflösen einzeln."""
    groups: dict[str, list] = {}
    for component in calendar.walk("VEVENT"):
        uid = str(component.get("UID", "") or "") or f"ohne-uid-{id(component)}"
        groups.setdefault(uid, []).append(component)
    timezones = calendar.walk("VTIMEZONE")
    for components in groups.values():
        part = icalendar.Calendar()
        for key in ("VERSION", "PRODID", "X-WR-TIMEZONE"):
            if key in calendar:
                part[key] = calendar[key]
        for component in timezones + components:
            part.add_component(component)
        yield part


def _expand(calendar, start: date, stop: date) -> list:
    try:
        return list(recurring_ical_events.of(calendar, skip_bad_series=True).between(start, stop))
    except Exception as exc:
        # Eine Serie, die auch skip_bad_series nicht abfängt: jede Serie einzeln,
        # nur die kaputte fällt weg
        log.debug(f"Kalender: Auflösen im Ganzen gescheitert ({exc}), Serie für Serie")
    found: list = []
    for part in _series_calendars(calendar):
        try:
            found.extend(recurring_ical_events.of(part, skip_bad_series=True).between(start, stop))
        except Exception as exc:
            log.debug(f"Kalender: Serie übersprungen ({exc})")
    return found


def as_date(value) -> date:
    return value.date() if isinstance(value, datetime) else value


def _local(value, tz):
    """datetime in die eingestellte Zeitzone; „floating“ (ohne Zone) gilt als lokal. date bleibt date."""
    if isinstance(value, datetime):
        return value.replace(tzinfo=tz) if value.tzinfo is None else value.astimezone(tz)
    return value


def _text(component, name: str) -> str:
    value = component.get(name)
    if value is None:
        return ""
    return " ".join(str(value).split())       # Zeilenumbrüche und doppelte Leerzeichen raus


def _occurrence(component, tz) -> dict | None:
    if str(component.get("STATUS", "") or "").strip().upper() == "CANCELLED":
        return None
    summary = _text(component, "SUMMARY")
    dtstart = component.get("DTSTART")
    if not summary or dtstart is None:
        return None
    start = dtstart.dt
    all_day = isinstance(start, date) and not isinstance(start, datetime)
    end = None
    if component.get("DTEND") is not None:
        end = component.get("DTEND").dt
    elif component.get("DURATION") is not None:
        end = start + component.get("DURATION").dt
    if end is None:
        end = start + timedelta(days=1) if all_day else start
    # Gemischte Angaben (ganztägiger Start, Ende mit Uhrzeit) – der Kalender ist
    # kaputt, der Termin soll trotzdem erscheinen
    if all_day and isinstance(end, datetime):
        end = max(end.date(), start + timedelta(days=1))
    elif not all_day and not isinstance(end, datetime):
        end = datetime.combine(end, time.min)
    start, end = _local(start, tz), _local(end, tz)
    return {
        "start": start,
        "end": end,
        "all_day": all_day,
        "summary": summary,
        "location": _text(component, "LOCATION"),
        "description": _text(component, "DESCRIPTION"),
        "uid": _text(component, "UID"),
    }


def _sort_key(o: dict):
    s = o["start"]
    return (as_date(s), 0 if o["all_day"] else 1, s.timetuple()[3:5] if isinstance(s, datetime) else (0, 0), o["summary"])


def occurrences(calendar, start: date, end: date) -> list[dict]:
    """
    Alle Termine, die den Zeitraum [start, end] berühren (Tage, beide
    einschließlich), sortiert nach Tag, ganztägig zuerst, dann Uhrzeit.
    Abgesagte Termine und Termine ohne Titel fehlen.
    """
    tz = local_tz()
    found = _expand(calendar, start, end + timedelta(days=1))
    result: list[dict] = []
    for component in found:
        try:
            occ = _occurrence(component, tz)
        except Exception as exc:
            log.debug(f"Kalender: Termin übersprungen ({exc})")
            continue
        if occ is not None:
            result.append(occ)
    result.sort(key=_sort_key)
    return result
