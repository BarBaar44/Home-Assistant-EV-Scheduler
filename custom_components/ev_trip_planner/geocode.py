"""Nominatim: a cached single answer for unattended use, and a candidate
list for the search where a person chooses.

Please respect Nominatim's usage policy: one request at a time, a real
contact in the User-Agent, and results cached (30 days here)."""

from __future__ import annotations

import asyncio
import time
from typing import Any

import aiohttp
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import (
    GEOCODE_CACHE_DAYS,
    GEOCODE_FAILED_TTL,
    MAX_SEARCH_RESULTS,
    NOMINATIM_URL,
)
from .planner import Coords, straight_km

_TIMEOUT = aiohttp.ClientTimeout(total=10)


class LookupFailed(Exception):
    """Nominatim did not answer (as opposed to: no such place)."""


class Geocoder:
    """Nominatim client with a persistent cache (a dict owned by the
    caller's storage, which saves it after every run)."""

    def __init__(self, hass: HomeAssistant, contact: str, cache: dict[str, Any]):
        self._hass = hass
        self._ua = f"ha-ev-trip-planner (contact: {contact})"
        self.cache = cache
        # Nominatim allows one request at a time.
        self._lock = asyncio.Lock()

    async def _get(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        session = async_get_clientsession(self._hass)
        async with self._lock:
            try:
                async with session.get(
                    NOMINATIM_URL,
                    params={"format": "json", **params},
                    headers={"User-Agent": self._ua},
                    timeout=_TIMEOUT,
                ) as resp:
                    resp.raise_for_status()
                    data = await resp.json(content_type=None)
            except (TimeoutError, aiohttp.ClientError, ValueError) as err:
                raise LookupFailed(str(err)) from err
        return data if isinstance(data, list) else []

    def prune(self, now: float | None = None) -> None:
        """Drop entries older than the cache lifetime."""
        now = time.time() if now is None else now
        stale = [
            k
            for k, v in self.cache.items()
            if not isinstance(v, dict)
            or now - float(v.get("ts", 0)) > GEOCODE_CACHE_DAYS * 86400
        ]
        for k in stale:
            del self.cache[k]

    def forget(self, text: str) -> None:
        """Drop a cached answer (a failed submit, so a fix retries at once)."""
        self.cache.pop(text.strip().lower(), None)

    async def geocode(self, text: str, home: Coords | None) -> Coords | None:
        """One answer, cached; the candidate nearest home (an ambiguous
        company name can otherwise land on another continent). No such
        place is cached for an hour, so a bad address isn't retried every
        run. Raises LookupFailed when Nominatim did not answer: that says
        nothing about the address, so it is neither cached nor reported
        as one."""
        key = text.strip().lower()
        entry = self.cache.get(key)
        now = time.time()
        if isinstance(entry, dict) and "lat" in entry:
            return (entry["lat"], entry["lon"])
        if (
            isinstance(entry, dict)
            and entry.get("failed")
            and now - entry.get("ts", 0) < GEOCODE_FAILED_TTL
        ):
            return None
        results = await self._get({"q": text, "limit": 5})
        points = []
        for r in results:
            try:
                points.append((float(r["lat"]), float(r["lon"])))
            except KeyError, TypeError, ValueError:
                continue
        if not points:
            self.cache[key] = {"failed": True, "ts": now}
            return None
        best = min(points, key=lambda p: straight_km(home, p)) if home else points[0]
        self.cache[key] = {"lat": best[0], "lon": best[1], "ts": now}
        return best

    async def search(
        self, query: str, home: Coords | None, limit: int = MAX_SEARCH_RESULTS
    ) -> list[dict[str, Any]]:
        """Candidates in Nominatim's relevance order, [] for no such place.
        Raises LookupFailed when the service did not answer. Never cached:
        the picked point travels with the trip as GEO=."""
        results = await self._get(
            {"q": query, "limit": max(limit * 2, 10), "addressdetails": 1}
        )
        out: list[dict[str, Any]] = []
        seen = set()
        for r in results:
            try:
                lat, lon = float(r["lat"]), float(r["lon"])
            except KeyError, TypeError, ValueError:
                continue
            display = str(r.get("display_name") or "").strip()
            if not display:
                continue
            # About 11 m: collapses a node and its way, keeps neighbours.
            fingerprint = (round(lat, 4), round(lon, 4))
            if fingerprint in seen:
                continue
            seen.add(fingerprint)
            address = r.get("address") or {}
            out.append(
                {
                    "display": display,
                    "lat": lat,
                    "lon": lon,
                    "km": straight_km(home, (lat, lon)) if home else None,
                    "house_number": address.get("house_number"),
                    "road": address.get("road"),
                    "city": address.get("city")
                    or address.get("town")
                    or address.get("village")
                    or address.get("municipality")
                    or address.get("county"),
                }
            )
        return out[:limit]
