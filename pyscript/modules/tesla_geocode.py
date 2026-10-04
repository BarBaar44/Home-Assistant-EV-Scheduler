"""
tesla_geocode — shared pyscript module
-------------------------------------------
Nominatim geocoding, with a persistent cache, shared by tesla_calendar.py
and tesla_trip_energy.py. Both call the same geocode_cached() against the
same cache file, so a location resolved by either app is immediately
available to the other — no duplicate Nominatim calls for the same
address, regardless of which app resolves it first.

Must live at /config/pyscript/modules/tesla_geocode.py. Import from an
app with:
    import tesla_geocode as geocode_mod

WHY THIS EXISTS AS A SEPARATE MODULE, NOT DUPLICATED IN EACH APP.
tesla_trip_energy.py only geocodes located events within
trip_cluster_hours of its nearest upcoming trip — that's the right
scope for CHARGE PLANNING (a trip three weeks out doesn't need a route
calculated today). But tesla_calendar.py's accept-RSVP gating wants
every located, not-yet-accepted event geocoded as soon as possible,
regardless of how far out it is — an invite shouldn't sit un-accepted
for weeks just because it's early. Rather than teach tesla_calendar.py
its own separate geocoding implementation (which would risk the two
apps' candidate-selection logic drifting apart over time), both call
into this one module and share one cache. The one-time Nominatim cost
of geocoding a far-future event slightly earlier than trip_energy
otherwise would have is negligible — it happens once per genuinely new
location, not on a recurring basis.

THREE ENTRY POINTS, THREE DIFFERENT JOBS:
  geocode_cached()    — unattended, cached, one answer. Used by both apps
                        against inbound invites.
  geocode()           — unattended, uncached, one answer. Picks the
                        candidate closest to home itself.
  search_candidates() — interactive, uncached, MANY answers. Used by the
                        dashboard destination picker, where a human is
                        waiting and can choose between them.

CACHE FORMAT (tesla_geocode_cache.json): location string (lowercased,
stripped) -> {"lat": ..., "lon": ..., "ts": ...} on success, or
{"failed": True, "ts": ...} on failure. A failure is cached for
FAILED_TTL_SECONDS so a persistently-bad address isn't retried on every
single poll by every app that touches it — but it does eventually retry
(a typo gets fixed, or Nominatim itself was just temporarily down).

CACHE PATH is passed in by the caller, not hardcoded here (same
convention as the other shared modules — this file has no app_config of
its own). It MUST be the same path in both callers' configs:
tesla_calendar.py's GEOCODE_CACHE_PATH and tesla_trip_energy.py's
GEOCODE_CACHE_PATH.
"""
import math
import logging
import requests

_logger = logging.getLogger(__name__)

FAILED_TTL_SECONDS = 3600


def geocode_cached(location_text, home_coords, cache, now_ts, nominatim_user_agent):
    """Look up `location_text` in `cache`; on a miss (or an expired
    failure entry) call Nominatim via geocode() and update `cache` in
    place. Returns ((lat, lon) or None, cache_hit: bool) — cache_hit is
    True whenever geocode() was NOT called this time (a real hit, or a
    still-fresh cached failure).

    Callers own saving `cache` back to disk — this function only mutates
    the in-memory dict, so a caller processing several locations in one
    pass can batch everything into a single save at the end.
    """
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
    """Geocode a free-text location via Nominatim.

    Ambiguous place names (e.g. a company with multiple real-world
    locations) can return a technically-valid match on the wrong
    continent. Rather than trusting Nominatim's top result blindly,
    request several candidates and pick whichever is closest to home —
    for a car charging automation, "nearest plausible match" is a far
    safer default than "first result".

    Returns (lat, lon) or None.
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

        # Distance computed inline rather than via a separate helper
        # function. pyscript's interpreter still governs code inside a
        # @pyscript_executor thread — it just isn't on the event loop —
        # and a call from inside one to a sibling plain function returns
        # an unrun coroutine instead of a result (see
        # tesla_outbound_email.py's module docstring for the full
        # explanation and the crash this exact shape caused there).
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
    """Return up to `limit` Nominatim matches for `query`, nearest to home
    first, as [{"display": str, "lat": float, "lon": float,
    "km": float or None, "house_number": str or None, "road": str or
    None, "city": str or None}, ...], in NOMINATIM'S OWN RELEVANCE ORDER.

    The structured fields matter as much as the coordinates. display_name
    is an address chain of varying length, so there is no fixed position
    that means "the town" — slicing off the first few components drops the
    city on any address that has a neighbourhood, which is most of them.

    The interactive counterpart to geocode(). That function collapses the
    candidate list to one itself because it runs unattended against an
    inbound invite and has nobody to ask; here a person is waiting, so the
    candidates come back intact. The case geocode() genuinely cannot
    handle is two branches of the same chain at a similar distance, where
    "closest to home" is a coin flip dressed up as a decision.

    THE RETURN VALUE IS THREE-WAY and callers must not collapse the first
    two: None means the lookup itself failed (network, timeout, Nominatim
    down) and is worth retrying unchanged; [] means the lookup succeeded
    and the place does not exist as typed, where retrying changes nothing
    and the user has to edit the query.

    RESULTS ARE NOT REORDERED BY DISTANCE, though the distance is
    returned for display. An earlier version sorted nearest-to-home, by
    analogy with geocode(). That analogy is wrong: geocode() sorts because
    an invite's LOCATION is whatever a third party typed and carries no
    hint about which of several matches is meant, whereas someone using
    this picker has just typed the town themselves. Reordering discards
    that — the strongest textual match gets pushed below a weaker one that
    happens to be nearer, and "Kerkstraat 15 haarlem" surfaces a same-named
    street in another town at the top. Nominatim already ranks on how well
    each result matches the query, which is the better signal when the
    query is deliberate.

    `house_number` is returned so the caller can tell a full address match
    from a street-level one. Nominatim silently drops an unmatched house
    number rather than failing, so "Kerkstraat 15" can come back as number
    25 on that street with nothing in the response marking the
    substitution — and a house number is exactly the part a user assumes
    was honoured.

    Deliberately does NOT touch the cache. A cache entry is keyed on a
    location string and holds exactly one coordinate pair — the right
    shape for the unattended path, the wrong shape for a list of
    alternatives, and writing the user's eventual choice under the raw
    search text would then answer a later invite for that text with a
    guess made in a different context. The chosen result is pinned onto
    the event as GEO=lat,lon by the caller instead, which is both
    per-event and immune to cache expiry.
    """
    try:
        resp = requests.get(
            "https://nominatim.openstreetmap.org/search",
            params={
                "q": query,
                "format": "json",
                # Over-fetch: near-duplicates get collapsed below, so
                # asking for exactly `limit` would leave fewer than that on
                # screen whenever Nominatim returns both a node and its
                # enclosing way for the same building.
                "limit": max(int(limit) * 2, 10),
                # Needed to read back the house number actually matched.
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
        # Rounding to ~11 m collapses a node and its enclosing way into
        # one entry without merging two genuinely different addresses.
        fingerprint = (round(lat, 4), round(lon, 4))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)

        # Same inline-haversine constraint as geocode() above: no calls to
        # sibling pyscript-defined functions from inside an executor
        # thread. home_coords may be None (zone.home unavailable), in
        # which case distance is simply unknown and Nominatim's own
        # relevance order stands.
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
        # Nominatim uses whichever of these fits the settlement's size, so
        # all four have to be tried before falling back to the county.
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
