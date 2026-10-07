"""Waze routing, on predicted traffic at the departure time.

Waze refuses plain HTTP clients (it checks the TLS fingerprint), and Home
Assistant pins a pywaze without the fix, so the routing request is made
here with curl_cffi impersonating a browser: the same request pywaze makes.
The request's `at` field is the departure in minutes from now, which makes
Waze answer with predicted traffic (a 17:30 trip booked at 15:00 is timed
on rush hour, not on mid-afternoon traffic).

Results are cached per destination, trip type and departure hour."""

from __future__ import annotations

import datetime as dt
import time
from typing import Any

from homeassistant.core import HomeAssistant

from .const import (
    LOGGER,
    NEAR_TRIP_HOURS,
    ROUTE_CACHE_HOURS,
    ROUTE_PREDICT_MAX_HOURS,
    WAZE_ROUTING_URL,
)
from .planner import Coords, Route, estimate_route


def _leg(session: Any, frm: Coords, to: Coords, at_min: int) -> tuple[float, float]:
    """(km, minutes) for one leg. Blocking; raises on any failure."""
    params = {
        "from": f"x:{frm[1]} y:{frm[0]}",
        "to": f"x:{to[1]} y:{to[0]}",
        "at": at_min,
        "returnJSON": "true",
        "returnGeometries": "false",
        "returnInstructions": "true",
        "timeout": 60000,
        "nPaths": 1,
        "options": "AVOID_TRAILS:t,AVOID_TOLL_ROADS:f,AVOID_FERRIES:f",
        "subscription": "*",
    }
    resp = session.get(
        WAZE_ROUTING_URL,
        params=params,
        headers={"referer": "https://www.waze.com/"},
        timeout=30,
    )
    if not 200 <= resp.status_code < 300:
        raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:120]}")
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Waze error: {data.get('error')}")
    if data.get("alternatives"):
        route = data["alternatives"][0]["response"]
    else:
        route = data["response"]
        if isinstance(route, list):
            route = route[0]
    segments = route.get("results") or route.get("result") or []
    seconds = sum(s.get("crossTime", s.get("cross_time", 0)) for s in segments)
    meters = sum(s.get("length", 0) for s in segments)
    if meters <= 0:
        raise RuntimeError("Waze returned an empty route")
    return meters / 1000.0, seconds / 60.0


def waze_legs(
    home: Coords, dest: Coords, out_at: int, back_at: int | None
) -> tuple[tuple[float, float], tuple[float, float] | None]:
    """Outbound and (unless back_at is None) return leg. Blocking."""
    from curl_cffi import requests as cffi_requests

    session = cffi_requests.Session(impersonate="chrome")
    try:
        out = _leg(session, home, dest, out_at)
        back = _leg(session, dest, home, back_at) if back_at is not None else None
    finally:
        session.close()
    return out, back


def _minutes_ahead(when: dt.datetime, now: dt.datetime) -> int:
    """Waze's `at`: minutes from now, 0 (current traffic) when in the past
    or beyond the prediction horizon."""
    minutes = (when - now).total_seconds() / 60
    if minutes <= 0 or minutes > ROUTE_PREDICT_MAX_HOURS * 60:
        return 0
    return int(minutes)


class Router:
    """Waze with a cache (a dict owned by the caller's storage)."""

    def __init__(self, hass: HomeAssistant, cache: dict[str, Any]):
        self._hass = hass
        self.cache = cache
        self.dirty = False

    def prune(self, now: float | None = None) -> None:
        now = time.time() if now is None else now
        stale = [
            k
            for k, v in self.cache.items()
            if not isinstance(v, dict)
            or now - float(v.get("ts", 0)) > ROUTE_CACHE_HOURS * 3600 * 4
        ]
        for k in stale:
            del self.cache[k]
        self.dirty = self.dirty or bool(stale)

    async def drive_now(self, home: Coords, dest: Coords) -> float | None:
        """One way drive in minutes on current traffic, None if Waze fails.
        For the reachability check: leaving now."""
        try:
            (_, minutes), _ = await self._hass.async_add_executor_job(
                waze_legs, home, dest, 0, None
            )
        except Exception as err:  # noqa: BLE001 - any failure means "unknown"
            LOGGER.warning("Waze failed for the reachability check: %s", err)
            return None
        return minutes

    async def route(
        self,
        home: Coords,
        dest: Coords,
        start: dt.datetime,
        departure: bool,
        one_way: bool,
        now: dt.datetime,
    ) -> Route | None:
        """Both legs for a trip starting at `start`, on predicted traffic at
        the departure (the start for a departure, start minus the drive for
        an arrival, estimated from the straight line first). Cached per trip
        hour, always recalculated inside NEAR_TRIP_HOURS. None when Waze
        fails (the caller estimates)."""
        hours_away = (start - now).total_seconds() / 3600
        key = (
            f"{home[0]:.5f},{home[1]:.5f}->{dest[0]:.5f},{dest[1]:.5f}"
            f"|{'one' if one_way else 'round'}|{'dep' if departure else 'arr'}"
            f"|{start.strftime('%Y%m%d%H')}"
        )
        entry = self.cache.get(key)
        ts = time.time()
        if (
            isinstance(entry, dict)
            and hours_away > NEAR_TRIP_HOURS
            and ts - entry.get("ts", 0) < ROUTE_CACHE_HOURS * 3600
        ):
            return Route(km=entry["km"], out_min=entry["out_min"])

        if departure:
            leave = start
        else:
            guess = estimate_route(home, dest, True).out_min
            leave = start - dt.timedelta(minutes=guess)
        # The return leg: an hour at the destination is as good a guess as
        # any; it only shifts which traffic the way back is costed on.
        back_leave = (leave if departure else start) + dt.timedelta(hours=1)
        try:
            (out_km, out_min), back = await self._hass.async_add_executor_job(
                waze_legs,
                home,
                dest,
                _minutes_ahead(leave, now),
                None if one_way else _minutes_ahead(back_leave, now),
            )
        except Exception as err:  # noqa: BLE001 - any failure: estimate instead
            LOGGER.warning("Waze route calc failed: %s", str(err)[:200])
            return None
        km = out_km + (back[0] if back else 0)
        self.cache[key] = {"km": km, "out_min": out_min, "ts": ts}
        self.dirty = True
        return Route(km=km, out_min=out_min)
