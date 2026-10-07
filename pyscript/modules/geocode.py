"""
geocode (shared pyscript module, formerly tesla_geocode)
--------------------------------------------------------
Nominatim geocoding with a persistent cache, shared by trip_scheduler
(submit time check and the destination search) and ev_trip_energy
(routing). Both use geocode_cached() against the same cache file, so a
location resolved by one is a free cache hit for the other.

Lives at /config/pyscript/modules/geocode.py.
    import geocode as geocode_mod

THREE ENTRY POINTS, THREE DIFFERENT JOBS:
  geocode_cached()    unattended, cached, one answer.
  geocode()           unattended, uncached, one answer; picks the
                      candidate closest to home itself.
  search_candidates() interactive, uncached, MANY answers, for the
                      destination search where a human chooses.

CACHE FORMAT: location string (lowercased, stripped) -> {"lat", "lon",
"ts"} on success, or {"failed": True, "ts"} on failure. A failure is
cached for FAILED_TTL_SECONDS so a bad address isn't retried on every poll,
but it does eventually retry. The cache PATH is passed in by callers and
must be the same everywhere.
"""
import math
import logging
import requests

_logger = logging.getLogger(__name__)

FAILED_TTL_SECONDS = 3600


def geocode_cached(location_text, home_coords, cache, now_ts, nominatim_user_agent):
    """Look up `location_text` in `cache`; on a miss (or an expired failure
    entry) call Nominatim via geocode() and update `cache` in place.
    Returns ((lat, lon) or None, cache_hit). Callers own saving `cache`."""
    key = location_text.strip().lower()
    entry = cache.get(key)
    if isinstance(entry, dict) and "lat" in entry:
        return (entry["lat"], entry["lon"]), True
    if isinstance(entry, dict) and entry.get("failed"):
        if now_ts - entry.get("ts", 0) < FAILED_TTL_SECONDS:
            return None, True

    coords = geocode(location_text, home_coords, nominatim_user_agent)
    if coords is None:
        cache[key] = {"failed": True, "ts": now_ts}
        return None, False
    cache[key] = {"lat": coords[0], "lon": coords[1], "ts": now_ts}
    return coords, False


@pyscript_executor
def geocode(location_text, home_coords, nominatim_user_agent):
    """Geocode a free text location via Nominatim, picking whichever of
    several candidates is closest to home (an ambiguous company name can
    otherwise resolve to the wrong continent). Returns (lat, lon) or None.

    Distance is computed inline: an executor thread must not call sibling
    pyscript functions (they return unrun coroutines there).
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={"q": location_text, "format": "json", "limit": 5},
            headers={"User-Agent": nominatim_user_agent},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
        if not results:
            return None

        candidates = [(float(r["lat"]), float(r["lon"])) for r in results]

        home_lat, home_lon = home_coords
        home_lat_r = math.radians(home_lat)
        closest = None
        closest_km = None
        for lat, lon in candidates:
            dlat = math.radians(lat - home_lat)
            dlon = math.radians(lon - home_lon)
            a = (
                math.sin(dlat / 2) ** 2
                + math.cos(home_lat_r)
                * math.cos(math.radians(lat))
                * math.sin(dlon / 2) ** 2
            )
            km = 2 * 6371.0 * math.asin(math.sqrt(a))
            if closest_km is None or km < closest_km:
                closest_km = km
                closest = (lat, lon)

        if len(candidates) > 1:
            _logger.info(
                f"Geocoded '{location_text}' to {len(candidates)} candidates; "
                f"picked closest to home: {closest}"
            )
        return closest
    except Exception as e:
        _logger.warning(f"Nominatim geocode failed: {e}")
        return None


@pyscript_executor
def search_candidates(query, home_coords, nominatim_user_agent, limit=6):
    """Up to `limit` Nominatim matches for `query`, in NOMINATIM'S OWN
    RELEVANCE ORDER, as [{"display", "lat", "lon", "km", "house_number",
    "road", "city"}, ...].

    Three way result: None means the lookup failed (retry unchanged), []
    means the place does not exist as typed (edit the query).

    Not reordered by distance: someone searching has typed the town
    themselves, and Nominatim's text ranking is the better signal then.
    `house_number` is returned because Nominatim silently substitutes a
    nearby number when the one asked for doesn't exist.

    Does NOT touch the cache: the chosen result is pinned onto the event
    as GEO=lat,lon by the caller instead, which survives cache expiry.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "json",
                # Over fetch: a node and its enclosing way get collapsed below.
                "limit": max(int(limit) * 2, 10),
                "addressdetails": 1,
            },
            headers={"User-Agent": nominatim_user_agent},
            timeout=10,
        )
        resp.raise_for_status()
        results = resp.json()
    except Exception as e:
        _logger.warning(f"Nominatim search failed for '{query}': {e}")
        return None

    if not results:
        return []

    home_lat, home_lon = home_coords if home_coords else (None, None)
    home_lat_r = math.radians(home_lat) if home_lat is not None else None

    candidates = []
    seen = set()
    for r in results:
        try:
            lat = float(r["lat"])
            lon = float(r["lon"])
        except (KeyError, TypeError, ValueError):
            continue
        display = str(r.get("display_name") or "").strip()
        if not display:
            continue
        # About 11 m: collapses a node and its way, keeps real neighbours.
        fingerprint = (round(lat, 4), round(lon, 4))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        km = None
        if home_lat_r is not None:
            dlat = math.radians(lat - home_lat)
            dlon = math.radians(lon - home_lon)
            a = (
                math.sin(dlat / 2) ** 2
                + math.cos(home_lat_r)
                * math.cos(math.radians(lat))
                * math.sin(dlon / 2) ** 2
            )
            km = 2 * 6371.0 * math.asin(math.sqrt(a))

        address = r.get("address") or {}
        city = (
            address.get("city")
            or address.get("town")
            or address.get("village")
            or address.get("municipality")
            or address.get("county")
        )
        candidates.append({
            "display": display,
            "lat": lat,
            "lon": lon,
            "km": km,
            "house_number": address.get("house_number"),
            "road": address.get("road"),
            "city": city,
        })

    if len(candidates) > int(limit):
        _logger.info(
            f"Search for '{query}' returned {len(candidates)} distinct "
            f"places; offering the top {limit} by relevance"
        )
    return candidates[: int(limit)]
