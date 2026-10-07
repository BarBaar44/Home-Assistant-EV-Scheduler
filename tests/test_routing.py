"""The Waze request and its two response shapes (the routing request
pyscript made live since 5 Oct 2026)."""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import pytest

from custom_components.ev_trip_planner import routing

TZ = ZoneInfo("Europe/Amsterdam")


class FakeResp:
    def __init__(self, data, status=200):
        self._data = data
        self.status_code = status
        self.text = str(data)

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, data, status=200):
        self.data, self.status, self.params = data, status, None

    def get(self, url, params, headers, timeout):
        assert url == routing.WAZE_ROUTING_URL
        assert headers["referer"] == "https://www.waze.com/"
        self.params = params
        return FakeResp(self.data, self.status)


SEGMENTS = [{"crossTime": 600, "length": 10000}, {"cross_time": 300, "length": 5000}]


def test_leg_alternatives_shape() -> None:
    session = FakeSession({"alternatives": [{"response": {"results": SEGMENTS}}]})
    km, minutes = routing._leg(session, (52.09, 5.12), (52.38, 4.64), 42)
    assert (km, minutes) == (15.0, 15.0)
    # x is longitude, y latitude; `at` is minutes from now.
    assert session.params["from"] == "x:5.12 y:52.09"
    assert session.params["to"] == "x:4.64 y:52.38"
    assert session.params["at"] == 42


def test_leg_response_list_shape() -> None:
    session = FakeSession({"response": [{"result": SEGMENTS}]})
    assert routing._leg(session, (0, 0), (1, 1), 0) == (15.0, 15.0)


@pytest.mark.parametrize(
    "data,status",
    [({"error": "no route"}, 200), ({}, 403), ({"response": {"results": []}}, 200)],
)
def test_leg_failures(data, status) -> None:
    with pytest.raises(Exception):  # noqa: B017 - any failure counts
        routing._leg(FakeSession(data, status), (0, 0), (1, 1), 0)


def test_minutes_ahead() -> None:
    now = dt.datetime(2026, 10, 7, 15, 0, tzinfo=TZ)
    assert routing._minutes_ahead(now + dt.timedelta(hours=2), now) == 120
    assert routing._minutes_ahead(now - dt.timedelta(hours=1), now) == 0
    assert routing._minutes_ahead(now + dt.timedelta(days=4), now) == 0
