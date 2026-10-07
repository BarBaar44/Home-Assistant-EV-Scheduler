"""
trip_scheduler (pyscript app)
-----------------------------
The trip form behind ev-trip-card: search a destination, schedule, move
and cancel a trip. Trips are events on an Invite Calendar entity
(`calendar_entity` below), created with invite_calendar.create_event, so
the integration owns the store, the invitation mail to the household
member, the SEQUENCE bumps and the threading. This app only validates,
geocodes, and turns the outcome into status messages.

It implements the EV trip planner contract, version 1 (CONTRACT.md in
https://github.com/BarBaar44/ev-trip-card):

    services  pyscript.ev_trip_search, ev_trip_clear_search,
              ev_trip_schedule, ev_trip_move, ev_trip_cancel,
              ev_trip_set_status, ev_trip_clear_status
    sensors   sensor.ev_trip_planner_trips, _search, _status
              (_plan comes from ev_trip_energy)

Trips are addressed by uid. The old UI dashboard (helpers, scripts,
sensor.tesla_trip_form_status, schedule_manual_trip and friends) was
removed on 7 Oct 2026.

Config (apps: trip_scheduler: in /config/pyscript/config.yaml):
    calendar_entity: calendar.tesla
    nominatim_user_agent: "..."
    # --- optional, defaults shown ---
    geocode_cache: /config/pyscript/tesla_geocode_cache.json
    household_map: /config/pyscript/tesla_household.json
    manual_trip_duration_min: 60     # DTEND offset for new trips
    trip_list_days: 180              # how far ahead the trip list looks

The calendar's own mailbox is the ORGANIZER of every trip and the member
an ATTENDEE (the role flip that keeps From: DMARC aligned); the
integration does that by itself for create_event. Trips made before the
cutover were organized by the same address, so they count as `own` and
can be moved and cancelled too.

Event markers in DESCRIPTION, read by ev_trip_energy:
    TRIP_TYPE=ONE_WAY | ROUND_TRIP
    TIME_IS=DEPARTURE    DTSTART is when you leave
    TIME_IS=ARRIVAL      DTSTART is when you must be there (arrive by)
    GEO=lat,lon          the destination the user picked in the search

The household map (HA user id -> {email, notify_service}) is a separate
JSON file, shared with ev_trip_energy, so it can be edited without a
config reload. A user's id is in the URL under Settings > People > Users.

REACHABILITY. An "arrive by" trip (new, or moved) that can't be reached
from home in time is refused: there is nothing useful to charge for. The
drive time comes from Waze, but only when it matters: a destination that
can't be reached even at 120 km/h straight line is refused without Waze,
one reachable even at 30 km/h straight line is accepted without Waze. If
Waze fails in between, the trip is accepted (ev_trip_energy copes).

This file must live at /config/pyscript/apps/trip_scheduler.py.
Modules used: json_store, atomic_io, geocode, routing.
"""
import re
import time
import datetime
import homeassistant.util.dt as dt_util
import json_store
import geocode as geocode_mod
import routing as routing_mod

APP = pyscript.app_config
CALENDAR_ENTITY = APP.get("calendar_entity", "calendar.tesla")
NOMINATIM_UA = APP.get("nominatim_user_agent")
GEOCODE_CACHE_PATH = APP.get("geocode_cache", "/config/pyscript/tesla_geocode_cache.json")
HOUSEHOLD_MAP_PATH = APP.get("household_map", "/config/pyscript/tesla_household.json")
MANUAL_TRIP_DURATION_MIN = int(APP.get("manual_trip_duration_min", 60))
TRIP_LIST_DAYS = int(APP.get("trip_list_days", 180))

if not NOMINATIM_UA:
    log.warning("trip_scheduler: no nominatim_user_agent configured, address search is off")

IC_DOMAIN = "invite_calendar"
IC_UPDATED_EVENT = "invite_calendar_updated"

MAX_DESTINATION_RESULTS = 6

# Contract version 1 (ev-trip-card CONTRACT.md).
CONTRACT_VERSION = 1
TRIPS_SENSOR = "sensor.ev_trip_planner_trips"
SEARCH_SENSOR = "sensor.ev_trip_planner_search"
STATUS_SENSOR = "sensor.ev_trip_planner_status"
STATUS_LEVELS = ("ok", "info", "success", "warning", "error")

# `source` values this app owns. _clear_status() only clears its own
# messages, so a housekeeping pass never wipes the card's own message.
STATUS_SOURCES = ("schedule", "cancel", "reschedule", "email", "selection", "search")

# uid -> {"name", "place", "arrival", "geo"} for upcoming trips: banner
# texts and the reachability check on a move. Rebuilt with the trip list.
_trips = {}

# Straight line speed bounds for the reachability check, km/h.
REACH_FAST_KMH = 120
REACH_SLOW_KMH = 30

_GEO_PIN_RE = re.compile(r"GEO=\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


# --------------------------------------------------------------------
# Invite Calendar calls
# --------------------------------------------------------------------

def _ic_call(action, **data):
    """(response, None) or (None, error text). The integration raises
    HomeAssistantError / ServiceValidationError with a translated
    message; that text goes on the banner as is."""
    try:
        resp = service.call(
            IC_DOMAIN, action, entity_id=CALENDAR_ENTITY,
            return_response=True, **data,
        )
    except Exception as e:
        text = str(e).strip() or e.__class__.__name__
        log.warning(f"trip_scheduler: {IC_DOMAIN}.{action} failed: {text}")
        return None, text
    # Entity services answer per entity: {"calendar.tesla": {...}}.
    if isinstance(resp, dict) and CALENDAR_ENTITY in resp:
        resp = resp[CALENDAR_ENTITY]
    return (resp if isinstance(resp, dict) else {}), None


def _list_events(days):
    """Occurrences from now for `days`, or None when the call failed."""
    resp, err = _ic_call(
        "list_events",
        start=dt_util.now().isoformat(),
        duration={"days": days},
    )
    if err is not None:
        return None
    return resp.get("events") or []


# --------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------

def _parse_local(value):
    """ISO datetime string as LOCAL wall time. dt_util.as_local() assumes
    a naive datetime is UTC, which shifted every trip by the UTC offset."""
    parsed = dt_util.parse_datetime(str(value))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_local(parsed)


def _home_coords():
    home = state.getattr("zone.home")
    if home and "latitude" in home:
        return (home["latitude"], home["longitude"])
    return None


def _pin_coords(description):
    """(lat, lon) from a GEO= pin, or None."""
    match = _GEO_PIN_RE.search(str(description or ""))
    if not match:
        return None
    lat = float(match.group(1))
    lon = float(match.group(2))
    if -90 <= lat <= 90 and -180 <= lon <= 180:
        return (lat, lon)
    return None


def _unreachable(dest, arrive_at, place):
    """Refusal text when `dest` can't be reached from home by `arrive_at`
    leaving now, else None. See REACHABILITY in the module docstring."""
    home = _home_coords()
    if home is None or dest is None:
        return None
    now = dt_util.now()
    minutes_left = (arrive_at - now).total_seconds() / 60
    km = routing_mod.straight_km(home, dest)
    fastest = km / REACH_FAST_KMH * 60
    if fastest > minutes_left:
        drive_text = f"at least {fastest:.0f} min"
    elif km / REACH_SLOW_KMH * 60 <= minutes_left:
        return None
    else:
        start = f"{home[0]},{home[1]}"
        end = f"{dest[0]},{dest[1]}"
        _, out_min, _, _ = routing_mod.waze_legs(start, end, False)
        if out_min is None:
            log.warning(f"Waze failed, can't check that {place} is reachable by {arrive_at}; accepting")
            return None
        if out_min <= minutes_left:
            return None
        drive_text = f"about {out_min:.0f} min"
    return (
        f"You can't be in {place} by {arrive_at.strftime('%H:%M')}: the drive "
        f"takes {drive_text} and it's {now.strftime('%H:%M')} now. Pick a "
        f"later time, or choose Leave at."
    )


# --------------------------------------------------------------------
# Status (the card's banner)
# --------------------------------------------------------------------

_STATUS_ICONS = {
    "ok": "mdi:check-circle-outline",
    "info": "mdi:progress-clock",
    "success": "mdi:check-circle-outline",
    "warning": "mdi:alert-outline",
    "error": "mdi:alert-circle-outline",
}


def _set_status(level, message, source):
    """`level` is also an attribute: state.get() raises NameError on a
    missing entity, state.getattr() returns None, so internal logic reads
    attributes."""
    state.set(STATUS_SENSOR, level, {
        "version": CONTRACT_VERSION,
        "level": level,
        "message": message,
        "source": source,
        "updated": dt_util.now().isoformat(timespec="seconds"),
        "friendly_name": "EV trip planner status",
        "icon": _STATUS_ICONS.get(level, "mdi:alert-circle-outline"),
    })


def _clear_status(force=False):
    """Back to "ok". Only clears this app's own messages, unless force."""
    current = state.getattr(STATUS_SENSOR) or {}
    level = current.get("level")
    if level == "ok":
        return
    if not force and level is not None and current.get("source") not in STATUS_SOURCES:
        return
    _set_status("ok", "", "")


@service
def ev_trip_clear_status():
    """Contract v1 `clear_status`."""
    _clear_status(force=True)


@service
def ev_trip_set_status(level, message):
    """Contract v1 `set_status`: the card's own messages, source form."""
    level = str(level)
    if level not in STATUS_LEVELS:
        level = "warning"
    _set_status(level, str(message), "form")


def _reject(reason, source="schedule"):
    """Refuse a request AND say why, in red."""
    log.warning(f"trip_scheduler refused: {reason}")
    _set_status("error", reason, source)


def _invite_pending(to_addr, done_text, source):
    """The change is saved; the integration could not send the mail and
    retries it on its next mailbox poll."""
    message = (
        f"{done_text}. The email to {to_addr} couldn't be sent yet; it goes "
        f"out automatically on the next mailbox check."
    )
    log.warning(f"Invite to {to_addr} pending ({source})")
    _set_status("warning", message, "email")


# --------------------------------------------------------------------
# Destination search
# --------------------------------------------------------------------

def _place_name(candidate):
    """"street number, town", built from Nominatim's structured fields,
    never by slicing display_name (that hid the town)."""
    road = candidate.get("road")
    city = candidate.get("city")
    number = candidate.get("house_number")
    if road and city:
        street = f"{road} {number}" if number else road
        return f"{street}, {city}"
    if city:
        return city
    return _short_name(candidate.get("display", ""))


def _short_name(display):
    """Leading part of a Nominatim display_name. Fallback only."""
    parts = [p.strip() for p in str(display).split(",") if p.strip()]
    return ", ".join(parts[:3]) if parts else str(display).strip()


def _query_house_number(query):
    """The bare number token the user typed, if any. Only ever used for a
    warning, never to reject a candidate."""
    for token in str(query).replace(",", " ").split():
        if token.isdigit():
            return token
    return None


def _number_warning(candidate, wanted_number):
    """"number 12, not 14" when Nominatim substituted a different house
    number, else None."""
    if not wanted_number:
        return None
    got = candidate.get("house_number")
    if str(got or "") == str(wanted_number):
        return None
    return f"number {got or 'n/a'}, not {wanted_number}"


def _publish_search(status, query="", results=None):
    """status: idle | results | empty | failed."""
    state.set(SEARCH_SENSOR, status, {
        "version": CONTRACT_VERSION,
        "query": query,
        "results": results or [],
        "friendly_name": "EV trip planner search",
        "icon": "mdi:map-search-outline",
    })


@service
def ev_trip_search(query):
    """Contract v1 `search`. A person is waiting, so alternatives go on
    screen instead of a nearest to home guess. Nominatim's relevance
    order is kept: the person typed the town themselves."""
    query = str(query or "").strip()
    if len(query) < 3:
        _set_status(
            "warning",
            "Type at least three characters of an address or place name, then search.",
            "search",
        )
        return
    if not NOMINATIM_UA:
        _set_status("error", "Address search isn't configured (nominatim_user_agent).", "search")
        return

    home_coords = _home_coords()
    if home_coords is None:
        log.warning("zone.home unavailable, search results won't show distances")

    candidates = geocode_mod.search_candidates(
        query, home_coords, NOMINATIM_UA, MAX_DESTINATION_RESULTS
    )

    if candidates is None:
        _publish_search("failed", query)
        _set_status(
            "error",
            "The address lookup service didn't answer. Try again in a "
            "moment; nothing has been scheduled.",
            "search",
        )
        return

    if not candidates:
        _publish_search("empty", query)
        _set_status(
            "warning",
            f"No places found for \"{query}\". Try a street and town, or a "
            f"business name with the town after it.",
            "search",
        )
        return

    wanted_number = _query_house_number(query)
    results = []
    seen = set()
    for c in candidates:
        place = _place_name(c)
        km = c.get("km")
        warning = _number_warning(c, wanted_number)
        # Identical lines are identical choices to the person picking: OSM
        # stores one street as several segments. Keep the first, which is
        # Nominatim's best match.
        key = (place, round(km) if km is not None else None, warning)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            "place": place,
            "location": c["display"],
            "geo": f"{c['lat']:.6f},{c['lon']:.6f}",
            "km": round(km, 1) if km is not None else None,
            "warning": warning,
        })

    _publish_search("results", query, results)
    if len(results) == 1:
        message = "Found one match. Check it, then schedule."
    else:
        message = f"Found {len(results)} matches, best first. Pick the right one before scheduling."
    _set_status("success", message, "search")
    log.info(f"Destination search '{query}' -> {len(results)} results")


@service
def ev_trip_clear_search():
    """Contract v1 `clear_search`."""
    _publish_search("idle")
    _clear_status(force=True)


# --------------------------------------------------------------------
# Schedule
# --------------------------------------------------------------------

@service
def ev_trip_schedule(start, place, location, geo, user_id, one_way=False, arrive_by=False):
    """Contract v1 `schedule`. Create a trip; the integration emails the
    household member the invitation. Checks run cheapest first; every
    refusal says why.

    start:     naive local wall time in HA's time zone, YYYY-MM-DDTHH:MM:SS.
               The departure, or with arrive_by the time to be there.
    place:     short name for SUMMARY, subject and banner.
    location:  full address for LOCATION.
    geo:       "lat,lon" picked in the search, pinned on the event. If
               unusable, `location` is geocoded instead.
    user_id:   the HA user booking it, mapped via the household map.
    """
    one_way = bool(one_way)
    arrive_by = bool(arrive_by)
    parsed = _parse_local(start)
    if parsed is None:
        _reject(f"Could not read the date/time '{start}'.")
        return

    if parsed <= dt_util.now():
        _reject(
            f"The trip time {parsed.strftime('%Y-%m-%d %H:%M')} is in the past. "
            f"A past trip would be invisible to the scheduler and impossible "
            f"to cancel."
        )
        return

    if not location or not str(location).strip():
        _reject(
            "Pick a destination before scheduling. The charging automation "
            "needs one to work out how much charge the trip needs."
        )
        return
    location = str(location).strip()
    place = str(place or "").strip() or location

    # Validate rather than trust: this is a service, callable with anything.
    dest = _pin_coords(f"GEO={geo}") if geo else None
    if geo and dest is None:
        log.warning(f"trip_scheduler: ignoring unusable geo '{geo}', looking up '{location}'")

    household_map = json_store.load_json_map(HOUSEHOLD_MAP_PATH, warn=log.warning)
    member_email = (household_map.get(user_id) or {}).get("email")
    if not member_email:
        _reject(
            f"User id '{user_id}' isn't in {HOUSEHOLD_MAP_PATH}, so "
            f"there's nobody to send the confirmation to. Add them to the "
            f"household map and try again."
        )
        return

    # Geocode only without a usable pin (costs a Nominatim call).
    # zone.home missing is not a reason to refuse.
    if dest is None:
        home_coords = _home_coords()
        if home_coords is None:
            log.warning("zone.home unavailable, scheduling without a geocode check")
        elif not NOMINATIM_UA:
            log.warning("No nominatim_user_agent configured, scheduling without a geocode check")
        else:
            geocode_cache = json_store.load_json_map(GEOCODE_CACHE_PATH, warn=log.warning)
            coords, cache_hit = geocode_mod.geocode_cached(
                location, home_coords, geocode_cache, time.time(), NOMINATIM_UA
            )
            if coords is None:
                # Drop the cached miss: right for the poll loop, wrong for
                # someone who fixes a typo and resubmits seconds later.
                geocode_cache.pop(location.strip().lower(), None)
                json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)
                _reject(
                    f"Couldn't find \"{location}\" on the map, so the trip wasn't "
                    f"scheduled. Search again and pick a result."
                )
                return
            if not cache_hit:
                json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)
            dest = coords

    # Last, and only for "arrive by": the one check that may ask Waze.
    if arrive_by:
        refusal = _unreachable(dest, parsed, place)
        if refusal:
            _reject(refusal)
            return

    end = parsed + datetime.timedelta(minutes=MANUAL_TRIP_DURATION_MIN)
    description_lines = [
        f"TRIP_TYPE={'ONE_WAY' if one_way else 'ROUND_TRIP'}",
        f"TIME_IS={'ARRIVAL' if arrive_by else 'DEPARTURE'}",
    ]
    if dest is not None and geo:
        description_lines.append(f"GEO={dest[0]:.6f},{dest[1]:.6f}")

    _set_status("info", "Scheduling the trip…", "schedule")
    resp, err = _ic_call(
        "create_event",
        summary=f"Trip to {place}",
        start_date_time=parsed.isoformat(),
        end_date_time=end.isoformat(),
        location=location,
        description="\n".join(description_lines),
        attendees=[member_email],
    )
    if err is not None:
        _reject(f"The trip wasn't scheduled: {err}")
        return

    uid = resp.get("uid", "?")
    _publish_search("idle")
    refresh_trips()

    trip_text = (
        f"Trip to {place}, "
        f"{'arriving by' if arrive_by else 'leaving'} {parsed.strftime('%a %d %b, %H:%M')}"
        f"{' (one-way)' if one_way else ''}"
    )
    if resp.get("pending"):
        _invite_pending(member_email, f"{trip_text}, scheduled", "schedule")
    else:
        _set_status("success", f"{trip_text}, scheduled.", "schedule")

    log.info(
        f"Scheduled trip {uid} to '{location}' at {parsed} "
        f"({'arrival' if arrive_by else 'departure'}, "
        f"{'one-way' if one_way else 'round trip'}), for {member_email}"
        f"{', invite pending' if resp.get('pending') else ''}"
    )


# --------------------------------------------------------------------
# Trip list, move, cancel
# --------------------------------------------------------------------

def _event_start(ev):
    """Tz aware local start of a list_events entry, None for all day."""
    if ev.get("all_day"):
        return None
    parsed = dt_util.parse_datetime(str(ev.get("start") or ""))
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_local(parsed)


def _summary_place(ev):
    """The place name a person sees: summary minus "Trip to ", else a
    short form of the location."""
    summary = str(ev.get("summary") or "").strip()
    if summary.lower().startswith("trip to "):
        return summary[8:].strip()
    if summary:
        return summary
    return _short_name(str(ev.get("location") or ""))


def _publish_trips(events):
    """Upcoming trips this calendar organizes, soonest first, on the trips
    sensor (written only on a change) and in _trips.

    recurrence_id is NOT a recurring marker: up to Invite Calendar 1.2.1
    every occurrence carried one, single events too. The form only makes
    single trips; a series made by hand would be moved or cancelled as a
    whole."""
    now = dt_util.now()
    rows = []
    for ev in events:
        if not ev.get("own"):
            continue
        if str(ev.get("status") or "").upper() == "CANCELLED":
            continue
        uid = ev.get("uid")
        start = _event_start(ev)
        if not uid or start is None or start < now:
            continue
        rows.append((start, uid, ev))
    rows.sort(key=lambda r: r[0])

    trips = []
    info = {}
    for start, uid, ev in rows:
        place = _summary_place(ev)
        description = str(ev.get("description") or "")
        arrival = "TIME_IS=ARRIVAL" in description
        trips.append({
            "uid": uid,
            "start": start.isoformat(timespec="seconds"),
            "place": place,
            "location": str(ev.get("location") or ""),
            "one_way": "TRIP_TYPE=ONE_WAY" in description,
            "time_is": "arrival" if arrival else "departure",
        })
        info[uid] = {
            "name": f"{place} on {start.strftime('%a %d %b, %H:%M')}",
            "place": place,
            "arrival": arrival,
            "geo": _pin_coords(description),
        }

    # In place, no `global`.
    _trips.clear()
    _trips.update(info)

    if (state.getattr(TRIPS_SENSOR) or {}).get("trips") == trips:
        return
    state.set(TRIPS_SENSOR, str(len(trips)), {
        "version": CONTRACT_VERSION,
        "trips": trips,
        "friendly_name": "EV trip planner trips",
        "icon": "mdi:car-clock",
    })


def _uid_arg(uid, verb):
    uid = str(uid or "").strip()
    if not uid:
        _set_status("error", f"No trip given to {verb}.", "selection")
        return None
    return uid


@service
def ev_trip_move(uid, start):
    """Contract v1 `move`. Only the start is sent: the integration keeps
    the duration, bumps SEQUENCE and mails the update."""
    uid = _uid_arg(uid, "move")
    if not uid:
        return
    trip = _trips.get(uid) or {}
    name = trip.get("name") or "the trip"

    parsed = _parse_local(start)
    if parsed is None:
        _reject(f"Could not read the new date/time '{start}'.", "reschedule")
        return
    if parsed <= dt_util.now():
        _reject(
            f"The new trip time {parsed.strftime('%Y-%m-%d %H:%M')} is in the "
            f"past. Nothing was changed.",
            "reschedule",
        )
        return
    if trip.get("arrival"):
        refusal = _unreachable(trip.get("geo"), parsed, trip.get("place") or name)
        if refusal:
            _reject(f"{refusal} Nothing was changed.", "reschedule")
            return

    _set_status("info", f"Moving \"{name}\"…", "reschedule")
    resp, err = _ic_call("update_event", uid=uid, start_date_time=parsed.isoformat())
    refresh_trips()
    if err is not None:
        _set_status("error", f"\"{name}\" was not moved: {err}", "reschedule")
        return

    moved_text = f"Moved to {parsed.strftime('%a %d %b, %H:%M')}"
    invited = resp.get("invited") or []
    if resp.get("pending"):
        _invite_pending(", ".join(invited) or "the member", moved_text, "reschedule")
    elif not invited:
        _set_status(
            "warning",
            f"{moved_text} here, but nobody is invited to this trip, so no "
            f"calendar was updated.",
            "reschedule",
        )
    else:
        _set_status("success", f"{moved_text}.", "reschedule")
    log.info(f"Moved trip {uid} to {parsed} (sequence {resp.get('sequence')})")


@service
def ev_trip_cancel(uid):
    """Contract v1 `cancel`. The integration sends the CANCEL BEFORE it
    changes the calendar: when the mail can't go out, nothing is
    cancelled and the call fails, so the member's calendar never keeps a
    trip that no longer exists here."""
    uid = _uid_arg(uid, "cancel")
    if not uid:
        return
    name = (_trips.get(uid) or {}).get("name") or "the trip"

    _set_status("info", f"Cancelling \"{name}\"…", "cancel")
    resp, err = _ic_call("cancel_event", uid=uid)
    refresh_trips()
    if err is not None:
        _set_status(
            "error",
            f"\"{name}\" was NOT cancelled: {err} The trip is still "
            f"planned and charged for; try again later.",
            "cancel",
        )
        return

    notified = resp.get("notified") or []
    log.info(f"Cancelled trip {uid} ('{name}'), notified {notified}")
    if notified:
        _set_status("success", f"Cancelled \"{name}\".", "cancel")
    else:
        _set_status(
            "warning",
            f"Cancelled \"{name}\" here, but it had nobody invited, so no "
            f"cancellation notice was sent.",
            "cancel",
        )


# --------------------------------------------------------------------
# Startup and housekeeping
# --------------------------------------------------------------------

@time_trigger("startup")
def init_trip_form():
    """state.set() entities don't survive a restart: recreate the status
    and the search. Startup only, so a cron never wipes an error before
    anyone read it."""
    _clear_status(force=True)
    _publish_search("idle")


@time_trigger("startup")
@time_trigger("cron(2-59/5 * * * *)")
@event_trigger(IC_UPDATED_EVENT, f"entity_id == '{CALENDAR_ENTITY}'")
@service
def refresh_trips(**kwargs):
    """Keep the trip list in sync: after any calendar change, and as trips
    age past their start. A failed read keeps the list as it is."""
    events = _list_events(TRIP_LIST_DAYS)
    if events is None:
        return
    _publish_trips(events)
