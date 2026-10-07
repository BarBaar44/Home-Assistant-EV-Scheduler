"""
ev_trip_energy (pyscript app)
-----------------------------
Reads upcoming events from the trip calendar (an Invite Calendar entity),
geocodes any LOCATION, calculates distance and energy via Waze, and
publishes the SOC the car needs and the time by which it needs it (picked
up by the evcc plan publisher automation). Accepts inbound invites once
their location is found. EV specific, not Tesla specific.

Requires in /config/pyscript/requirements.txt: requests, pywaze,
curl_cffi (Waze blocks plain clients, see modules/routing.py).

Requires in /config/pyscript/modules/: atomic_io.py, json_store.py,
geocode.py, routing.py

Config (apps: ev_trip_energy: in /config/pyscript/config.yaml):
    calendar_entity: calendar.tesla
    nominatim_user_agent: "..."
    usable_battery_kwh: 79.0
    safety_buffer_pct: 10
    lookahead_days: 7
    default_notify_service: "notify.mobile_app_<device>"   # NEVER notify.notify
    # --- optional, defaults shown ---
    geocode_cache: /config/pyscript/tesla_geocode_cache.json
    household_map: /config/pyscript/tesla_household.json
    alerted_path: /config/pyscript/tesla_trip_energy_alerted.json
    route_cache_path: /config/pyscript/tesla_route_cache.json
    efficiency_sensor: "sensor.tesla_adjusted_efficiency_wh_km"
    fallback_efficiency_wh_km: 153
    prep_buffer_min: 15
    trip_cluster_hours: 12
    all_day_departure_hour: 8
    near_trip_hours: 6
    route_cache_hours: 6
    fallback_detour_factor: 1.3       # Waze down: road km = straight line x this
    fallback_speed_kmh: 70            # Waze down: assumed average speed
    idle_required_soc: 0
    floor_soc: 0
    floor_ready_hour: 7
    soc_sensor: "sensor.calimero_battery_level"
    accept_invites: true              # accept_event once a location geocodes

The calendar entry's accept policy must be Manual: this app decides when
an invite is accepted (after its location is found), the integration
sends the RSVP.

DESIGN NOTES:

* SOC FLOOR. The battery is kept between floor_soc and the car's limit
  while plugged in, published through the SAME two helpers as a trip, as
  ONE plan: evcc only works towards its next plan. See _choose_plan().
  floor_soc: 0 keeps the old behaviour (idle values, 2099 deadline).

* Departure or arrival. An inbound invite's start is when you have to
  BE somewhere, so the drive comes off it. A trip from the form is a
  departure (TIME_IS=DEPARTURE, or organized by this calendar, `own`),
  unless the form said "arrive by" (TIME_IS=ARRIVAL, which wins over
  `own`). See _is_departure().

* Trips are budgeted in CLUSTERS: every located event within
  trip_cluster_hours of the first is summed.

* Geocode and route results are CACHED (Nominatim's usage policy).
  Routes are always recalculated inside near_trip_hours.

* A GEO=lat,lon pin in DESCRIPTION is honoured; a bad pin falls back to
  the LOCATION text.

* Alerts go to the event's person: the invited member for an own trip,
  the organizer for an inbound invite, through the household map. The
  fallback notify service must NOT be a broadcast; notify.notify is
  refused at load.

* Alert dedup is keyed per occurrence (uid + start). Entries for removed
  events are dropped on invite_calendar_updated; occurrences more than a
  day in the past are dropped on every run.

* The helpers are RESET when there is no trip.

* The published plan is also on sensor.ev_trip_planner_plan (EV trip
  planner contract v1, see ev-trip-card CONTRACT.md) with the place and
  km, for the card. Written on every run that decides a plan, so it is
  back within 5 minutes of a restart.

This file must live at /config/pyscript/apps/ev_trip_energy.py.

Assumes these helpers exist in Home Assistant:
    input_number.next_trip_required_soc   (0-100, step 0.1)
    input_datetime.next_trip_deadline     (date AND time)
    input_text.next_trip_notify_service   (optional)
"""
import re
import math
import time
import datetime
import homeassistant.util.dt as dt_util
import json_store
import geocode as geocode_mod
import routing as routing_mod

APP = pyscript.app_config
CALENDAR_ENTITY = APP.get("calendar_entity", "calendar.tesla")
IC_DOMAIN = "invite_calendar"
IC_UPDATED_EVENT = "invite_calendar_updated"

NOMINATIM_UA = APP.get("nominatim_user_agent")
if not NOMINATIM_UA:
    log.error("ev_trip_energy: no nominatim_user_agent configured, geocoding will fail")
FALLBACK_USABLE_KWH = float(APP.get("usable_battery_kwh", 79.0))
SAFETY_BUFFER_PCT = float(APP.get("safety_buffer_pct", 10))
LOOKAHEAD_DAYS = int(APP.get("lookahead_days", 7))

EFFICIENCY_SENSOR = APP.get("efficiency_sensor", "sensor.tesla_adjusted_efficiency_wh_km")
FALLBACK_WH_KM = float(APP.get("fallback_efficiency_wh_km", 153))
PREP_BUFFER_MIN = float(APP.get("prep_buffer_min", 15))
TRIP_CLUSTER_HOURS = float(APP.get("trip_cluster_hours", 12))
ALL_DAY_DEPARTURE_HOUR = int(APP.get("all_day_departure_hour", 8))
NEAR_TRIP_HOURS = float(APP.get("near_trip_hours", 6))
ROUTE_CACHE_HOURS = float(APP.get("route_cache_hours", 6))
# Used only when Waze fails: road km = straight line km x this factor,
# drive time at this average speed. See _estimate_route().
FALLBACK_DETOUR_FACTOR = float(APP.get("fallback_detour_factor", 1.3))
FALLBACK_SPEED_KMH = float(APP.get("fallback_speed_kmh", 70))
IDLE_REQUIRED_SOC = float(APP.get("idle_required_soc", 0))
FLOOR_SOC = float(APP.get("floor_soc", 0))
FLOOR_READY_HOUR = int(APP.get("floor_ready_hour", 7))
SOC_SENSOR = APP.get("soc_sensor", "sensor.calimero_battery_level")
ACCEPT_INVITES = bool(APP.get("accept_invites", True))

HOUSEHOLD_MAP_PATH = APP.get("household_map", "/config/pyscript/tesla_household.json")

_SAFE_NOTIFY_SERVICE = "notify.persistent_notification"
DEFAULT_NOTIFY_SERVICE = str(APP.get("default_notify_service", _SAFE_NOTIFY_SERVICE)).strip()
if DEFAULT_NOTIFY_SERVICE in ("notify.notify", "notify", ""):
    log.error(
        f"default_notify_service is '{DEFAULT_NOTIFY_SERVICE}', which "
        f"BROADCASTS to every phone. Using {_SAFE_NOTIFY_SERVICE} instead. "
        f"Set it to one person's notify.mobile_app_<device> under apps: ev_trip_energy:"
    )
    DEFAULT_NOTIFY_SERVICE = _SAFE_NOTIFY_SERVICE

# Occurrence key -> last alerted failure type, so a persistently failing
# occurrence only notifies once.
ALERTED_PATH = APP.get("alerted_path", "/config/pyscript/tesla_trip_energy_alerted.json")
ALERT_KEEP_PAST_SECONDS = 86400

GEOCODE_CACHE_PATH = APP.get("geocode_cache", "/config/pyscript/tesla_geocode_cache.json")
ROUTE_CACHE_PATH = APP.get("route_cache_path", "/config/pyscript/tesla_route_cache.json")
GEOCODE_CACHE_DAYS = 30

TARGET_SOC_HELPER = "input_number.next_trip_required_soc"
TARGET_ETA_HELPER = "input_datetime.next_trip_deadline"
TARGET_NOTIFY_HELPER = "input_text.next_trip_notify_service"

# EV trip planner contract v1 (ev-trip-card CONTRACT.md).
CONTRACT_VERSION = 1
PLAN_SENSOR = "sensor.ev_trip_planner_plan"

FAR_FUTURE = "2099-01-01 00:00:00"


# --------------------------------------------------------------------
# Invite Calendar
# --------------------------------------------------------------------

def _ic_call(action, **data):
    """(response, None) or (None, error text)."""
    try:
        resp = service.call(
            IC_DOMAIN, action, entity_id=CALENDAR_ENTITY,
            return_response=True, **data,
        )
    except Exception as e:
        return None, (str(e).strip() or e.__class__.__name__)
    if isinstance(resp, dict) and CALENDAR_ENTITY in resp:
        resp = resp[CALENDAR_ENTITY]
    return (resp if isinstance(resp, dict) else {}), None


def _accept_if_due(ev, accepted_now):
    """RSVP ACCEPTED for an inbound invite whose location was found.
    Only managed (arrived by mail since the integration was set up), not
    yet accepted at this SEQUENCE, and not organized by this calendar.
    Once per uid per run: occurrences of a series share the RSVP."""
    if not ACCEPT_INVITES:
        return
    uid = ev.get("uid")
    if (
        not uid
        or uid in accepted_now
        or ev.get("own")
        or not ev.get("managed")
        or ev.get("accepted")
    ):
        return
    accepted_now.add(uid)
    resp, err = _ic_call("accept_event", uid=uid)
    if err is not None:
        log.warning(f"Could not accept '{ev.get('summary', '')}' ({uid}): {err}")
        return
    if resp.get("sent"):
        log.info(f"Accepted '{ev.get('summary', '')}' ({uid}), its location was found")


@event_trigger(IC_UPDATED_EVENT, f"entity_id == '{CALENDAR_ENTITY}'")
def forget_removed_events(removed=None, **kwargs):
    """Drop alert dedup entries of events that left the calendar (a
    cancel, by mail or from the form, or retention)."""
    if not removed:
        return
    json_store.pop_uids_from_files([ALERTED_PATH], list(removed), warn=log.warning)


def _prune_past_alerts(alerted_map, now_ts):
    """Occurrence keys whose start is more than a day ago. True if any
    were dropped (the caller saves)."""
    stale = []
    for key in alerted_map:
        _, sep, ts = str(key).partition(json_store.OCC_SEP)
        if not sep:
            continue
        try:
            if now_ts - int(ts) > ALERT_KEEP_PAST_SECONDS:
                stale.append(key)
        except ValueError:
            stale.append(key)
    for key in stale:
        alerted_map.pop(key, None)
    return bool(stale)


# --------------------------------------------------------------------
# Main run
# --------------------------------------------------------------------

@time_trigger("cron(*/5 * * * *)")
@event_trigger(IC_UPDATED_EVENT, f"entity_id == '{CALENDAR_ENTITY}'")
@service
def check_next_trip_energy(**kwargs):
    """Compute the SOC and deadline for the next cluster of located trips.

    Runs every 5 minutes (time passing changes deadlines and the floor),
    and immediately when the calendar changed: the integration fires
    invite_calendar_updated after a poll AND after create/update/cancel,
    with the new data already in place.

    task_unique: an event run can overlap a cron run; the newer one wins
    (it read the fresher calendar). JSON writes are atomic and the helpers
    are rewritten in full by the survivor, so a killed run leaves nothing
    half done.
    """
    task.unique("ev_trip_energy_check")
    log.info(f"Checking {CALENDAR_ENTITY} for upcoming located trips")

    home = state.getattr("zone.home")
    if not home or "latitude" not in home:
        log.warning("zone.home is unavailable, cannot calculate trip energy")
        return
    home_coords = (home["latitude"], home["longitude"])

    resp, err = _ic_call(
        "list_events",
        start=dt_util.now().isoformat(),
        duration={"days": LOOKAHEAD_DAYS},
    )
    if err is not None:
        log.warning(f"{IC_DOMAIN}.list_events failed: {err}")
        return
    upcoming = resp.get("events") or []

    located = []
    for ev in upcoming:
        if not ev.get("location"):
            continue
        if str(ev.get("status") or "").upper() == "CANCELLED":
            continue
        start = _event_start(ev)
        if start is None:
            log.warning(f"Skipping event with unparseable start: {ev.get('start')}")
            continue
        located.append((start, ev))
    located.sort(key=lambda item: item[0])

    now_ts = time.time()
    alerted_map = json_store.load_json_map(ALERTED_PATH, warn=log.warning)
    if _prune_past_alerts(alerted_map, now_ts):
        json_store.save_json_map(ALERTED_PATH, alerted_map)

    if not located:
        log.info(f"No upcoming located events in the next {LOOKAHEAD_DAYS} days")
        _clear_trip_requirement()
        return

    first_start = located[0][0]
    cluster_end = first_start + datetime.timedelta(hours=TRIP_CLUSTER_HOURS)
    cluster_size = len([item for item in located if item[0] < cluster_end])

    geocode_cache = json_store.load_json_map(GEOCODE_CACHE_PATH, warn=log.warning)
    route_cache = json_store.load_json_map(ROUTE_CACHE_PATH, warn=log.warning)
    household_map = json_store.load_json_map(HOUSEHOLD_MAP_PATH, warn=log.warning)
    caches_dirty = False

    if json_store.prune_expired(geocode_cache, now_ts, GEOCODE_CACHE_DAYS * 86400):
        caches_dirty = True
    if json_store.prune_expired(route_cache, now_ts, ROUTE_CACHE_HOURS * 3600 * 4):
        caches_dirty = True

    total_km = 0.0
    first_leg_min = None
    succeeded_keys = []
    pinned_count = 0
    estimated_count = 0
    accepted_now = set()

    # Every located event in the window is geocoded (cache backed), so an
    # invite is accepted as soon as it is in range, not only once it is
    # the next trip. Only the first cluster is routed and costed.
    for index, (start, ev) in enumerate(located):
        in_cluster = index < cluster_size
        is_first = index == 0
        uid = ev.get("uid") or f"nouid:{ev.get('summary', '')}"
        alert_key = json_store.occurrence_key(uid, start)
        location = ev["location"]
        summary = ev.get("summary") or "an event"
        notify_service = _notify_service_for(_person_email(ev), household_map)
        description = ev.get("description") or ""
        one_way = "TRIP_TYPE=ONE_WAY" in description

        pinned = _pinned_coords(description, summary)
        if pinned is not None:
            dest = pinned
            if in_cluster:
                pinned_count += 1
        else:
            dest, cache_hit = geocode_mod.geocode_cached(
                location, home_coords, geocode_cache, now_ts, NOMINATIM_UA
            )
            if not cache_hit:
                caches_dirty = True
        if dest is None:
            log.warning(f"Could not geocode '{location}' for event '{summary}'")
            _maybe_alert(
                alerted_map, alert_key, "geocode_failed",
                "Trip: location not found",
                f"Could not identify the location \"{location}\" for \"{summary}\". "
                f"Please check the address.",
                notify_service,
            )
            if is_first:
                _save_caches(geocode_cache, route_cache, caches_dirty)
                return
            continue

        _accept_if_due(ev, accepted_now)
        if not in_cluster:
            continue

        route, fresh = _route_cached(home_coords, dest, start, one_way, route_cache, now_ts)
        if fresh:
            caches_dirty = True
        estimated = route is None
        if estimated:
            # Waze refused or failed. Publishing nothing would leave the car
            # with no plan at all, so plan on a straight line estimate and
            # say so once. The estimate is never cached: the next run tries
            # Waze again, and a real route clears the alert.
            route = _estimate_route(home_coords, dest, one_way)
            estimated_count += 1
            log.warning(
                f"Waze route lookup failed for '{location}' (event '{summary}'); "
                f"using an estimate of {route['km']:.0f} km"
            )
            _maybe_alert(
                alerted_map, alert_key, "route_estimated",
                "Trip: route estimated",
                f"Waze could not calculate a route to \"{location}\" for "
                f"\"{summary}\". Charging is planned on an estimate of "
                f"{route['km']:.0f} km instead; it switches back to the real "
                f"route as soon as Waze answers again.",
                notify_service,
            )

        total_km += route["km"]
        if is_first:
            first_leg_min = route["out_min"]
        if not estimated:
            succeeded_keys.append(alert_key)

    _save_caches(geocode_cache, route_cache, caches_dirty)

    wh_per_km = _efficiency_wh_km()
    energy_needed_kwh = total_km * (wh_per_km / 1000)

    usable_kwh = get_usable_battery_kwh()
    required_soc_delta = (energy_needed_kwh / usable_kwh) * 100
    raw_target = required_soc_delta + SAFETY_BUFFER_PCT

    first_ev = located[0][1]
    first_uid = first_ev.get("uid") or f"nouid:{first_ev.get('summary', '')}"
    first_key = json_store.occurrence_key(first_uid, first_start)
    first_summary = first_ev.get("summary") or "an event"
    first_notify = _notify_service_for(_person_email(first_ev), household_map)

    if raw_target > 100:
        _maybe_alert(
            alerted_map, first_key, "insufficient_range",
            "Trip: charging stop needed",
            f"\"{first_summary}\" needs about {energy_needed_kwh:.0f} kWh "
            f"({total_km:.0f} km), more than a full charge. Plan a charging "
            f"stop along the way.",
            first_notify,
        )
    target_soc = min(100, raw_target)

    first_is_departure = _is_departure(first_ev)
    drive_offset_min = 0 if first_is_departure else (first_leg_min or 0)
    start_meaning = "departure" if first_is_departure else "arrival"

    deadline = first_start - datetime.timedelta(minutes=drive_offset_min + PREP_BUFFER_MIN)
    if deadline <= dt_util.now():
        deadline = dt_util.now()

    plan_soc, plan_deadline, plan_kind = _choose_plan(
        target_soc, deadline, _current_soc(), dt_util.now()
    )

    input_number.set_value(entity_id=TARGET_SOC_HELPER, value=round(plan_soc, 1))
    input_datetime.set_datetime(
        entity_id=TARGET_ETA_HELPER,
        datetime=plan_deadline.strftime("%Y-%m-%d %H:%M:%S"),
    )
    _set_notify_helper(first_notify if plan_kind == "trip" else DEFAULT_NOTIFY_SERVICE)
    if plan_kind == "trip":
        _publish_plan(plan_soc, plan_deadline, "trip", first_ev, total_km)
    else:
        _publish_plan(plan_soc, plan_deadline, plan_kind)

    # A real route clears an earlier "route estimated" alert, so the next
    # Waze failure is reported again. Only that one: the charging stop
    # alert shares the first event's key and used to repeat every run.
    cleared = False
    for key in succeeded_keys:
        if alerted_map.get(key) == "route_estimated":
            alerted_map.pop(key, None)
            cleared = True
    if cleared:
        json_store.save_json_map(ALERTED_PATH, alerted_map)

    log.info(
        f"Next trip cluster ({cluster_size} event(s), {pinned_count} pinned, "
        f"{estimated_count} estimated, "
        f"first: '{first_summary}'): "
        f"{total_km:.1f} km, {energy_needed_kwh:.1f} kWh at {wh_per_km:.0f} Wh/km, "
        f"target SOC {target_soc:.1f}% by {deadline.strftime('%Y-%m-%d %H:%M')} "
        f"(event starts {first_start.strftime('%H:%M')} as {start_meaning}, "
        f"{drive_offset_min:.0f} min drive + {PREP_BUFFER_MIN:.0f} min buffer)"
    )
    if plan_kind != "trip":
        log.info(
            f"Publishing the {FLOOR_SOC:.0f}% floor by "
            f"{plan_deadline.strftime('%Y-%m-%d %H:%M')} first: the car is "
            f"below it and the trip plan is due later"
        )
    elif FLOOR_SOC > 0 and plan_soc > target_soc:
        log.info(f"Trip needs less than the floor; publishing {plan_soc:.0f}%")


def _is_departure(ev):
    """True when the event's start is when the car leaves. TIME_IS=ARRIVAL
    (the form's "arrive by") wins over `own`; otherwise TIME_IS=DEPARTURE
    or a trip this calendar organizes is a departure, and an inbound
    invite is an arrival."""
    description = ev.get("description") or ""
    if "TIME_IS=ARRIVAL" in description:
        return False
    return "TIME_IS=DEPARTURE" in description or bool(ev.get("own"))


def _person_email(ev):
    """Whom an event's alerts are for: the invited member of a trip this
    calendar organizes, the organizer of an inbound invite."""
    if ev.get("own"):
        attendees = ev.get("attendees") or []
        return attendees[0] if attendees else None
    return ev.get("organizer")


def _event_place(ev):
    """Short place name for display: SUMMARY minus "Trip to "."""
    summary = str(ev.get("summary") or "").strip()
    if summary.lower().startswith("trip to "):
        return summary[8:].strip()
    return summary or str(ev.get("location") or "")


def _publish_plan(soc, deadline, kind, ev=None, km=None):
    """sensor.ev_trip_planner_plan: the plan just decided, for the card.
    The evcc automation keeps reading the helpers."""
    is_trip = kind == "trip" and ev is not None
    state.set(PLAN_SENSOR, round(float(soc), 1), {
        "version": CONTRACT_VERSION,
        "kind": kind,
        "deadline": deadline.isoformat(timespec="seconds") if deadline else None,
        "uid": ev.get("uid") if is_trip else None,
        "place": _event_place(ev) if is_trip else None,
        "km": round(km, 1) if is_trip and km is not None else None,
        "unit_of_measurement": "%",
        "friendly_name": "EV trip planner plan",
        "icon": "mdi:ev-station",
    })


def _clear_trip_requirement():
    """No upcoming located trip: publish the floor if set, otherwise reset
    the helpers to idle. Helpers are written only on a change; the plan
    sensor every time (it does not survive a restart)."""
    if FLOOR_SOC > 0:
        soc, deadline, _ = _choose_plan(None, None, None, dt_util.now())
        _publish_plan(soc, deadline, "floor")
        wanted_eta = deadline.strftime("%Y-%m-%d %H:%M:%S")
        unchanged = (
            _helper_float(TARGET_SOC_HELPER) == round(soc, 1)
            and _helper_state(TARGET_ETA_HELPER) == wanted_eta
        )
        if unchanged:
            return
        input_number.set_value(entity_id=TARGET_SOC_HELPER, value=round(soc, 1))
        input_datetime.set_datetime(entity_id=TARGET_ETA_HELPER, datetime=wanted_eta)
        _set_notify_helper(DEFAULT_NOTIFY_SERVICE)
        log.info(f"No upcoming trips, keeping the {soc:.0f}% floor, due {deadline.strftime('%Y-%m-%d %H:%M')}")
        return

    _publish_plan(IDLE_REQUIRED_SOC, None, "idle")
    current = _helper_state(TARGET_SOC_HELPER)
    try:
        already_idle = abs(float(current) - IDLE_REQUIRED_SOC) < 0.05
    except (TypeError, ValueError):
        already_idle = False
    if already_idle:
        return
    input_number.set_value(entity_id=TARGET_SOC_HELPER, value=IDLE_REQUIRED_SOC)
    input_datetime.set_datetime(entity_id=TARGET_ETA_HELPER, datetime=FAR_FUTURE)
    _set_notify_helper(DEFAULT_NOTIFY_SERVICE)
    log.info("No upcoming trips, reset required SOC and deadline")


# --------------------------------------------------------------------
# SOC floor
# --------------------------------------------------------------------

def _next_floor_ready(now):
    """The next floor_ready_hour strictly after now, local wall time."""
    ready = now.replace(hour=FLOOR_READY_HOUR, minute=0, second=0, microsecond=0)
    if ready <= now:
        ready = ready + datetime.timedelta(days=1)
    return ready


def _choose_plan(trip_soc, trip_deadline, current_soc, now):
    """The ONE plan to publish: (soc, deadline, "trip" | "floor").

    * no trip                         -> floor by the next ready hour
    * trip due before that hour       -> max(trip, floor) by the trip deadline
    * trip due later, car below the
      floor (or SOC unknown)          -> floor by the next ready hour first
    * trip due later, car at or above -> max(trip, floor) by the trip deadline
    """
    if FLOOR_SOC <= 0:
        return trip_soc, trip_deadline, "trip"
    ready = _next_floor_ready(now)
    if trip_soc is None:
        return FLOOR_SOC, ready, "floor"
    combined = max(trip_soc, FLOOR_SOC)
    if trip_deadline <= ready:
        return combined, trip_deadline, "trip"
    if current_soc is None or current_soc < FLOOR_SOC:
        return FLOOR_SOC, ready, "floor"
    return combined, trip_deadline, "trip"


def _current_soc():
    try:
        return float(state.get(SOC_SENSOR))
    except (TypeError, ValueError, NameError):
        return None


def _helper_state(entity_id):
    try:
        return state.get(entity_id)
    except NameError:
        return None


def _helper_float(entity_id):
    try:
        return round(float(_helper_state(entity_id)), 1)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------
# Event parsing
# --------------------------------------------------------------------

def _event_start(ev):
    """Tz aware local start for a list_events entry. All day events
    depart at all_day_departure_hour rather than midnight."""
    raw = ev.get("start")
    if not raw:
        return None
    raw = str(raw)
    if not ev.get("all_day"):
        parsed = dt_util.parse_datetime(raw)
        if parsed is not None:
            if parsed.tzinfo is None:
                return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return dt_util.as_local(parsed)
    day = dt_util.parse_date(raw[:10])
    if day is None:
        return None
    return dt_util.start_of_local_day(day) + datetime.timedelta(hours=ALL_DAY_DEPARTURE_HOUR)


def _efficiency_wh_km():
    """The efficiency sensor, or the EPA baseline when it is unavailable
    or missing (state.get raises NameError for a missing entity)."""
    try:
        return float(state.get(EFFICIENCY_SENSOR))
    except (TypeError, ValueError, NameError):
        log.warning(f"{EFFICIENCY_SENSOR} is unavailable, falling back to {FALLBACK_WH_KM} Wh/km")
        return FALLBACK_WH_KM


_GEO_PIN_RE = re.compile(r"GEO=\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def _pinned_coords(description, summary):
    """(lat, lon) from a GEO=lat,lon pin in DESCRIPTION, or None."""
    if "GEO=" not in description:
        return None
    match = _GEO_PIN_RE.search(description)
    if match:
        lat = float(match.group(1))
        lon = float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    log.warning(f"Ignoring unusable GEO= pin on '{summary}', looking up the location text instead")
    return None


# --------------------------------------------------------------------
# Routing, with cache
# --------------------------------------------------------------------

def _route_cached(home_coords, dest_coords, event_start, one_way, cache, now_ts):
    """({'km': total, 'out_min': minutes} or None, recalculated)."""
    key = (
        f"{home_coords[0]:.5f},{home_coords[1]:.5f}"
        f"->{dest_coords[0]:.5f},{dest_coords[1]:.5f}"
        f"|{'one' if one_way else 'round'}"
    )
    hours_away = (event_start - dt_util.now()).total_seconds() / 3600
    entry = cache.get(key)

    if (
        isinstance(entry, dict)
        and "km" in entry
        and hours_away > NEAR_TRIP_HOURS
        and now_ts - entry.get("ts", 0) < ROUTE_CACHE_HOURS * 3600
    ):
        return {"km": entry["km"], "out_min": entry["out_min"]}, False

    start = f"{home_coords[0]},{home_coords[1]}"
    end = f"{dest_coords[0]},{dest_coords[1]}"
    out_km, out_min, back_km, back_min = _waze_legs(start, end, not one_way)

    if out_km is None:
        return None, False
    if not one_way and back_km is None:
        return None, False

    total = out_km if one_way else out_km + back_km
    cache[key] = {"km": total, "out_min": out_min, "ts": now_ts}
    return {"km": total, "out_min": out_min}, True


def _estimate_route(home_coords, dest_coords, one_way):
    """{'km', 'out_min'} from the great circle distance times
    fallback_detour_factor, at fallback_speed_kmh. Erring long is the
    safe side for a charge plan."""
    lat1, lon1 = home_coords
    lat2, lon2 = dest_coords
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = (
        math.sin(dlat / 2) ** 2
        + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    )
    leg_km = 2 * 6371.0 * math.asin(math.sqrt(a)) * FALLBACK_DETOUR_FACTOR
    out_min = leg_km / FALLBACK_SPEED_KMH * 60
    total = leg_km if one_way else 2 * leg_km
    return {"km": total, "out_min": out_min}


def _waze_legs(start, end, include_return):
    """Both legs via the shared routing module (Waze with curl_cffi);
    (out_km, out_min, back_km, back_min), Nones on failure."""
    return routing_mod.waze_legs(start, end, include_return)


def _save_caches(geocode_cache, route_cache, dirty):
    if not dirty:
        return
    json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)
    json_store.save_json_map(ROUTE_CACHE_PATH, route_cache)


def get_usable_battery_kwh():
    """Placeholder for live usable capacity from the vehicle integration."""
    # TODO: wire up once it's confirmed which sensor exposes usable capacity.
    return FALLBACK_USABLE_KWH


# --------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------

def _fallback_notify(reason):
    """DEFAULT_NOTIFY_SERVICE, loudly: reaching it means the person could
    not be resolved, which is the real bug."""
    log.warning(f"Could not route a trip alert to its person ({reason}), falling back to {DEFAULT_NOTIFY_SERVICE}")
    return DEFAULT_NOTIFY_SERVICE


def _set_notify_helper(service_name):
    """Publish the resolved notify service for the evcc plan automation.
    A missing helper is a warning, not a failure."""
    if not service_name:
        return
    try:
        current = state.get(TARGET_NOTIFY_HELPER)
    except NameError:
        log.warning(
            f"{TARGET_NOTIFY_HELPER} does not exist; create it as a Text "
            f"helper, or the evcc automation falls back to its own default"
        )
        return
    if current == service_name:
        return
    input_text.set_value(entity_id=TARGET_NOTIFY_HELPER, value=service_name)


def _notify_service_for(email, household_map):
    """Email -> their notify service, via the household map. Case blind:
    calendars rewrite address case freely."""
    if not email:
        return _fallback_notify("no person on file for this event")
    wanted = str(email).strip().lower()
    for record in household_map.values():
        if str(record.get("email") or "").strip().lower() == wanted:
            service_name = record.get("notify_service")
            if service_name:
                return service_name
            return _fallback_notify(f"{email} has no notify_service in {HOUSEHOLD_MAP_PATH}")
    return _fallback_notify(f"{email} is not in {HOUSEHOLD_MAP_PATH}")


def _maybe_alert(alerted_map, alert_key, failure_type, title, message, notify_service):
    """alert_failure() once per (occurrence, failure_type)."""
    if not alert_key:
        alert_failure(title, message, notify_service, "unknown")
        return
    if alerted_map.get(alert_key) == failure_type:
        log.info(f"Already alerted for {alert_key} ({failure_type}), not repeating")
        return
    alert_failure(title, message, notify_service, alert_key)
    alerted_map[alert_key] = failure_type
    json_store.save_json_map(ALERTED_PATH, alerted_map)


def alert_failure(title, message, notify_service=DEFAULT_NOTIFY_SERVICE, event_uid="unknown"):
    """Persistent notification plus a push to the event's person.
    notification_id includes the occurrence key, so two failures don't
    overwrite each other. List comprehension, not a generator expression:
    pyscript doesn't implement those."""
    slug = "".join([ch if ch.isalnum() else "_" for ch in str(event_uid)])[-40:]
    persistent_notification.create(
        title=title,
        message=message,
        notification_id=f"ev_trip_energy_{slug}",
    )
    domain, _, svc = notify_service.partition(".")
    try:
        service.call(domain, svc, title=title, message=message)
    except Exception as e:
        log.warning(f"Push via {notify_service} failed: {e}")
