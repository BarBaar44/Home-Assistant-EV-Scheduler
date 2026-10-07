"""Trip semantics, free of Home Assistant: what an event means as a trip,
when the car must be ready, how much charge it needs, and which single plan
goes to the charger. Ported from the pyscript apps (ev_trip_energy,
trip_scheduler), where each rule has its history."""

from __future__ import annotations

import datetime as dt
import math
import re
from dataclasses import dataclass
from typing import Any

from .const import (
    FALLBACK_DETOUR_FACTOR,
    FALLBACK_SPEED_KMH,
    MARK_ARRIVAL,
    MARK_DEPARTURE,
    MARK_ONE_WAY,
    REACH_FAST_KMH,
    REACH_SLOW_KMH,
)

_GEO_PIN_RE = re.compile(r"GEO=\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")

type Coords = tuple[float, float]


# ----------------------------------------------------------------- events


def _coords(lat_s: str, lon_s: str) -> Coords | None:
    lat, lon = float(lat_s), float(lon_s)
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def parse_geo(value: str | None) -> Coords | None:
    """(lat, lon) from a "lat,lon" string, or None when unusable."""
    try:
        lat_s, lon_s = str(value or "").split(",", 1)
        return _coords(lat_s.strip(), lon_s.strip())
    except ValueError:
        return None


def pinned_coords(description: str | None) -> Coords | None:
    """(lat, lon) from a GEO=lat,lon line in an event description."""
    match = _GEO_PIN_RE.search(str(description or ""))
    return _coords(match.group(1), match.group(2)) if match else None


def is_one_way(description: str | None) -> bool:
    return MARK_ONE_WAY in (description or "")


def is_departure(event: dict[str, Any]) -> bool:
    """True when the event's start is when the car leaves. TIME_IS=ARRIVAL
    (arrive by) wins over `own`; TIME_IS=DEPARTURE or a trip this calendar
    organizes is a departure; an inbound invite is an arrival (its start is
    when you must BE there)."""
    description = event.get("description") or ""
    if MARK_ARRIVAL in description:
        return False
    return MARK_DEPARTURE in description or bool(event.get("own"))


def person_email(event: dict[str, Any]) -> str | None:
    """Whose trip it is: the invited member of a trip this calendar
    organizes, the organizer of an inbound invite."""
    if event.get("own"):
        attendees = event.get("attendees") or []
        return attendees[0] if attendees else None
    return event.get("organizer")


def short_name(display: str) -> str:
    """Leading part of a Nominatim display_name. Fallback only."""
    parts = [p.strip() for p in str(display).split(",") if p.strip()]
    return ", ".join(parts[:3]) if parts else str(display).strip()


def event_place(event: dict[str, Any]) -> str:
    """The place a person sees: SUMMARY minus "Trip to ", else the summary,
    else a short form of the location."""
    summary = str(event.get("summary") or "").strip()
    if summary.lower().startswith("trip to "):
        return summary[8:].strip()
    if summary:
        return summary
    return short_name(str(event.get("location") or ""))


def event_start(
    event: dict[str, Any], tz: dt.tzinfo, all_day_hour: int
) -> dt.datetime | None:
    """Aware local start of a list_events entry. All day events depart at
    `all_day_hour`. A naive time is local wall time, never UTC."""
    raw = event.get("start")
    if not raw:
        return None
    raw = str(raw)
    if not event.get("all_day"):
        try:
            parsed = dt.datetime.fromisoformat(raw)
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=tz)
            return parsed.astimezone(tz)
    try:
        day = dt.date.fromisoformat(raw[:10])
    except ValueError:
        return None
    return dt.datetime.combine(day, dt.time(all_day_hour), tz)


def is_cancelled(event: dict[str, Any]) -> bool:
    return str(event.get("status") or "").upper() == "CANCELLED"


# ------------------------------------------------------------ distances


def straight_km(a: Coords, b: Coords) -> float:
    """Great circle distance in km."""
    lat1, lon1 = a
    lat2, lon2 = b
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    h = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1))
        * math.cos(math.radians(lat2))
        * math.sin(dlon / 2) ** 2
    )
    return 2 * 6371.0 * math.asin(math.sqrt(h))


@dataclass(frozen=True)
class Route:
    """A trip's distance (both legs unless one way) and outbound drive."""

    km: float
    out_min: float
    estimated: bool = False


def estimate_route(home: Coords, dest: Coords, one_way: bool) -> Route:
    """Straight line × detour factor at a fixed speed, for when Waze
    fails. Erring long is the safe side for a charge plan."""
    leg = straight_km(home, dest) * FALLBACK_DETOUR_FACTOR
    return Route(
        km=leg if one_way else 2 * leg,
        out_min=leg / FALLBACK_SPEED_KMH * 60,
        estimated=True,
    )


# ---------------------------------------------------------- reachability


@dataclass(frozen=True)
class Reach:
    """Outcome of the cheap part of the reachability check."""

    verdict: str  # "ok", "late", or "ask" (Waze decides)
    fastest_min: float


def reach_bounds(home: Coords, dest: Coords, minutes_left: float) -> Reach:
    """Can an arrive by trip still be made, leaving now? Not even at
    REACH_FAST_KMH straight line: late, no Waze. Even at REACH_SLOW_KMH:
    fine, no Waze. In between: ask Waze."""
    km = straight_km(home, dest)
    fastest = km / REACH_FAST_KMH * 60
    if fastest > minutes_left:
        return Reach("late", fastest)
    if km / REACH_SLOW_KMH * 60 <= minutes_left:
        return Reach("ok", fastest)
    return Reach("ask", fastest)


def unreachable_text(
    place: str, arrive_at: dt.datetime, now: dt.datetime, drive_text: str
) -> str:
    return (
        f"You can't be in {place} by {arrive_at.strftime('%H:%M')}: the drive "
        f"takes {drive_text} and it's {now.strftime('%H:%M')} now. Pick a "
        f"later time, or choose Leave at."
    )


# ----------------------------------------------------------------- energy


def required_soc(
    km: float, wh_per_km: float, usable_kwh: float, buffer_pct: float
) -> tuple[float, float]:
    """(energy kWh, SOC % before the 100 cap)."""
    kwh = km * wh_per_km / 1000
    return kwh, kwh / usable_kwh * 100 + buffer_pct


def deadline(
    start: dt.datetime,
    departure: bool,
    out_min: float,
    prep_min: float,
    now: dt.datetime,
) -> dt.datetime:
    """When the car must be charged: the departure minus the prep buffer,
    where the departure is the start, or the start minus the drive for an
    arrival. Clamped to now."""
    drive = 0 if departure else out_min
    when = start - dt.timedelta(minutes=drive + prep_min)
    return max(when, now)


def next_floor_ready(now: dt.datetime, ready_hour: int) -> dt.datetime:
    """The next `ready_hour` strictly after now, local wall time."""
    ready = now.replace(hour=ready_hour, minute=0, second=0, microsecond=0)
    if ready <= now:
        ready += dt.timedelta(days=1)
    return ready


def choose_plan(
    trip_soc: float | None,
    trip_deadline: dt.datetime | None,
    current_soc: float | None,
    now: dt.datetime,
    floor_soc: float,
    floor_ready_hour: int,
) -> tuple[float, dt.datetime | None, str]:
    """The ONE plan for the charger: (soc, deadline, kind), kind trip,
    floor or idle. evcc only works towards its next plan.

    * no trip, no floor                -> idle
    * no trip                          -> floor by the next ready hour
    * trip due before that hour        -> max(trip, floor) by the trip deadline
    * trip due later, car below the
      floor (or SOC unknown)           -> floor by the next ready hour first
    * trip due later, car at or above  -> max(trip, floor) by the trip deadline
    """
    if trip_soc is None:
        if floor_soc <= 0:
            return 0.0, None, "idle"
        return floor_soc, next_floor_ready(now, floor_ready_hour), "floor"
    if floor_soc <= 0:
        return trip_soc, trip_deadline, "trip"
    ready = next_floor_ready(now, floor_ready_hour)
    combined = max(trip_soc, floor_soc)
    if trip_deadline is not None and trip_deadline <= ready:
        return combined, trip_deadline, "trip"
    if current_soc is None or current_soc < floor_soc:
        return floor_soc, ready, "floor"
    return combined, trip_deadline, "trip"


# ----------------------------------------------------------------- search


def place_name(candidate: dict[str, Any]) -> str:
    """ "street number, town" from Nominatim's structured fields, never by
    slicing display_name (that showed the neighbourhood, hid the town)."""
    road = candidate.get("road")
    city = candidate.get("city")
    number = candidate.get("house_number")
    if road and city:
        street = f"{road} {number}" if number else road
        return f"{street}, {city}"
    if city:
        return city
    return short_name(candidate.get("display", ""))


def query_house_number(query: str) -> str | None:
    """The bare number the user typed, if any. Only used for a warning."""
    for token in str(query).replace(",", " ").split():
        if token.isdigit():
            return token
    return None


def number_warning(candidate: dict[str, Any], wanted: str | None) -> str | None:
    """ "number 12, not 14" when Nominatim substituted another house number."""
    if not wanted:
        return None
    got = candidate.get("house_number")
    if str(got or "") == str(wanted):
        return None
    return f"number {got or 'n/a'}, not {wanted}"


def search_results(
    query: str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Contract search results, Nominatim's order kept, identical lines
    (one street stored as several OSM segments) collapsed to the first."""
    wanted = query_house_number(query)
    results = []
    seen = set()
    for c in candidates:
        place = place_name(c)
        km = c.get("km")
        warning = number_warning(c, wanted)
        key = (place, round(km) if km is not None else None, warning)
        if key in seen:
            continue
        seen.add(key)
        results.append(
            {
                "place": place,
                "location": c["display"],
                "geo": f"{c['lat']:.6f},{c['lon']:.6f}",
                "km": round(km, 1) if km is not None else None,
                "warning": warning,
            }
        )
    return results
