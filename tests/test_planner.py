"""Pure trip rules."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

from custom_components.ev_trip_planner import planner

TZ = ZoneInfo("Europe/Amsterdam")
NOW = dt.datetime(2026, 10, 7, 15, 0, tzinfo=TZ)


def test_departure_rule() -> None:
    assert planner.is_departure({"own": True})
    assert planner.is_departure({"description": "TIME_IS=DEPARTURE"})
    assert not planner.is_departure({"own": False})
    # Arrive by wins over own.
    assert not planner.is_departure({"own": True, "description": "TIME_IS=ARRIVAL"})


def test_person_email() -> None:
    assert planner.person_email({"own": True, "attendees": ["a@x"]}) == "a@x"
    assert planner.person_email({"own": True, "attendees": []}) is None
    assert planner.person_email({"own": False, "organizer": "o@x"}) == "o@x"


def test_geo() -> None:
    assert planner.parse_geo("52.0,4.3") == (52.0, 4.3)
    assert planner.parse_geo("garbage") is None
    assert planner.parse_geo("91,0") is None
    assert planner.pinned_coords("TRIP_TYPE=ONE_WAY\nGEO=52.1, 4.2") == (52.1, 4.2)
    assert planner.pinned_coords("TRIP_TYPE=ONE_WAY") is None


def test_event_start_all_day_and_naive() -> None:
    start = planner.event_start({"start": "2026-10-09", "all_day": True}, TZ, 8)
    assert start == dt.datetime(2026, 10, 9, 8, tzinfo=TZ)
    naive = planner.event_start({"start": "2026-10-09T10:00:00"}, TZ, 8)
    assert naive == dt.datetime(2026, 10, 9, 10, tzinfo=TZ)
    utc = planner.event_start({"start": "2026-10-09T08:00:00+00:00"}, TZ, 8)
    assert utc.hour == 10


def test_deadline() -> None:
    start = NOW + dt.timedelta(hours=5)
    assert planner.deadline(start, True, 60, 15, NOW) == start - dt.timedelta(
        minutes=15
    )
    assert planner.deadline(start, False, 60, 15, NOW) == start - dt.timedelta(
        minutes=75
    )
    # Clamped to now.
    soon = NOW + dt.timedelta(minutes=30)
    assert planner.deadline(soon, False, 60, 15, NOW) == NOW.replace(second=0)


def test_deadline_whole_minutes() -> None:
    """A fractional drive time or the clamp to now never leaves seconds."""
    start = NOW.replace(second=0) + dt.timedelta(hours=5)
    got = planner.deadline(start, False, 37.4, 15, NOW)
    assert (got.second, got.microsecond) == (0, 0)
    assert got == start - dt.timedelta(minutes=53)  # 52.4 rounded down
    late = NOW.replace(second=40)
    got = planner.deadline(late, False, 60, 15, late)
    assert got == late.replace(second=0)


def test_required_soc() -> None:
    kwh, soc = planner.required_soc(120, 153, 79, 10)
    assert round(kwh, 2) == 18.36
    assert round(soc, 1) == round(18.36 / 79 * 100 + 10, 1)


def test_choose_plan_table() -> None:
    ready = dt.datetime(2026, 10, 8, 7, tzinfo=TZ)
    early = NOW + dt.timedelta(hours=10)  # before the 07:00 ready hour
    late = NOW + dt.timedelta(days=3)
    assert planner.choose_plan(None, None, None, NOW, 0, 7) == (0.0, None, "idle")
    assert planner.choose_plan(None, None, None, NOW, 50, 7) == (50, ready, "floor")
    assert planner.choose_plan(30, early, 40, NOW, 50, 7) == (50, early, "trip")
    assert planner.choose_plan(90, late, 40, NOW, 50, 7) == (50, ready, "floor")
    assert planner.choose_plan(90, late, None, NOW, 50, 7) == (50, ready, "floor")
    assert planner.choose_plan(90, late, 60, NOW, 50, 7) == (90, late, "trip")
    assert planner.choose_plan(90, late, 10, NOW, 0, 7) == (90, late, "trip")


def test_reach_bounds() -> None:
    home, haarlem = (52.09, 5.12), (52.38, 4.64)  # ~46 km straight
    assert planner.reach_bounds(home, haarlem, 10).verdict == "late"
    assert planner.reach_bounds(home, haarlem, 40).verdict == "ask"
    assert planner.reach_bounds(home, haarlem, 24 * 60).verdict == "ok"


def test_search_results() -> None:
    candidates = [
        {
            "display": "Lidl, 119, Papsouwselaan, Delft",
            "lat": 52.0012,
            "lon": 4.3712,
            "km": 46.23,
            "road": "Papsouwselaan",
            "house_number": "119",
            "city": "Delft",
        },
        # Same street, another OSM segment: collapsed.
        {
            "display": "Lidl, 119, Papsouwselaan, Delft (2)",
            "lat": 52.0013,
            "lon": 4.3713,
            "km": 46.3,
            "road": "Papsouwselaan",
            "house_number": "119",
            "city": "Delft",
        },
    ]
    results = planner.search_results("lidl 14 delft", candidates)
    assert results == [
        {
            "place": "Papsouwselaan 119, Delft",
            "location": "Lidl, 119, Papsouwselaan, Delft",
            "geo": "52.001200,4.371200",
            "km": 46.2,
            "warning": "number 119, not 14",
        }
    ]


def test_estimate_route() -> None:
    route = planner.estimate_route((52.09, 5.12), (52.38, 4.64), False)
    assert route.estimated
    assert 110 < route.km < 125
    assert route.out_min > 0
