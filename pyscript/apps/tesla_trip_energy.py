"""
Tesla trip energy calculator (pyscript app)
---------------------------------------------
Reads upcoming events from calendar.tesla, geocodes any LOCATION field,
calculates distance/energy via Waze, and publishes the SOC the car needs
and the time by which it needs it.

Requires (add to /config/pyscript/requirements.txt):
    icalendar
    requests
    pywaze

Requires /config/pyscript/modules/tesla_file_io.py, tesla_json_store.py
and tesla_ics_store.py (shared modules, also used by tesla_calendar.py).

Requires in configuration.yaml:
    pyscript:
      apps:
        tesla_trip_energy:
          nominatim_user_agent: "my-ha-ev-scheduler/1.0 (contact: you@example.com)"
          usable_battery_kwh: 79.0        # Model 3 LR RWD fallback
          safety_buffer_pct: 10           # arrival buffer on top of calculated need
          lookahead_days: 7
          default_notify_service: "notify.mobile_app_<device>"   # NEVER notify.notify
          ics_path: "/config/www/tesla.ics"       # read-only, for UID lookup
          # --- optional, defaults shown ---
          efficiency_sensor: "sensor.tesla_adjusted_efficiency_wh_km"
          fallback_efficiency_wh_km: 153  # EPA baseline, used if the sensor is unavailable
          prep_buffer_min: 15             # getting-out-the-door time before departure
          trip_cluster_hours: 12          # trips within this window are budgeted together
          all_day_departure_hour: 8       # assumed departure for date-only events
          near_trip_hours: 6              # inside this window, always re-route (live traffic)
          route_cache_hours: 6            # outside it, reuse a cached distance this long
          idle_required_soc: 0            # written when there is no upcoming trip
          floor_soc: 0                    # SOC floor kept while plugged in; 0 = off
          floor_ready_hour: 7             # the floor is due by this local hour
          soc_sensor: "sensor.calimero_battery_level"   # read to order floor vs trip

WHAT CHANGED AND WHY (the non-obvious bits):

* SOC FLOOR (1 Oct). The battery is kept between floor_soc (50) and the car's
  80% limit while plugged in, the band Tesla's guidance points at (about 80%
  daily, about 50% for storage). Without it, evcc in smart mode with no
  trip charges on solar surplus only, which 2 kWp barely ever delivers, so
  the car could sit near empty at home. The floor is published through the
  SAME two helpers as a trip, as ONE plan: evcc only works towards its
  next plan, so a separate repeating floor plan in evcc would starve a
  trip plan due shortly after it. Which plan wins is decided in
  _choose_plan(). Everything above the floor (50 to 80) is evcc's job:
  solar surplus, plus its smart cost limit for cheap grid hours.
  floor_soc: 0 keeps the old behaviour exactly (idle values, 2099 deadline).

* Deadline is DEPARTURE, not event start — and what "event start" MEANS
  depends on where the event came from. An inbound calendar invite's
  DTSTART is when you have to BE somewhere, so the outbound leg comes off
  it: deadline = event_start - travel_time - prep_buffer_min. A manually
  scheduled trip's DTSTART is the DEPARTURE time the user typed into the
  dashboard form, so only prep_buffer_min comes off it. Subtracting the
  drive from both put a ten-hour trip departing 07:00 tomorrow at a
  deadline of the previous evening, which the evcc plan publisher's
  15-minute floor then turned into "charge flat out right now". Manual
  trips are identified by TIME_IS=DEPARTURE in DESCRIPTION, with a
  manual- UID prefix as fallback for trips booked before that marker
  existed.

* Trips are budgeted in CLUSTERS. Only the single nearest located event
  used to be considered. Two trips three hours apart meant the car was
  charged for the first, came home near empty, and had no time to charge
  before the second. Every located event starting within
  trip_cluster_hours of the first is now summed. This is deliberately
  conservative — it ignores any charging that might happen in between.

* Geocode and route results are CACHED. At one call per location per leg
  every 5 minutes, a single unchanging event a week out generated ~288
  Nominatim and ~576 Waze requests a day. Nominatim's usage policy
  explicitly asks clients to cache and not repeat identical queries; this
  is the kind of traffic that gets a User-Agent blocked. Geocodes are
  cached for 30 days keyed on the location string; routes are cached for
  route_cache_hours and always recalculated inside near_trip_hours, when
  live traffic actually matters.

* A GEO=lat,lon line in the event DESCRIPTION is HONOURED. The dashboard's
  destination picker pins the branch the user chose onto the event; this
  app used to ignore it and geocode the LOCATION text instead, so an
  ambiguous address could be costed against a different place than the
  one on screen. A pinned event now skips geocoding entirely. A malformed
  pin logs a warning and falls back to the LOCATION text.

* The fallback notify service must NOT be a broadcast service.
  `notify.notify` fans out to every registered notify platform, so an
  alert meant for one trip's organizer reaches every phone in the house.
  It was the documented default and it did exactly that. The in-code
  default is now `notify.persistent_notification`, and every fallback to
  the default is logged with the reason, because a fallback means the
  organizer could not be resolved and that is the real bug.

* UIDs are resolved from tesla.ics, not from calendar.get_events. The
  Remote Calendar integration does NOT return a uid in get_events (checked
  24 Sep: start, end, summary, description, location only). Every event
  therefore got a synthetic "summary|start" key, which can never match the
  manual-<uuid> keys tesla_calendar.py writes into the organizer map. So
  organizer routing always fell back to the default notify service, and
  the dedup markers were keyed on something nothing else could clear.
  _event_uid() now looks the real UID up in tesla.ics by summary + start
  time, and only falls back to the synthetic key (with a warning) when
  that match fails or is ambiguous. The file is only READ here; this app
  never writes it, and a corrupt file is left for tesla_calendar.py to
  quarantine.

* RECURRING EVENTS (1 Oct). Remote Calendar expands RRULE itself, so
  get_events already returns each occurrence. Two things here did not
  cope with that. The UID index only knew each series' FIRST start, so
  every later occurrence fell to a synthetic key and organizer routing
  fell back; _ics_uid_index() now expands each series over the lookahead
  window (and indexes moved instances by their new start). And the
  failure-alert dedup was keyed per UID, so an alert for this week's
  instance suppressed next week's; it is keyed per occurrence now
  (json_store.occurrence_key()). Existing per-UID entries in the alert
  map are simply never matched again, so a still-failing event alerts
  once more after the upgrade.

* A broadcast default notify service is REFUSED, not just warned about.
  default_notify_service has been set to notify.notify more than once. It
  is now replaced by notify.persistent_notification at load, with an
  error in the log, so a misconfiguration can never broadcast again.

* The helpers are RESET when there is no trip. They used to keep the last
  trip's values forever, so once the final calendar event passed, the
  charging automation would keep charging to that SOC against a deadline
  in the past, indefinitely.

This file must live at /config/pyscript/apps/tesla_trip_energy.py — the
filename (minus .py) must match the app name used in configuration.yaml.

Assumes these helpers exist in Home Assistant:
    input_number.next_trip_required_soc   (0-100, step 0.1)
    input_datetime.next_trip_deadline
"""
import re
import time
import logging
import datetime
import homeassistant.util.dt as dt_util
import pywaze.route_calculator as route_calculator
import tesla_json_store as json_store
import tesla_ics_store as ics_store
import tesla_geocode as geocode_mod
import tesla_file_io
from icalendar import Calendar

_logger = logging.getLogger(__name__)

NOMINATIM_UA = pyscript.app_config["nominatim_user_agent"]
FALLBACK_USABLE_KWH = float(pyscript.app_config.get("usable_battery_kwh", 79.0))
SAFETY_BUFFER_PCT = float(pyscript.app_config.get("safety_buffer_pct", 10))
LOOKAHEAD_DAYS = int(pyscript.app_config.get("lookahead_days", 7))

EFFICIENCY_SENSOR = pyscript.app_config.get(
    "efficiency_sensor", "sensor.tesla_adjusted_efficiency_wh_km"
)
FALLBACK_WH_KM = float(pyscript.app_config.get("fallback_efficiency_wh_km", 153))
PREP_BUFFER_MIN = float(pyscript.app_config.get("prep_buffer_min", 15))
TRIP_CLUSTER_HOURS = float(pyscript.app_config.get("trip_cluster_hours", 12))
ALL_DAY_DEPARTURE_HOUR = int(pyscript.app_config.get("all_day_departure_hour", 8))
NEAR_TRIP_HOURS = float(pyscript.app_config.get("near_trip_hours", 6))
ROUTE_CACHE_HOURS = float(pyscript.app_config.get("route_cache_hours", 6))
IDLE_REQUIRED_SOC = float(pyscript.app_config.get("idle_required_soc", 0))
FLOOR_SOC = float(pyscript.app_config.get("floor_soc", 0))
FLOOR_READY_HOUR = int(pyscript.app_config.get("floor_ready_hour", 7))
SOC_SENSOR = pyscript.app_config.get("soc_sensor", "sensor.calimero_battery_level")

# Must match ORGANIZER_MAP_PATH in tesla_calendar.py — that app writes
# this file, this app only reads it, so the organizer of a failing event
# is notified directly instead of always defaulting to one person.
ORGANIZER_MAP_PATH = pyscript.app_config.get(
    "organizer_map_path", "/config/pyscript/tesla_organizer_map.json"
)
# Same file tesla_calendar.py reads for user_id -> email. This app only
# knows an event's organizer by EMAIL, so _notify_service_for() builds an
# email -> notify_service reverse index from it at call time.
HOUSEHOLD_MAP_PATH = "/config/pyscript/tesla_household.json"
# `notify.notify` is Home Assistant's BROADCAST service: it fans out to
# every registered notify platform, so one person's trip alert arrives on
# every phone in the house. It is deliberately not the default here, and
# _notify_service_for() warns if it has been configured anyway.
_SAFE_NOTIFY_SERVICE = "notify.persistent_notification"
DEFAULT_NOTIFY_SERVICE = str(pyscript.app_config.get(
    "default_notify_service", _SAFE_NOTIFY_SERVICE
)).strip()
if DEFAULT_NOTIFY_SERVICE in ("notify.notify", "notify", ""):
    log.error(
        f"default_notify_service is '{DEFAULT_NOTIFY_SERVICE}', which "
        f"BROADCASTS to every phone. Using {_SAFE_NOTIFY_SERVICE} instead. "
        f"Set it to one person's notify.mobile_app_<device> in "
        f"/config/pyscript/config.yaml under apps: tesla_trip_energy:"
    )
    DEFAULT_NOTIFY_SERVICE = _SAFE_NOTIFY_SERVICE

# Read-only: tesla_calendar.py owns this file. Used to recover the real
# event UID, which calendar.get_events does not return.
ICS_PATH = pyscript.app_config.get("ics_path", "/config/www/tesla.ics")

# Occurrence key ("<uid>|occ=<start ts>") -> last-alerted failure type, so
# a persistently-failing occurrence only notifies once instead of every 5
# minutes. Cleared on success, on a different failure type, and by
# tesla_calendar.py on cancellation (json_store.pop_uid clears every
# occurrence of a UID at once).
ALERTED_PATH = "/config/pyscript/tesla_trip_energy_alerted.json"

GEOCODE_CACHE_PATH = "/config/pyscript/tesla_geocode_cache.json"
ROUTE_CACHE_PATH = "/config/pyscript/tesla_route_cache.json"
GEOCODE_CACHE_DAYS = 30

CALENDAR_ENTITY = "calendar.tesla"
TARGET_SOC_HELPER = "input_number.next_trip_required_soc"
TARGET_ETA_HELPER = "input_datetime.next_trip_deadline"

# Third published helper, read by the "evcc publish trip plan" automation.
# That automation only ever sees the SOC and deadline helpers, so it has no
# way to know who booked the trip and its charge-limit notification was
# hardcoded to one person. Publishing the resolved notify service here lets
# it route to whoever _notify_service_for() picked, the same as this app's
# own alerts. Optional: a missing helper degrades to the automation's own
# fallback rather than breaking a trip calculation.
TARGET_NOTIFY_HELPER = "input_text.next_trip_notify_service"

FAR_FUTURE = "2099-01-01 00:00:00"


@time_trigger("cron(*/5 * * * *)")
@service
def check_next_trip_energy():
    """Compute the SOC and deadline for the next cluster of located trips."""
    log.info("Checking calendar.tesla for upcoming located trips")

    home = state.getattr("zone.home")
    if not home or "latitude" not in home:
        log.warning("zone.home is unavailable — cannot calculate trip energy")
        return
    home_coords = (home["latitude"], home["longitude"])

    try:
        events = calendar.get_events(
            entity_id=CALENDAR_ENTITY,
            start_date_time=dt_util.now(),
            duration={"days": LOOKAHEAD_DAYS},
        )
    except Exception as e:
        log.warning(f"calendar.get_events failed: {e}")
        return

    upcoming = events.get(CALENDAR_ENTITY, {}).get("events", [])
    uid_index = None
    if any([not ev.get("uid") for ev in upcoming]):
        uid_index = _ics_uid_index()

    located = []
    for ev in upcoming:
        if not ev.get("location"):
            continue
        start = _event_start(ev)
        if start is None:
            log.warning(f"Skipping event with unparseable start: {ev.get('start')}")
            continue
        located.append((start, ev))
    located.sort(key=lambda item: item[0])

    alerted_map = json_store.load_json_map(ALERTED_PATH, warn=log.warning)

    if not located:
        log.info(f"No upcoming located events in the next {LOOKAHEAD_DAYS} days")
        _clear_trip_requirement()
        return

    first_start = located[0][0]
    cluster_end = first_start + datetime.timedelta(hours=TRIP_CLUSTER_HOURS)
    cluster = [item for item in located if item[0] < cluster_end]

    organizer_map = json_store.load_json_map(ORGANIZER_MAP_PATH, warn=log.warning)
    geocode_cache = json_store.load_json_map(GEOCODE_CACHE_PATH, warn=log.warning)
    route_cache = json_store.load_json_map(ROUTE_CACHE_PATH, warn=log.warning)
    now_ts = time.time()
    caches_dirty = False

    if json_store.prune_expired(geocode_cache, now_ts, GEOCODE_CACHE_DAYS * 86400):
        caches_dirty = True
    if json_store.prune_expired(route_cache, now_ts, ROUTE_CACHE_HOURS * 3600 * 4):
        caches_dirty = True

    total_km = 0.0
    first_leg_min = None
    succeeded_keys = []
    pinned_count = 0

    for index, (start, ev) in enumerate(cluster):
        uid = _event_uid(ev, uid_index)
        # Per occurrence, not per UID: a recurring series shares one UID,
        # and a UID key let this week's alert silence next week's.
        alert_key = json_store.occurrence_key(uid, start)
        location = ev["location"]
        summary = ev.get("summary") or "an event"
        notify_service = _notify_service_for(organizer_map.get(uid) if uid else None)
        is_first = index == 0
        description = ev.get("description") or ""
        one_way = "TRIP_TYPE=ONE_WAY" in description

        pinned = _pinned_coords(description, summary)
        if pinned is not None:
            dest = pinned
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
                "Tesla trip: location not found",
                f"Could not identify the location \"{location}\" for \"{summary}\". "
                f"Please check the address.",
                notify_service,
            )
            if is_first:
                # No distance and no travel time for the event that sets the
                # deadline — nothing meaningful to publish.
                _save_caches(geocode_cache, route_cache, caches_dirty)
                return
            continue

        route, fresh = _route_cached(
            home_coords, dest, start, one_way, route_cache, now_ts
        )
        if fresh:
            caches_dirty = True
        if route is None:
            log.warning(f"Waze route lookup failed for '{location}' (event '{summary}')")
            _maybe_alert(
                alerted_map, alert_key, "route_failed",
                "Tesla trip: route not found",
                f"Could not calculate a route to \"{location}\" for \"{summary}\".",
                notify_service,
            )
            if is_first:
                _save_caches(geocode_cache, route_cache, caches_dirty)
                return
            continue

        total_km += route["km"]
        if is_first:
            first_leg_min = route["out_min"]
        succeeded_keys.append(alert_key)

    _save_caches(geocode_cache, route_cache, caches_dirty)

    wh_per_km = _efficiency_wh_km()
    energy_needed_kwh = total_km * (wh_per_km / 1000)

    usable_kwh = get_usable_battery_kwh()
    required_soc_delta = (energy_needed_kwh / usable_kwh) * 100
    raw_target = required_soc_delta + SAFETY_BUFFER_PCT

    first_uid = _event_uid(cluster[0][1], uid_index)
    first_key = json_store.occurrence_key(first_uid, first_start)
    first_summary = cluster[0][1].get("summary") or "an event"
    first_notify = _notify_service_for(organizer_map.get(first_uid) if first_uid else None)

    if raw_target > 100:
        # Silently clamping to 100 reported success for a trip the car
        # physically cannot make without stopping to charge.
        _maybe_alert(
            alerted_map, first_key, "insufficient_range",
            "Tesla trip: charging stop needed",
            f"\"{first_summary}\" needs about {energy_needed_kwh:.0f} kWh "
            f"({total_km:.0f} km) — more than a full charge. Plan a charging "
            f"stop along the way.",
            first_notify,
        )
    target_soc = min(100, raw_target)

    # DTSTART means different things depending on where the event came
    # from. A manually scheduled trip carries the DEPARTURE time typed
    # into the dashboard form; an inbound invite carries the time you
    # have to BE somewhere, so the drive has to come off it. Subtracting
    # the drive from both put a ten-hour trip departing 07:00 tomorrow at
    # a deadline of the previous evening, and the plan publisher's
    # 15-minute floor then told evcc to charge flat out immediately.
    #
    # TIME_IS=DEPARTURE is written by tesla_calendar.schedule_manual_trip().
    # The manual- UID prefix is the fallback: it covers trips booked
    # before the marker existed, and it covers the case where Remote
    # Calendar does not hand back a DESCRIPTION at all.
    first_description = cluster[0][1].get("description") or ""
    first_is_manual = (
        "TIME_IS=DEPARTURE" in first_description
        or first_uid.startswith("manual-")
    )
    drive_offset_min = 0 if first_is_manual else (first_leg_min or 0)
    start_meaning = "departure" if first_is_manual else "arrival"

    deadline = first_start - datetime.timedelta(
        minutes=drive_offset_min + PREP_BUFFER_MIN
    )
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
    _set_notify_helper(
        first_notify if plan_kind == "trip" else DEFAULT_NOTIFY_SERVICE
    )

    # Success — clear earlier failure alerts, so a later break is treated
    # as a fresh problem worth alerting on again.
    cleared = False
    for key in succeeded_keys:
        if alerted_map.pop(key, None) is not None:
            cleared = True
    if cleared:
        json_store.save_json_map(ALERTED_PATH, alerted_map)

    log.info(
        f"Next trip cluster ({len(cluster)} event(s), {pinned_count} pinned, "
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
            f"below it and the trip plan is due later. The trip plan follows "
            f"once the floor is reached"
        )
    elif FLOOR_SOC > 0 and plan_soc > target_soc:
        log.info(f"Trip needs less than the floor; publishing {plan_soc:.0f}%")


def _clear_trip_requirement():
    """No upcoming located trip.

    With floor_soc set, publish the floor by the next floor_ready_hour,
    so the car never sits below the band at home. Without it, reset the
    helpers to idle so the charging automation falls back to its
    economical default instead of chasing a trip that already happened.
    Either way, writes only on a change, to keep the recorder quiet.
    """
    if FLOOR_SOC > 0:
        soc, deadline, _ = _choose_plan(None, None, None, dt_util.now())
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
        log.info(
            f"No upcoming trips — keeping the {soc:.0f}% floor, due "
            f"{deadline.strftime('%Y-%m-%d %H:%M')}"
        )
        return

    current = state.get(TARGET_SOC_HELPER)
    try:
        already_idle = abs(float(current) - IDLE_REQUIRED_SOC) < 0.05
    except (TypeError, ValueError):
        already_idle = False
    if already_idle:
        return
    input_number.set_value(entity_id=TARGET_SOC_HELPER, value=IDLE_REQUIRED_SOC)
    input_datetime.set_datetime(entity_id=TARGET_ETA_HELPER, datetime=FAR_FUTURE)
    _set_notify_helper(DEFAULT_NOTIFY_SERVICE)
    log.info("No upcoming trips — reset required SOC and deadline")


# --------------------------------------------------------------------
# SOC floor
# --------------------------------------------------------------------

def _next_floor_ready(now):
    """The next floor_ready_hour strictly after now, local wall time
    (aware arithmetic on one zoneinfo keeps 07:00 at 07:00 across DST)."""
    ready = now.replace(hour=FLOOR_READY_HOUR, minute=0, second=0, microsecond=0)
    if ready <= now:
        ready = ready + datetime.timedelta(days=1)
    return ready


def _choose_plan(trip_soc, trip_deadline, current_soc, now):
    """The ONE plan to publish: (soc, deadline, "trip" | "floor").

    evcc works towards its next plan only, which is why the floor and a
    trip are merged here instead of being two plans in evcc:

    * no trip                       -> floor by the next ready hour
    * trip due before that hour     -> max(trip, floor) by the trip deadline
    * trip due later, car below the
      floor (or SOC unknown)        -> floor by the next ready hour first;
                                       the next run, once it is reached,
                                       switches to the trip plan
    * trip due later, car at or
      above the floor               -> max(trip, floor) by the trip deadline

    An unknown SOC is treated as below the floor: the worst case is the
    trip plan starting one night later, still well ahead of its deadline.
    With floor_soc 0 the trip values pass through untouched.
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
    """The car's SOC, or None when the sensor is missing or not numeric."""
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
    """Tz-aware local start time for a calendar.get_events entry.

    All-day events come back as a date-only string ("2026-09-03"), which
    cv.datetime rejects — the old code passed it straight to
    input_datetime.set_datetime and the service call errored out AFTER the
    SOC had already been written, leaving SOC updated and deadline stale.
    Date-only events are assumed to depart at all_day_departure_hour
    rather than midnight.
    """
    raw = ev.get("start")
    if not raw:
        return None
    parsed = dt_util.parse_datetime(raw)
    if parsed is not None:
        return dt_util.as_local(parsed)
    day = dt_util.parse_date(raw)
    if day is None:
        return None
    return dt_util.start_of_local_day(day) + datetime.timedelta(
        hours=ALL_DAY_DEPARTURE_HOUR
    )


def _uid_key(summary, start):
    """Match key shared by calendar.get_events entries and tesla.ics
    VEVENTs: stripped summary plus the start as a whole-second timestamp,
    so timezone representation differences don't matter."""
    return f"{(summary or '').strip()}|{int(start.timestamp())}"


def _is_all_day(component):
    raw = component.get("DTSTART")
    return raw is not None and not isinstance(raw.dt, datetime.datetime)


def _ics_start(component):
    """Start of a VEVENT, normalised exactly like _event_start() does for
    get_events entries (all-day -> all_day_departure_hour), or None."""
    raw = component.get("DTSTART")
    if raw is None:
        return None
    value = raw.dt
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return dt_util.as_local(value)
    return dt_util.start_of_local_day(value) + datetime.timedelta(
        hours=ALL_DAY_DEPARTURE_HOUR
    )


_AMBIGUOUS = object()


def _index_put(index, key, uid):
    """Add key -> uid. The same key from two DIFFERENT UIDs is ambiguous
    and stored as _AMBIGUOUS; the same key twice from one UID (a moved
    instance whose original slot is also expanded from the master) is
    fine."""
    existing = index.get(key)
    if existing is None:
        index[key] = uid
    elif existing != uid:
        index[key] = _AMBIGUOUS


def _ics_uid_index():
    """{match key: UID} built from tesla.ics, or {} if it can't be read.

    Read-only on purpose: tesla_ics_store.load_or_create_calendar() moves
    an unparseable file aside, and that decision belongs to
    tesla_calendar.py, not to this app. A key shared by two events (same
    summary, same start) maps to None: guessing between them would route
    an alert to the wrong organizer.

    A recurring master is indexed at every occurrence inside the
    lookahead window (plus a day either side for timezone slack), because
    get_events returns each occurrence with its own start while the file
    only holds the first. Overrides (moved instances) are indexed at
    their own DTSTART, which is what get_events reports for them. A
    series that fails to expand still gets its first start, as before.
    """
    try:
        cal = Calendar.from_ical(tesla_file_io.read_file(ICS_PATH))
    except Exception as e:
        log.warning(f"Could not read {ICS_PATH} for UID lookup: {e}")
        return {}
    window_start = dt_util.now() - datetime.timedelta(days=1)
    window_end = dt_util.now() + datetime.timedelta(days=LOOKAHEAD_DAYS + 1)
    index = {}
    for c in cal.subcomponents:
        if c.name != "VEVENT" or c.get("UID") is None:
            continue
        uid = str(c.get("UID"))
        summary = str(c.get("SUMMARY", ""))
        start = _ics_start(c)
        if start is None:
            continue
        _index_put(index, _uid_key(summary, start), uid)
        if ics_store.is_recurring(c) and ics_store.recurrence_key(c) is None:
            shift = (
                datetime.timedelta(hours=ALL_DAY_DEPARTURE_HOUR)
                if _is_all_day(c) else datetime.timedelta(0)
            )
            for occ in ics_store.occurrence_starts(c, window_start, window_end):
                _index_put(index, _uid_key(summary, dt_util.as_local(occ) + shift), uid)
    return {k: (None if v is _AMBIGUOUS else v) for k, v in index.items()}


def _event_uid(ev, uid_index=None):
    """Real event UID for dedup and organizer lookup.

    calendar.get_events on the Remote Calendar integration does not return
    a uid, so the UID is recovered from tesla.ics by summary + start. Only
    if that fails too does this fall back to a synthetic key, which keeps
    dedup working but can never match the organizer map, hence the
    warning. (No key at all would make _maybe_alert() alert every 5
    minutes, which is why the synthetic key exists.)
    """
    uid = ev.get("uid")
    if uid:
        return str(uid)
    start = _event_start(ev)
    if uid_index and start is not None:
        found = uid_index.get(_uid_key(ev.get("summary"), start))
        if found:
            return found
    log.warning(
        f"No UID for '{ev.get('summary', '')}' at {ev.get('start', '')} in "
        f"{ICS_PATH} — using a synthetic key; organizer routing will fall back"
    )
    return f"synthetic:{ev.get('summary', '')}|{ev.get('start', '')}"


def _efficiency_wh_km():
    """Read the efficiency sensor, falling back to the EPA baseline.

    float(state.get(...)) on an 'unknown'/'unavailable' sensor raised and
    aborted the entire run with no notification and no fallback — and the
    sensor depends on the weather integration, which does go unavailable.
    """
    try:
        return float(state.get(EFFICIENCY_SENSOR))
    except (TypeError, ValueError, NameError):
        # NameError: pyscript's state.get() raises it for an entity that
        # doesn't exist at all (renamed, deleted), which the old
        # (TypeError, ValueError) let through to abort the whole run.
        log.warning(
            f"{EFFICIENCY_SENSOR} is unavailable — falling back to "
            f"{FALLBACK_WH_KM} Wh/km"
        )
        return FALLBACK_WH_KM


_GEO_PIN_RE = re.compile(r"GEO=\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)")


def _pinned_coords(description, summary):
    """Return (lat, lon) from a GEO=lat,lon line in the event description,
    or None when there is no usable pin.

    Written by tesla_calendar.schedule_manual_trip() when the user picked
    a destination in the dashboard search. A regex rather than a line
    split, so it works whether the calendar integration hands back real
    newlines or escaped ones.

    Out-of-range values are rejected rather than trusted: the calendar
    app validates before writing, but DESCRIPTION is free text that any
    calendar client can edit. Rejecting falls back to geocoding LOCATION,
    which is the pre-picker behaviour, not a failure.
    """
    if "GEO=" not in description:
        return None
    match = _GEO_PIN_RE.search(description)
    if match:
        lat = float(match.group(1))
        lon = float(match.group(2))
        if -90 <= lat <= 90 and -180 <= lon <= 180:
            return (lat, lon)
    log.warning(
        f"Ignoring unusable GEO= pin on '{summary}' — "
        f"looking up the location text instead"
    )
    return None


# --------------------------------------------------------------------
# Geocoding lives in the shared tesla_geocode module now (geocode_mod
# .geocode_cached() / .geocode()) — see that file's docstring for why:
# tesla_calendar.py needs the same Nominatim-calling, cached, closest-
# to-home logic for its own accept-RSVP gating, and duplicating it here
# risked the two copies drifting apart over time.
# --------------------------------------------------------------------


# --------------------------------------------------------------------
# Routing, with cache
# --------------------------------------------------------------------

def _route_cached(home_coords, dest_coords, event_start, one_way, cache, now_ts):
    """Returns ({'km': total, 'out_min': minutes} or None, recalculated).

    Recalculates when the event is inside near_trip_hours (live traffic
    matters then) or the cached value is older than route_cache_hours.
    Otherwise reuses the cache — a route to a fixed address doesn't
    change materially from one 5-minute poll to the next.
    """
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


@pyscript_executor
def _waze_legs(start, end, include_return):
    """Both legs over ONE client and ONE event loop.

    Previously each leg was its own asyncio.run() -> new event loop -> new
    httpx.AsyncClient -> new TLS handshake.

    Returns (out_km, out_min, back_km, back_min); Nones on failure.
    """
    import asyncio

    async def _calc():
        async with route_calculator.WazeRouteCalculator() as client:
            out = (await client.calc_routes(start, end))[0]
            if not include_return:
                return out.distance, out.duration, None, None
            back = (await client.calc_routes(end, start))[0]
            return out.distance, out.duration, back.distance, back.duration

    try:
        return asyncio.run(_calc())
    except Exception as e:
        _logger.warning(f"Waze route calc failed: {e}")
        return None, None, None, None


def _save_caches(geocode_cache, route_cache, dirty):
    if not dirty:
        return
    json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)
    json_store.save_json_map(ROUTE_CACHE_PATH, route_cache)


def get_usable_battery_kwh():
    """Placeholder for pulling live usable capacity from the Tesla Fleet API.
    Falls back to the configured constant if not available."""
    # TODO: wire up to the actual Fleet API charge-state sensor once you've
    # confirmed which attribute exposes usable capacity for this vehicle.
    return FALLBACK_USABLE_KWH


# --------------------------------------------------------------------
# Notifications
# --------------------------------------------------------------------

def _fallback_notify(reason):
    """Return DEFAULT_NOTIFY_SERVICE, loudly.

    Every path that reaches the default has failed to identify who the
    alert is for. That used to be silent, and with `notify.notify`
    configured as the default it meant a trip alert for one person was
    broadcast to every phone in the house. The push still goes out —
    losing the alert entirely is worse — but the reason is now in the log
    so the underlying resolution failure can be fixed.
    """
    log.warning(
        f"Could not route a trip alert to its organizer ({reason}) — "
        f"falling back to {DEFAULT_NOTIFY_SERVICE}"
    )
    return DEFAULT_NOTIFY_SERVICE


def _set_notify_helper(service_name):
    """Publish the resolved notify service for the evcc plan automation.

    Written alongside the SOC and deadline helpers so the automation can
    send its charge-limit notification to whoever booked the trip, rather
    than to a hardcoded target. Reset to DEFAULT_NOTIFY_SERVICE when no
    trip is upcoming, so a stale organizer never outlives their trip.

    A missing helper is a warning, not a failure. The automation carries
    its own fallback, and refusing to publish a perfectly good trip plan
    because an optional input_text was never created would be a worse
    outcome than an unrouted notification.
    """
    if not service_name:
        return
    try:
        current = state.get(TARGET_NOTIFY_HELPER)
    except NameError:
        log.warning(
            f"{TARGET_NOTIFY_HELPER} does not exist — create it as a Text "
            f"helper, or the evcc automation falls back to its own default "
            f"notify target"
        )
        return
    if current == service_name:
        return  # don't churn the recorder every 5 minutes
    input_text.set_value(entity_id=TARGET_NOTIFY_HELPER, value=service_name)


def _notify_service_for(organizer_email):
    """Resolve an organizer's email to their notify.* service, by building
    an email -> notify_service reverse lookup from the shared household
    file at call time.

    Falls back to DEFAULT_NOTIFY_SERVICE for unrecognized or missing
    organizers (e.g. external invite senders — there's no HA device to
    push to for someone outside the household), but never silently: see
    _fallback_notify(). A fallback here is the symptom of a UID or
    household-map problem, not a normal outcome, and it used to be the
    route by which everyone got notified about one person's trip.
    """
    if not organizer_email:
        return _fallback_notify("no organizer on file for this event")
    household_map = json_store.load_json_map(HOUSEHOLD_MAP_PATH, warn=log.warning)
    for record in household_map.values():
        if record.get("email") == organizer_email:
            service_name = record.get("notify_service")
            if service_name:
                return service_name
            return _fallback_notify(
                f"{organizer_email} has no notify_service in "
                f"{HOUSEHOLD_MAP_PATH}"
            )
    return _fallback_notify(
        f"{organizer_email} is not in {HOUSEHOLD_MAP_PATH}"
    )


def _maybe_alert(alerted_map, alert_key, failure_type, title, message, notify_service):
    """Fire alert_failure() only if this exact (occurrence, failure_type)
    combination hasn't already been alerted on — a persistently-failing
    occurrence would otherwise re-notify every 5 minutes. A different
    failure_type, or a different occurrence of the same series, still
    alerts."""
    if not alert_key:
        alert_failure(title, message, notify_service, "unknown")
        return

    if alerted_map.get(alert_key) == failure_type:
        log.info(f"Already alerted for {alert_key} ({failure_type}) — not repeating")
        return

    alert_failure(title, message, notify_service, alert_key)
    alerted_map[alert_key] = failure_type
    json_store.save_json_map(ALERTED_PATH, alerted_map)


def alert_failure(title, message, notify_service=DEFAULT_NOTIFY_SERVICE, event_uid="unknown"):
    """Surface a trip-energy calculation failure so it's never silent.

    Fires a persistent notification (HA's bell icon) and a mobile push to
    the organizer of the affected event. The notification_id includes the
    UID — deriving it from the title alone meant two different events
    failing the same way overwrote each other's notification. `event_uid`
    is now the occurrence key, so two occurrences of one series don't
    overwrite each other either.
    """
    # A bare generator expression here (join(... for ... in ...) without
    # brackets) raises NotImplementedError: not implemented ast
    # ast_generatorexp — pyscript's AST interpreter implements list/set/
    # dict comprehensions but not generator expressions specifically.
    # Wrapping it as a list comprehension avoids the gap.
    slug = "".join([ch if ch.isalnum() else "_" for ch in str(event_uid)])[-40:]
    persistent_notification.create(
        title=title,
        message=message,
        notification_id=f"tesla_trip_energy_{slug}",
    )
    domain, _, svc = notify_service.partition(".")
    try:
        service.call(domain, svc, title=title, message=message)
    except Exception as e:
        log.warning(f"Push via {notify_service} failed: {e}")
