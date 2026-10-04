"""
Tesla calendar watcher (pyscript app)
--------------------------------------
Watches a dedicated mailbox (e.g. tesla@example.com) over IMAP for calendar
invitations (iMIP: METHOD=REQUEST / CANCEL) and mirrors them into an .ics
file that Home Assistant's Remote Calendar integration reads.

RSVPs "accepted" back to the organizer (METHOD:REPLY, PARTSTAT=ACCEPTED)
so the car actually shows as having accepted rather than sitting on "no
response" — the "auto accept invitations" requirement. Deliberately
gated: an event only gets accepted once it has a LOCATION that
tesla_trip_energy.py has successfully geocoded (read from that app's own
geocode cache — this file never calls Nominatim itself). Accepting an
event with no usable location, or one that hasn't geocoded yet, would
show the car as committed to a trip with no real charging plan behind
it. See _run_accept_replies() for the full reasoning and the
lookahead_days caveat that comes with it.

If an incoming event has no LOCATION set, sends a threaded reply email
to the organizer asking them to add one (the charging automation needs
LOCATION to calculate trip energy/SOC requirements) — independent of and
in addition to the accept-gating above, not a substitute for it.

Also exposes manual trip scheduling for the "Schedule a Trip" dashboard
card: schedule/cancel/reschedule a trip directly from HA, with a proper
outbound iMIP invite email so the organizer can manage it from their own
calendar app too.

RECURRING INVITES (1 Oct). A recurring invite arrives as one UID spread
over several VEVENTs: a master with RRULE plus overrides carrying
RECURRENCE-ID (a moved or edited instance). _handle_message() now groups
each calendar part's VEVENTs by UID and applies them as a set:

  REQUEST with the master  -> the whole series is replaced
                              (ics_store.replace_series). It used to be
                              upserted component by component on UID
                              alone, so an override overwrote the master
                              and the series vanished.
  REQUEST, overrides only  -> each override is added beside the master.
  CANCEL with the master   -> the whole series is removed.
  CANCEL, RECURRENCE-ID    -> only that instance goes: its override is
                              removed and an EXDATE is added to the
                              master. It used to remove the whole series.

Accept RSVPs and the missing-location reply are per series, sent for the
master. Retention pruning judges a series by its last occurrence (see
ics_store.prune_past_events). Recurring MANUAL trips are not supported
yet; this covers inbound invites only.

Generic plumbing (file I/O, ICS calendar store, JSON map persistence,
outbound email building) lives in shared pyscript MODULES rather than in
this file, since pyscript gives every file its own separate global
context — even sibling files in the same app can't call each other's
functions without an explicit import. The modules/ folder is the one
place pyscript allows real cross-file imports. This file keeps only the
actual @service/@time_trigger entry points and app-specific glue.

THREADING. Pyscript runs @service / @time_trigger functions inside Home
Assistant's asyncio event loop. Every blocking network call therefore
has to be pushed into a worker thread with @pyscript_executor or it
stalls the whole HA instance for its duration. All the IMAP traffic
lives in _fetch_unprocessed() / _mark_processed() below for that reason;
SMTP is in tesla_outbound_email, file I/O is in tesla_file_io.

Requires (add to /config/pyscript/requirements.txt):
    icalendar
    requests

Requires these files in /config/pyscript/modules/ (NOT under apps/):
    tesla_file_io.py
    tesla_ics_store.py
    tesla_json_store.py
    tesla_outbound_email.py
    tesla_geocode.py

Requires in configuration.yaml:
    pyscript:
      apps:
        tesla_calendar:
          imap_host: mail.example.com
          imap_user: tesla@example.com
          imap_pass: !secret tesla_mailbox_password
          smtp_host: mail.example.com
          smtp_port: 587
          smtp_user: tesla@example.com   # MUST be the same mailbox as imap_user
          smtp_pass: !secret tesla_mailbox_password
          nominatim_user_agent: "my-ha-ev-scheduler/1.0 (contact: you@example.com)"
          # ^ REQUIRED — same value as tesla_trip_energy's own
          # nominatim_user_agent. This app geocodes pending events'
          # LOCATION itself now (to gate accept-RSVPs — see
          # _run_accept_replies()), through the shared tesla_geocode
          # module and cache, so it needs the same identifying string
          # Nominatim's usage policy asks every client to send.
          # --- optional, defaults shown ---
          send_accept_replies: true    # RSVP accepted to inbound invitations
          manual_trip_duration_min: 60 # DTEND offset for manual trips
          retention_days: 30           # drop events this long past their end

SENDER IDENTITY. Outbound invites always go out From: tesla@, with
tesla@ as the VEVENT's ORGANIZER and the household member as an
ATTENDEE. RFC 6047 requires the sender to match the ORGANIZER on
REQUEST/CANCEL, so making tesla@ the organizer is what lets From: stay
tesla@ and keeps DMARC aligned — Gmail enforces alignment, and an
unaligned invite gets filed as spam or rejected.

The earlier design did the opposite: the household member was the
ORGANIZER and a `send_as_organizer` option decided whether From:
impersonated them. That needed each household address on the sending
mailbox's "allowed to send as" list in mailcow, and still failed DMARC
for external recipients. The option is gone and this file no longer
reads the config key at all — it defaulted to true, so a stale copy of
this file would silently reintroduce the regression rather than fail
loudly. A leftover `send_as_organizer:` entry in config.yaml is inert.

The trade-off is that the member is an attendee on their own trip and
gets no organizer edit rights in their own calendar client. Reschedule
and cancel go through the HA dashboard, which is authoritative, so
nothing is lost.

The household user -> {email, notify_service} mapping lives in its own
file, NOT in this config block — see HOUSEHOLD_MAP_PATH below.

This file must live at /config/pyscript/apps/tesla_calendar.py — the
filename (minus .py) must match the app name used in configuration.yaml.
"""
import imaplib
import email
import time
import uuid
import datetime
from icalendar import Calendar, Event, vCalAddress, vText
import homeassistant.util.dt as dt_util
import tesla_ics_store as ics_store
import tesla_json_store as json_store
import tesla_outbound_email as outbound_email
import tesla_geocode as geocode_mod

IMAP_HOST = pyscript.app_config["imap_host"]
IMAP_USER = pyscript.app_config["imap_user"]
IMAP_PASS = pyscript.app_config["imap_pass"]
ICS_PATH = "/config/www/tesla.ics"       # served by HA at /local/tesla.ics
CALENDAR_ENTITY = "calendar.tesla"       # entity id of the Remote Calendar

# Required so this app can geocode a pending event's LOCATION itself (via
# the shared tesla_geocode module) rather than only passively waiting on
# tesla_trip_energy.py's own cluster-scoped geocoding to reach it — see
# GEOCODE_CACHE_PATH and _run_accept_replies() below. MUST be set to the
# same value as tesla_trip_energy's nominatim_user_agent — Nominatim's
# usage policy identifies clients by this string, and it's the same
# underlying app making the same kind of request either way.
NOMINATIM_UA = pyscript.app_config["nominatim_user_agent"]

SMTP_HOST = pyscript.app_config["smtp_host"]
SMTP_PORT = int(pyscript.app_config.get("smtp_port", 587))
SMTP_USER = pyscript.app_config["smtp_user"]
SMTP_PASS = pyscript.app_config["smtp_pass"]
SMTP_FROM_NAME = "Tesla Calendar"

# NOTE: `send_as_organizer` is deliberately NOT read here any more. It
# defaulted to True, which meant a stale copy of this file silently sends
# From: the household member again — exactly the DMARC regression the role
# flip fixed. Leaving the key unread makes a leftover entry in
# config.yaml inert rather than quietly harmful.
SEND_ACCEPT_REPLIES = bool(pyscript.app_config.get("send_accept_replies", True))
MANUAL_TRIP_DURATION_MIN = int(
    pyscript.app_config.get("manual_trip_duration_min", 60)
)
RETENTION_DAYS = int(pyscript.app_config.get("retention_days", 30))

# HA user ID -> {email, notify_service}, hand-maintained, one record per
# household member. Read by BOTH apps: this one looks it up by user_id
# (schedule_manual_trip resolves context.user_id -> email),
# tesla_trip_energy.py builds a reverse email -> notify_service index
# from the same file. MUST match HOUSEHOLD_MAP_PATH there.
#
# Lives in its own JSON file rather than pyscript app config so it can be
# edited without touching configuration.yaml or triggering a pyscript
# config reload. Not web-served. Loaded fresh on every call.
#
# Find a user's ID under Settings > People > Users > (click the user) —
# it's in the page URL, /config/users/<user_id>. Find a notify service
# name under Settings > Devices & Services > Mobile App. There is no
# native HA way to derive the latter from the former.
HOUSEHOLD_MAP_PATH = "/config/pyscript/tesla_household.json"

# Not web-served (unlike ICS_PATH under /config/www) — internal lookup
# data mapping event UID -> organizer email, so tesla_trip_energy.py can
# notify the actual organizer rather than always defaulting to one person.
ORGANIZER_MAP_PATH = "/config/pyscript/tesla_organizer_map.json"

# UID -> Message-ID of the most recent outbound confirmation/update email
# for a manually-scheduled trip, so a later reschedule/cancel email
# threads onto the earlier one instead of arriving as an unrelated message.
SENT_INVITE_MSGID_PATH = "/config/pyscript/tesla_sent_invite_msgids.json"

# Occurrence key -> last-alerted failure type, written by
# tesla_trip_energy.py to avoid repeat-alerting on the same persistent
# geocode/route failure. Keyed "<uid>|occ=<start ts>", so it is always
# cleared with json_store.pop_uid(), never with a plain pop(uid).
# Must match ALERTED_PATH in tesla_trip_energy.py.
ALERTED_PATH = "/config/pyscript/tesla_trip_energy_alerted.json"

# UID -> SEQUENCE we last RSVP'd "accepted" for, so a reschedule gets a
# fresh accept but a duplicate delivery of the same invite doesn't.
ACCEPTED_PATH = "/config/pyscript/tesla_accepted.json"

# location string (lowercased) -> {"lat", "lon", "ts"} on a successful
# geocode, or {"failed": True, "ts"} on a failure. SHARED with
# tesla_trip_energy.py via the tesla_geocode module — both apps read AND
# write this file now (through geocode_mod.geocode_cached(), never
# directly), so a location resolved by either one is immediately
# available to the other. This app actively geocodes a pending event's
# LOCATION here, rather than only waiting on tesla_trip_energy.py's own
# cluster-scoped geocoding to reach it eventually — see
# _run_accept_replies() for why. MUST match GEOCODE_CACHE_PATH in
# tesla_trip_energy.py.
GEOCODE_CACHE_PATH = "/config/pyscript/tesla_geocode_cache.json"

# Message-ID -> consecutive failure count. A message that raises while
# being processed is deliberately NOT flagged TeslaProcessed, so it gets
# retried on the next poll — but a permanently malformed one would then
# be retried forever, so after MAX_MESSAGE_ATTEMPTS we give up, flag it,
# and raise a notification rather than looping silently.
FAILED_PATH = "/config/pyscript/tesla_failed_messages.json"
MAX_MESSAGE_ATTEMPTS = 3

# Only these two iTIP methods touch the calendar. Everything else
# (REPLY, PUBLISH, COUNTER, REFRESH, DECLINECOUNTER) is ignored.
#
# This used to be an `if CANCEL / else upsert`, which meant a METHOD:REPLY
# — whose VEVENT is a deliberately minimal stub carrying the same UID but
# typically no LOCATION or SUMMARY — overwrote the real event in place.
# The location vanished, trip energy stopped calculating, and the script
# then emailed the organizer asking them to add a location to an event
# that already had one. Now that we send outbound invites, replies
# genuinely do arrive here.
HANDLED_METHODS = ("REQUEST", "CANCEL")

MANUAL_TRIP_SELECT = "input_select.manual_trip_to_cancel"
MANUAL_TRIP_OPTIONS_SENSOR = "sensor.manual_trip_options"
# Cleared here rather than in the dashboard script. "Clear the form only
# when the trip was actually accepted" is a rule about the outcome of
# scheduling, so it belongs next to the code that decides the outcome —
# the YAML version had to re-derive the validation to know when to clear,
# and the two copies could disagree.
MANUAL_TRIP_LOCATION = "input_text.manual_trip_location"
MANUAL_TRIP_ONE_WAY = "input_boolean.manual_trip_one_way"
NO_TRIPS_OPTION = "(none)"

# Destination picker. The location field is a SEARCH BOX now, not the
# destination itself: it feeds search_destination(), whose results land in
# MANUAL_TRIP_DEST_SELECT for the user to choose from. The chosen result's
# coordinates ride along to schedule_manual_trip() as `geo` and get pinned
# onto the event, so nothing downstream re-guesses which branch of a chain
# was meant.
#
# Same split as MANUAL_TRIP_SELECT / MANUAL_TRIP_OPTIONS_SENSOR above, for
# the same reason: input_select options are plain strings with nowhere to
# carry a lat/lon, so the label -> coordinate mapping goes on a companion
# sensor's attributes.
MANUAL_TRIP_DATETIME = "input_datetime.manual_trip_datetime"
MANUAL_TRIP_DEST_SELECT = "input_select.manual_trip_destination"
MANUAL_TRIP_DEST_SENSOR = "sensor.manual_trip_destination_results"
NO_SEARCH_OPTION = "(search first)"
MAX_DESTINATION_RESULTS = 6

# Form feedback surface. Written by BOTH apps (tesla_trip_energy.py sets
# it from alert_failure()), read by the dashboard's markdown banner, which
# renders it as a coloured <ha-alert> directly above the trip form.
#
# WHY: every failure path here used to be persistent-notification-only.
# A persistent notification lives behind the bell icon in the sidebar,
# collapsed by default — someone who taps Submit, sees the fields clear,
# and walks away never learns the trip was refused. The notification is
# still raised (it's the right place for something you want to find
# later, and it's what pushes to a phone), but the form now says so too,
# in red, where the person who caused the error is actually looking.
#
# State is one of: ok | success | warning | error. Those last three are
# exactly ha-alert's alert-type values, so the markdown card can pass the
# state straight through without a mapping table.
FORM_STATUS_SENSOR = "sensor.tesla_trip_form_status"

# Which `source` values this app owns. _clear_form_status() only clears a
# status it put there itself — otherwise tesla_trip_energy.py's next
# successful 5-minute run would silently wipe a "location not found"
# error the user hasn't read yet, and vice versa.
FORM_STATUS_SOURCES = (
    "schedule", "cancel", "reschedule", "email", "selection", "search",
)


# --------------------------------------------------------------------
# IMAP — everything here runs in a worker thread, off the event loop.
# No pyscript globals (log, state, service) are available inside these.
# --------------------------------------------------------------------

@pyscript_executor
def _fetch_unprocessed(host, user, password):
    """Return [(msg_num, raw_rfc822_bytes), ...] for messages not yet
    flagged TeslaProcessed."""
    messages = []
    imap = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        imap.login(user, password)
        imap.select("INBOX")
        status, data = imap.search(None, "UNKEYWORD", "TeslaProcessed")
        if status == "OK" and data and data[0]:
            for num in data[0].split():
                status, msg_data = imap.fetch(num, "(RFC822)")
                if status == "OK" and msg_data and msg_data[0]:
                    messages.append((num, msg_data[0][1]))
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return messages


@pyscript_executor
def _mark_processed(host, user, password, msg_nums):
    """Flag messages with our private TeslaProcessed keyword.

    Deliberately not \\Seen — simply opening a message in SOGo's webmail
    marks it read, which would silently skip it.
    """
    if not msg_nums:
        return 0
    imap = imaplib.IMAP4_SSL(host, timeout=30)
    try:
        imap.login(user, password)
        imap.select("INBOX")
        for num in msg_nums:
            imap.store(num, "+FLAGS", "TeslaProcessed")
    finally:
        try:
            imap.logout()
        except Exception:
            pass
    return len(msg_nums)


# --------------------------------------------------------------------
# Mailbox polling
# --------------------------------------------------------------------

@time_trigger("cron(*/5 * * * *)")
@service
def check_tesla_invites():
    """Poll the mailbox every 5 minutes for new invitations."""
    log.info(f"Checking {IMAP_USER} for calendar invitations")

    try:
        messages = _fetch_unprocessed(IMAP_HOST, IMAP_USER, IMAP_PASS)
    except Exception as e:
        log.warning(f"IMAP fetch failed, will retry next cycle: {e}")
        return

    cal = ics_store.load_or_create_calendar(ICS_PATH)

    changed = False
    dirty = set()
    to_flag = []

    # Loaded unconditionally, not gated behind "if messages" — retention
    # pruning and the accept-RSVP retry scan below both need to run on
    # EVERY poll, including ones where no new mail arrived, so all four
    # need to be in memory regardless. These are small JSON files; the
    # extra I/O on a quiet poll is negligible next to the correctness
    # this buys (an earlier version gated some of these behind "if
    # messages" and could KeyError during retention pruning on a
    # no-new-mail cycle).
    maps = {
        "organizer": json_store.load_json_map(ORGANIZER_MAP_PATH, warn=log.warning),
        "alerted": json_store.load_json_map(ALERTED_PATH, warn=log.warning),
        "accepted": json_store.load_json_map(ACCEPTED_PATH, warn=log.warning),
        "failed": json_store.load_json_map(FAILED_PATH, warn=log.warning),
    }

    if messages:
        for num, raw in messages:
            msg_id = None
            try:
                msg = email.message_from_bytes(raw)
                msg_id = msg.get("Message-ID") or f"nomsgid-{num}"
                if _handle_message(msg, cal, maps, dirty):
                    changed = True
            except Exception as e:
                attempts = int(maps["failed"].get(msg_id, 0)) + 1 if msg_id else MAX_MESSAGE_ATTEMPTS
                if msg_id:
                    maps["failed"][msg_id] = attempts
                    dirty.add("failed")
                if attempts >= MAX_MESSAGE_ATTEMPTS:
                    log.error(
                        f"Giving up on message {msg_id} after {attempts} "
                        f"attempts: {e}"
                    )
                    _notify_user(
                        "Tesla calendar: unreadable invitation",
                        "An email in the tesla@ mailbox could not be processed "
                        "and has been skipped. Check the Home Assistant log.",
                        "tesla_calendar_bad_message",
                    )
                    to_flag.append(num)
                else:
                    log.warning(
                        f"Message {msg_id} failed ({attempts}/{MAX_MESSAGE_ATTEMPTS}), "
                        f"leaving it unflagged for retry: {e}"
                    )
                continue

            # Only flag once the message parsed cleanly. Flagging happens
            # after save_calendar() below, so a crash between here and the
            # save leaves the message eligible for a retry instead of
            # losing the invite permanently.
            to_flag.append(num)
            if msg_id and maps["failed"].pop(msg_id, None) is not None:
                dirty.add("failed")

    # Retention: nothing used to remove past events, so tesla.ics grew
    # forever and was fully re-parsed and re-serialised every 5 minutes.
    # A recurring series is judged by its LAST occurrence (and an
    # unbounded one is never pruned) — see ics_store.prune_past_events().
    cutoff = dt_util.now() - datetime.timedelta(days=RETENTION_DAYS)
    pruned = ics_store.prune_past_events(cal, cutoff)
    if pruned:
        changed = True
        log.info(f"Pruned {pruned} event(s) older than {RETENTION_DAYS} days")

    # Always write the file, even with no changes, so it exists from the
    # very first run instead of only appearing once an invite arrives
    # (Remote Calendar's config flow validates that the file exists).
    ics_store.save_calendar(cal, ICS_PATH)

    if to_flag:
        try:
            _mark_processed(IMAP_HOST, IMAP_USER, IMAP_PASS, to_flag)
        except Exception as e:
            # The events are already saved; worst case is a duplicate
            # upsert next cycle, which is idempotent.
            log.warning(f"Could not flag {len(to_flag)} message(s) as processed: {e}")

    if pruned:
        # prune_to_keys() matches on the UID part of each key, so the
        # per-occurrence alert keys survive as long as their series does.
        live = ics_store.all_uids(cal)
        for key, path in (
            ("organizer", ORGANIZER_MAP_PATH),
            ("alerted", ALERTED_PATH),
            ("accepted", ACCEPTED_PATH),
        ):
            if json_store.prune_to_keys(maps[key], live):
                dirty.add(key)

    for key, path in (
        ("organizer", ORGANIZER_MAP_PATH),
        ("alerted", ALERTED_PATH),
        ("accepted", ACCEPTED_PATH),
        ("failed", FAILED_PATH),
    ):
        if key in dirty:
            json_store.save_json_map(path, maps[key])

    # Accept-RSVP retry scan — runs every poll, regardless of whether any
    # new mail arrived, and only marks (uid, sequence) accepted after a
    # confirmed send. Actively geocodes each pending event's LOCATION
    # itself (via the shared tesla_geocode module) rather than waiting on
    # tesla_trip_energy.py's own cluster-scoped geocoding to reach it —
    # see _run_accept_replies() for why, and for why this replaced the
    # old message-triggered queue.
    home = state.getattr("zone.home")
    home_coords = (
        (home["latitude"], home["longitude"])
        if home and "latitude" in home else None
    )
    if home_coords is None:
        log.warning("zone.home unavailable — skipping accept-RSVP geocode this poll")

    geocode_cache = json_store.load_json_map(GEOCODE_CACHE_PATH, warn=log.warning)
    accepted_dirty, geocode_dirty = _run_accept_replies(
        cal, maps["accepted"], geocode_cache, home_coords
    )
    if accepted_dirty:
        json_store.save_json_map(ACCEPTED_PATH, maps["accepted"])
    if geocode_dirty:
        json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)

    refresh_manual_trip_list(cal)

    if changed:
        homeassistant.update_entity(entity_id=CALENDAR_ENTITY)
        log.info("tesla.ics updated, refreshed Remote Calendar entity")


def _handle_message(msg, cal, maps, dirty):
    """Apply one email's calendar parts to `cal`. Returns True if the
    calendar changed. Raises on anything unexpected — the caller decides
    whether to retry or give up.

    Each calendar part's VEVENTs are grouped by UID and applied as a set,
    because a recurring invite is one UID over several VEVENTs (master
    plus RECURRENCE-ID overrides). See the module docstring for what each
    method does with a set.
    """
    changed = False
    seen = set()

    for part in msg.walk():
        ctype = part.get_content_type()
        filename = (part.get_filename() or "").lower()
        if ctype not in ("text/calendar", "application/ics") and not filename.endswith(".ics"):
            continue

        payload = part.get_payload(decode=True)
        if not payload:
            continue

        try:
            invite = Calendar.from_ical(payload)
        except Exception as e:
            log.warning(f"Could not parse calendar part ({ctype}): {e}")
            continue

        method = str(invite.get("METHOD", "REQUEST")).upper()
        if method not in HANDLED_METHODS:
            log.info(f"Ignoring METHOD:{method} — only REQUEST/CANCEL are applied")
            continue

        groups = {}
        order = []
        for component in invite.walk("VEVENT"):
            uid = str(component.get("UID"))
            if uid not in groups:
                groups[uid] = []
                order.append(uid)
            groups[uid].append(component)

        for uid in order:
            components = groups[uid]

            # A multipart/alternative message often carries the same
            # calendar object twice (inline plus attachment), which used
            # to log "Added/updated event" twice for one invite.
            if (method, uid) in seen:
                continue
            seen.add((method, uid))

            masters = [c for c in components if ics_store.recurrence_key(c) is None]
            master = masters[0] if masters else None

            if method == "CANCEL":
                if master is not None:
                    # The whole event, or the whole series.
                    if ics_store.remove_event(cal, uid):
                        changed = True
                        log.info(f"Cancelled event {uid}")
                    for key in ("organizer", "alerted", "accepted"):
                        if json_store.pop_uid(maps[key], uid):
                            dirty.add(key)
                    continue
                # One or more single instances of a series. The series and
                # its side-car entries stay; a stale per-occurrence alert
                # key is harmless and goes when the series does.
                for c in components:
                    if ics_store.cancel_occurrence(cal, uid, c["RECURRENCE-ID"]):
                        changed = True
                        log.info(
                            f"Cancelled one occurrence of {uid} "
                            f"({c['RECURRENCE-ID'].dt})"
                        )
                continue

            # METHOD:REQUEST
            if ics_store.replace_series(cal, components):
                changed = True
                overrides = len(components) - len(masters)
                extra = f" with {overrides} changed occurrence(s)" if overrides else ""
                kind = "series" if master is not None and ics_store.is_recurring(master) else "event"
                if master is None:
                    kind = "occurrence(s)"
                log.info(f"Added/updated {kind} {uid}{extra}")

            # Organizer, missing-location reply: per series, from the
            # master. An invite to a single instance of someone else's
            # series has no master, so its first override stands in.
            primary = master if master is not None else components[0]
            organizer_email = ics_store.get_organizer_email(primary)
            if organizer_email and maps["organizer"].get(uid) != organizer_email:
                maps["organizer"][uid] = organizer_email
                dirty.add("organizer")

            if not primary.get("LOCATION"):
                if organizer_email:
                    ok = outbound_email.send_missing_location_reply(
                        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME,
                        organizer_email,
                        msg,
                        str(primary.get("SUMMARY", "the event")),
                        _start_text(primary),
                    )
                    if not ok:
                        log.warning(
                            f"Missing-location reply to {organizer_email} "
                            f"could not be sent"
                        )
                else:
                    log.warning(
                        f"Event {uid} missing LOCATION and has no ORGANIZER "
                        "— can't send reply"
                    )

            # RSVP acceptance is handled separately by _run_accept_replies(),
            # which scans the saved calendar after this loop rather than
            # queuing from here. Queuing from here meant the "accepted"
            # marker for (uid, sequence) was written to disk before the
            # send was even attempted — so a crash, an SMTP rejection, or
            # any other failure still left the marker saying "done", and
            # since retry was tied to re-reading this exact message (which
            # never happens again once it's flagged TeslaProcessed), a
            # failed accept could never be retried. Scanning the calendar
            # itself decouples "have I accepted this UID/SEQUENCE" from
            # "did I just re-parse this exact email".

    return changed


def _send_accept_reply(organizer_email, component, source_msg):
    """Returns True on a confirmed successful send, False otherwise.
    The caller decides whether it's now safe to mark this
    (uid, sequence) as accepted based on this return value — see
    _run_accept_replies()."""
    ok = outbound_email.send_accept_reply(
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME,
        IMAP_USER, organizer_email, component, source_msg,
    )
    if not ok:
        log.warning(
            f"Could not RSVP accepted to {organizer_email} — if this is a "
            f"sender-check rejection, add {IMAP_USER} to the {SMTP_USER} "
            f"mailbox's 'allowed to send as' list, or set "
            f"send_accept_replies: false"
        )
    return ok


def _run_accept_replies(cal, accepted_map, geocode_cache, home_coords):
    """Send an accept RSVP for every VEVENT in `cal` whose ORGANIZER
    isn't tesla@ itself, whose LOCATION geocodes successfully, and whose
    current SEQUENCE hasn't been successfully accepted yet. Mutates
    `accepted_map` and/or `geocode_cache` in place, only for outcomes
    that actually happened (a confirmed send; a genuine geocode
    attempt). Returns (accepted_dirty, geocode_dirty) so the caller knows
    which of the two to save.

    ONE RSVP PER SERIES. A recurring series is accepted once, for its
    master. Overrides are skipped whenever their master is present: they
    share the UID, so accepting each would flip accepted_map between the
    master's SEQUENCE and the override's on every poll and re-send
    forever. An override with no master (an invite to a single instance
    of someone else's series) is accepted on its own.

    GATED ON GEOCODING, DELIBERATELY. An event with no LOCATION, or one
    that fails to geocode, is skipped entirely: no accept is sent for
    it. Auto-accepting an event the system can't calculate a charge
    requirement for would show the car as committed to a trip with no
    real charging plan behind it. The missing-LOCATION courtesy reply
    (see _handle_message) still goes out regardless — that's a separate,
    independent notification to the organizer, not a substitute for
    acceptance.

    ACTIVELY GEOCODES HERE, not just checks a cache someone else filled.
    tesla_trip_energy.py only geocodes located events within its own
    trip_cluster_hours window of the NEAREST upcoming trip — the right
    scope for charge planning, but it means a far-future invite would
    otherwise sit un-accepted for weeks, only getting geocoded (and
    therefore accepted) once it got close. This function calls
    geocode_mod.geocode_cached() directly instead, for every pending
    located event regardless of how far out it is — through the SAME
    shared cache tesla_trip_energy.py uses, so whichever app resolves a
    location first, the other gets a free cache hit rather than a
    second Nominatim call. The one-time cost is per genuinely NEW
    location, not a recurring one: once cached, every later poll (by
    either app) just reads it.

    Runs as a scan over the CURRENT calendar rather than being triggered
    from message-parsing, so a send that failed on an earlier poll (no
    mailcow "allowed to send as" yet, a transient SMTP error) is
    automatically retried on every subsequent poll — including ones
    where no new mail arrived at all — without needing the original
    invite email to be re-read (it never is, once flagged
    TeslaProcessed). `source_msg` is passed as None here: it's only used
    to thread the reply onto the original invite via In-Reply-To, which
    is optional — the iMIP REPLY itself is fully valid without it, since
    calendar clients match REPLYs by UID/SEQUENCE, not by email threading.
    """
    if not SEND_ACCEPT_REPLIES:
        return False, False

    if home_coords is None:
        # zone.home unavailable this run (see check_tesla_invites) — no
        # coordinates to geocode against. Try again next poll.
        return False, False

    accepted_dirty = False
    geocode_dirty = False
    now_ts = time.time()

    master_uids = set([
        str(c.get("UID")) for c in cal.subcomponents
        if c.name == "VEVENT" and ics_store.recurrence_key(c) is None
    ])

    for component in cal.subcomponents:
        if component.name != "VEVENT":
            continue

        uid = str(component.get("UID"))
        if ics_store.recurrence_key(component) is not None and uid in master_uids:
            continue  # the series is accepted via its master

        organizer_email = ics_store.get_organizer_email(component)
        if not organizer_email or organizer_email.lower() == IMAP_USER.lower():
            continue  # no organizer, or an event we created ourselves

        location = str(component.get("LOCATION", ""))
        if not location:
            continue  # nothing to geocode; missing-location reply covers this

        coords, cache_hit = geocode_mod.geocode_cached(
            location, home_coords, geocode_cache, now_ts, NOMINATIM_UA
        )
        if not cache_hit:
            geocode_dirty = True
        if coords is None:
            continue  # not geocodable (yet, or possibly ever) — no accept

        seq = int(component.get("SEQUENCE", 0))
        if accepted_map.get(uid) == seq:
            continue  # already accepted at this sequence

        if _send_accept_reply(organizer_email, component, None):
            accepted_map[uid] = seq
            accepted_dirty = True

    return accepted_dirty, geocode_dirty


def _start_text(component):
    """Human-readable DTSTART, tolerating a missing property (legal in
    some iTIP messages, and the old code raised AttributeError on it)."""
    dtstart = component.get("DTSTART")
    if dtstart is None:
        return "an unspecified time"
    return str(dtstart.dt)


# --------------------------------------------------------------------
# Manual trip scheduling
# --------------------------------------------------------------------

def _parse_local(value):
    """Parse an ISO datetime string as LOCAL wall time.

    dt_util.as_local() assumes a NAIVE datetime is UTC, so passing it an
    input_datetime state (which is naive local wall time) silently shifted
    every manual trip by the UTC offset — a 14:00 trip became 16:00 in
    summer. Attach the local zone instead.
    """
    parsed = dt_util.parse_datetime(value)
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
    return dt_util.as_local(parsed)


@service
def schedule_manual_trip(trip_datetime, location, organizer_name,
                         one_way=False, geo=None, display_name=None):
    """Add a manually-scheduled trip directly to tesla.ics, and email the
    organizer a proper iMIP invite so they can add it to their own
    calendar.

    Bypasses the mailbox/iMIP *inbound* pipeline for creation, but
    structures the event exactly like a real invite (ORGANIZER =
    organizer's email, ATTENDEE = tesla@, plus DTSTAMP/DTEND/SEQUENCE) so
    any reschedule or cancellation the organizer later makes in their own
    calendar app flows back through check_tesla_invites() the normal way.

    trip_datetime: ISO datetime string (e.g. from an input_datetime state),
        interpreted as local wall time.
    location: free-text location, geocoded downstream same as any invite.
    organizer_name: an HA user ID (the dashboard's submit script passes
        context.user_id automatically) — resolved to an email via the
        file at HOUSEHOLD_MAP_PATH.
    one_way: if True, tags the event so tesla_trip_energy.py skips the
        return leg when calculating energy needed.
    geo: optional "lat,lon" for the destination, as chosen by the user in
        the dashboard's destination picker. When present the address
        lookup below is skipped entirely and the coordinates are pinned
        onto the event as GEO=, so the trip routes to the place that was
        actually picked. Optional rather than required so the service
        stays callable without a picker (Developer Tools, an automation,
        or a future voice intent) — those callers get the old
        geocode-at-submit behaviour.
    display_name: optional short form of `location` for anything a person
        reads — SUMMARY, the email subject, the confirmation banner.
        LOCATION keeps the full string regardless, since that is what a
        calendar client geolocates from. Defaults to `location` when not
        given, so a caller without a picker behind it behaves as before.
    """
    parsed = _parse_local(trip_datetime)
    if parsed is None:
        _reject(f"Could not read the date/time '{trip_datetime}'.")
        return

    if parsed <= dt_util.now():
        _reject(
            f"The trip time {parsed.strftime('%Y-%m-%d %H:%M')} is in the past. "
            f"A past trip would be invisible to the scheduler and impossible "
            f"to cancel from the dashboard."
        )
        return

    if not location or not str(location).strip():
        _reject(
            "Enter a location before submitting — the charging automation "
            "needs one to work out how much charge the trip needs."
        )
        return
    location = str(location).strip()
    display_name = str(display_name).strip() if display_name else ""
    if not display_name:
        display_name = location

    # Validate rather than trust. The dashboard only ever sends a `geo`
    # that came out of search_destination(), but this is a service: it is
    # callable from Developer Tools, an automation or a template with
    # anything at all in that field. A malformed value falls back to
    # geocoding the text instead of poisoning the event with a GEO= line
    # that tesla_trip_energy.py would then route against.
    pinned = None
    if geo:
        try:
            lat_s, lon_s = str(geo).split(",", 1)
            lat_f = float(lat_s)
            lon_f = float(lon_s)
            if -90 <= lat_f <= 90 and -180 <= lon_f <= 180:
                pinned = f"{lat_f:.6f},{lon_f:.6f}"
        except (ValueError, TypeError):
            pinned = None
        if pinned is None:
            log.warning(
                f"schedule_manual_trip: ignoring unusable geo '{geo}' — "
                f"falling back to looking up '{location}'"
            )

    household_map = json_store.load_json_map(HOUSEHOLD_MAP_PATH, warn=log.warning)
    organizer_email = (household_map.get(organizer_name) or {}).get("email")
    if not organizer_email:
        _reject(
            f"User id '{organizer_name}' isn't in {HOUSEHOLD_MAP_PATH}, so "
            f"there's nobody to send the confirmation to. Add them to the "
            f"household map and try again."
        )
        return

    # Skipped entirely when the user picked a destination from the search
    # results: the coordinates are already resolved and confirmed by a
    # human, so a second lookup could only ever disagree with what they
    # saw on screen.
    if not pinned:
        # Geocode NOW, synchronously, instead of letting tesla_trip_energy.py
        # discover the failure on its next 5-minute pass.
        #
        # A bad address is by far the most likely reason a manual trip goes
        # wrong, and it's the one the person at the dashboard can actually fix
        # — but only if they're told while they're still standing there. The
        # cost is one Nominatim call on the submit path (cached afterwards,
        # and trip_energy would have made the same call anyway), in exchange
        # for the error landing in the form instead of in a notification five
        # minutes later, long after the tab is closed.
        #
        # LAST of the checks, deliberately. Everything above rejects for free;
        # only a submission that is otherwise completely valid is worth
        # spending a Nominatim call on. Reordering this any earlier means
        # every blank-organizer or past-datetime rejection burns a request
        # against Nominatim's usage policy for nothing.
        #
        # zone.home missing is NOT treated as a reason to refuse: geocoding
        # needs it only for the closest-to-home tiebreak, and refusing to
        # schedule a trip because a zone is briefly unavailable would be
        # worse than scheduling one that trip_energy retries later.
        home = state.getattr("zone.home")
        home_coords = (
            (home["latitude"], home["longitude"])
            if home and "latitude" in home else None
        )
        if home_coords is None:
            log.warning("zone.home unavailable — scheduling without a geocode check")
        else:
            geocode_cache = json_store.load_json_map(GEOCODE_CACHE_PATH, warn=log.warning)
            coords, cache_hit = geocode_mod.geocode_cached(
                location, home_coords, geocode_cache, time.time(), NOMINATIM_UA
            )
            if coords is None:
                # Drop the cached failure rather than persisting it.
                #
                # geocode_cached() caches a miss for FAILED_TTL_SECONDS, which
                # is right for the 5-minute poll loop — a bad address in a
                # calendar invite shouldn't re-hit Nominatim every cycle — and
                # wrong for an interactive submit. Someone who mistyped an
                # address will fix it and press Submit again within seconds;
                # someone who hit a momentary Nominatim outage will retry the
                # identical string. A cached miss would reject both without a
                # network call, for an hour, with no way to tell that the
                # service had come back.
                #
                # Saved unconditionally: the entry being dropped may have been
                # written by this call (cache_hit False) or already on disk
                # from a previous poll by tesla_trip_energy.py (cache_hit
                # True). Popping only the in-memory copy in the second case
                # would leave the stale miss on disk, which is the exact thing
                # this is here to prevent.
                geocode_cache.pop(location.strip().lower(), None)
                json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)
                _reject(
                    f"Couldn't find \"{location}\" on the map, so the trip wasn't "
                    f"scheduled. Try a fuller address — street and town, or a "
                    f"postcode."
                )
                return
            if not cache_hit:
                json_store.save_json_map(GEOCODE_CACHE_PATH, geocode_cache)

    end = parsed + datetime.timedelta(minutes=MANUAL_TRIP_DURATION_MIN)

    event = Event()
    event_uid = f"manual-{uuid.uuid4()}"
    event.add("uid", event_uid)
    event.add("summary", f"Trip to {display_name}")
    event.add("dtstart", parsed)
    # A VEVENT with no DTEND is zero-length. Beyond being poor iMIP, it
    # makes whether calendar.get_events returns it inside a window
    # implementation-dependent.
    event.add("dtend", end)
    event.add("location", location)
    event.add("sequence", 0)
    event.add("status", "CONFIRMED")
    # RFC 5545 requires DTSTAMP on every VEVENT in an iTIP message.
    ics_store.touch_dtstamp(event)

    # ROLE FLIP — tesla@ is the ORGANIZER, the household member is the
    # ATTENDEE. This reads backwards ("the member scheduled it, so they
    # are the organizer") and is deliberate:
    #
    # RFC 6047 requires From: to match the ORGANIZER on REQUEST/CANCEL.
    # Sending From: a household address while authenticated as tesla@
    # breaks DMARC alignment, which Gmail enforces — invites to Gmail
    # recipients get filed as spam or rejected outright. Making tesla@
    # the organizer lets From: always be tesla@, aligned, with no
    # per-recipient special-casing and no "allowed to send as" list to
    # maintain in mailcow.
    #
    # The cost is that the household member is an attendee on their own
    # trip and doesn't get organizer edit rights in their calendar client.
    # That's fine: reschedule and cancel go through this dashboard, which
    # is the authoritative side anyway. See T2.5 / T3.5 in Test_plan.md —
    # T3.5 (organizer edit rights) is marked obsolete for this reason.
    organizer = vCalAddress(f"MAILTO:{IMAP_USER}")
    organizer.params["CN"] = vText("Tesla")
    event.add("organizer", organizer, encode=0)

    attendee = vCalAddress(f"MAILTO:{organizer_email}")
    attendee.params["CN"] = vText(organizer_email.split("@")[0])
    attendee.params["ROLE"] = vText("REQ-PARTICIPANT")
    attendee.params["PARTSTAT"] = vText("NEEDS-ACTION")
    attendee.params["RSVP"] = vText("TRUE")
    event.add("attendee", attendee, encode=0)

    description_lines = [f"TRIP_TYPE={'ONE_WAY' if one_way else 'ROUND_TRIP'}"]
    if pinned:
        # Pin the chosen coordinates to the event itself rather than
        # relying on the geocode cache. The cache is keyed on the location
        # string and expires; a trip booked three months out would be
        # re-looked-up long after the entry aged out, and could resolve to
        # a different branch of the same chain than the one the user
        # picked — silently, with the event text unchanged. GEO= travels
        # with the event and outlives any cache.
        description_lines.append(f"GEO={pinned}")
    event.add("description", "\n".join(description_lines))

    cal = ics_store.load_or_create_calendar(ICS_PATH)
    ics_store.upsert_event(cal, event)
    ics_store.save_calendar(cal, ICS_PATH)
    homeassistant.update_entity(entity_id=CALENDAR_ENTITY)
    refresh_manual_trip_list(cal)

    organizer_map = json_store.load_json_map(ORGANIZER_MAP_PATH, warn=log.warning)
    organizer_map[event_uid] = organizer_email
    json_store.save_json_map(ORGANIZER_MAP_PATH, organizer_map)

    # The trip is committed at this point — everything below is email.
    #
    # PROVISIONAL, not success. The SMTP send below blocks for as long as
    # it takes (up to two attempts at a 20-second socket timeout), and
    # state.set writes reach the frontend immediately because the send
    # itself runs in a @pyscript_executor thread and leaves the event loop
    # free. Claiming "scheduled" here and downgrading to a warning
    # afterwards meant anyone who looked away for two seconds saw a green
    # confirmation that later turned amber — the worst possible ordering,
    # since the false reassurance is the part they take with them.
    # An info banner is true at the moment it's shown and resolves to
    # green or amber in place.
    trip_text = (
        f"Trip to {display_name} scheduled for "
        f"{parsed.strftime('%a %d %b, %H:%M')}"
        f"{' (one-way)' if one_way else ''}"
    )
    _set_form_status("info", f"{trip_text}. Sending the invite…", "schedule")
    input_text.set_value(entity_id=MANUAL_TRIP_LOCATION, value="")
    input_boolean.turn_off(entity_id=MANUAL_TRIP_ONE_WAY)
    _reset_destination_results()
    _reset_trip_datetime()

    msg_id = outbound_email.send_trip_invite_email(
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME,
        _invite_from(organizer_email), organizer_email, event, "REQUEST",
        parsed.strftime("%Y-%m-%d %H:%M"), display_name,
    )
    if msg_id:
        msgid_map = json_store.load_json_map(SENT_INVITE_MSGID_PATH, warn=log.warning)
        msgid_map[event_uid] = msg_id
        json_store.save_json_map(SENT_INVITE_MSGID_PATH, msgid_map)
        _set_form_status("success", f"{trip_text}.", "schedule")
    else:
        _email_failed(organizer_email, "confirmation")

    log.info(
        f"Manually scheduled trip {event_uid} to '{location}' at {parsed} "
        f"({'one-way' if one_way else 'round trip'}), organizer {organizer_email}"
    )


def _invite_from(member_email):
    """Address to put in From: on an outbound invite. Always tesla@.

    The `send_as_organizer` option that used to make this conditional is
    gone. It could only ever produce a From: that disagreed with the
    ORGANIZER property or an unaligned DMARC envelope — both of which
    cost deliverability and neither of which bought anything, once the
    role flip made tesla@ the organizer in the first place.

    Kept as a function rather than inlining SMTP_USER at the three call
    sites so there remains one obvious place to look when someone asks
    "what address do invites come from".

    `member_email` is unused, and stays in the signature so the call
    sites still read as "the invite to this person" rather than a bare
    constant.
    """
    return SMTP_USER


_STATUS_ICONS = {
    "info": "mdi:progress-clock",
    "success": "mdi:check-circle-outline",
    "warning": "mdi:alert-outline",
    "error": "mdi:alert-circle-outline",
}


def _set_form_status(level, message, source):
    """Publish a message onto the dashboard form's inline banner.

    level:   "error" | "warning" | "success" — passed straight through as
             ha-alert's alert-type by the markdown card.
    source:  which code path wrote it, so _clear_form_status() can avoid
             clearing somebody else's message. See FORM_STATUS_SOURCES.

    state.set() entities don't survive a restart; refresh_manual_trip_options
    runs at startup and recreates this one as "ok".
    """
    state.set(FORM_STATUS_SENSOR, level, {
        # `level` duplicates the state deliberately. Reading a pyscript
        # entity's STATE with state.get() raises NameError when the entity
        # doesn't exist yet (which it won't, between a restart and the
        # startup trigger below) — state.getattr() just returns None. So
        # all the internal logic reads attributes only, and the state
        # exists purely for the dashboard's conditional card.
        "level": level,
        "message": message,
        "source": source,
        "updated": dt_util.now().isoformat(timespec="seconds"),
        "friendly_name": "Trip form status",
        "icon": _STATUS_ICONS.get(level, "mdi:alert-circle-outline"),
    })


def _clear_form_status(force=False):
    """Reset the banner to "ok" (which hides it).

    Only clears a status this app wrote, unless force=True — a successful
    reschedule shouldn't erase an unread geocoding error raised by
    tesla_trip_energy.py about a different trip.
    """
    current = state.getattr(FORM_STATUS_SENSOR) or {}
    level = current.get("level")
    if level == "ok":
        return  # already clear; don't churn the recorder every 5 minutes
    if not force and level is not None and current.get("source") not in FORM_STATUS_SOURCES:
        return
    state.set(FORM_STATUS_SENSOR, "ok", {
        "level": "ok",
        "message": "",
        "source": "",
        "updated": dt_util.now().isoformat(timespec="seconds"),
        "friendly_name": "Trip form status",
        "icon": "mdi:check-circle-outline",
    })


@service
def clear_trip_form_status():
    """Dismiss the form banner. Bound to the card's Dismiss button, and to
    an automation that fires when the location field is edited — retyping
    an address that failed to geocode should visibly reset the form."""
    _clear_form_status(force=True)


@service
def set_trip_form_status(level, message, source="schedule"):
    """Service wrapper so the dashboard's own validation can write to the
    same banner the app writes to.

    Without this, a form rejected in YAML (empty search box, past
    datetime, nothing picked) had to fall back to a persistent
    notification while a rejection from Python appeared inline — two
    different-looking outcomes for the same class of mistake. The
    dashboard passes source: form, which is deliberately NOT in
    FORM_STATUS_SOURCES: a message the user caused by mis-filling the
    form should not be auto-cleared by this app's next housekeeping pass.
    """
    _set_form_status(str(level), str(message), str(source))


# --------------------------------------------------------------------
# Destination search
# --------------------------------------------------------------------

def _summary_place(component):
    """The place name a person sees for an existing event.

    Prefers SUMMARY with its "Trip to " prefix stripped, since that was
    written from the short display name at schedule time. Falls back to
    LOCATION for events scheduled before the picker existed, or created
    by anything that didn't set a SUMMARY in that form.
    """
    summary = str(component.get("SUMMARY", "")).strip()
    if summary.lower().startswith("trip to "):
        return summary[8:].strip()
    if summary:
        return summary
    return _short_name(str(component.get("LOCATION", "")))


def _place_name(candidate):
    """The name a person needs to recognise a search result: street and
    house number, then the town.

    Built from Nominatim's structured address fields, NOT by slicing
    display_name. The chain has no fixed length — "25, Kerkstraat,
    Centrum, Haarlem, ..." puts the neighbourhood third and the city
    fourth — so taking the first three components hides exactly the field
    that tells you whether the result is in the right town, which is the
    one thing worth checking. Falls back to the slice when the structured
    fields are missing.
    """
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
    """The leading, distinguishing part of a Nominatim display_name.

    Fallback only — prefer _place_name() where the structured fields are
    available.

    display_name is the full address chain — "1, Dam, Burgwallen-Oude
    Zijde, Centrum, Amsterdam, Noord-Holland, Nederland, 1012 JS,
    Nederland" — which is the right thing to put in LOCATION, where a
    calendar client geolocates from it, and the wrong thing to put in a
    subject line, where it produced a six-line subject naming the country
    twice.
    """
    parts = [p.strip() for p in str(display).split(",") if p.strip()]
    return ", ".join(parts[:3]) if parts else str(display).strip()


def _query_house_number(query):
    """The house number the user typed, if any.

    Looks for a bare number token — "Kerkstraat 15 haarlem" -> "15".
    Postcodes and years are not bare numbers in this sense; "6228" would
    match, which is why the result is only ever used to add a warning
    label, never to reject a candidate.
    """
    for token in str(query).replace(",", " ").split():
        if token.isdigit():
            return token
    return None


def _dest_label(candidate, wanted_number=None):
    """Compact, scannable dropdown label for one search result.

    Nominatim's display_name is the whole address chain ("Lidl, 12,
    Vijzelstraat, Centrum-Oost, Amsterdam, Noord-Holland, Nederland,
    1017HL"), which is unreadable in a dropdown and overruns the option
    length limit. Keep the leading specifics and append the distance —
    distance is the field that actually tells two branches of the same
    chain apart, which is the entire reason this picker exists. The full
    display_name is kept in the mapping and used as the event LOCATION.
    """
    head = _place_name(candidate)
    if candidate.get("km") is not None:
        head = f"{head} — {candidate['km']:.0f} km"
    # Nominatim substitutes a nearby house number when the one asked for
    # doesn't exist, and says nothing about having done so. Flagging it
    # here is the only place the user can catch it: by the time the trip
    # is scheduled, the wrong number is just the address.
    got = candidate.get("house_number")
    if wanted_number and str(got or "") != str(wanted_number):
        head = f"{head} ⚠ number {got or 'n/a'}, not {wanted_number}"
    return head[:255]


def _reset_trip_datetime():
    """Park the datetime field on the next whole hour from now.

    input_datetime keeps whatever was last entered, so the form came back
    showing the previous trip's departure — a time in the past, often days
    old. That is worse than an arbitrary default: it looks deliberate, so
    it invites a submit without reading it, and the past-datetime guard
    then refuses a trip the user believed they had filled in correctly.
    """
    nxt = (dt_util.now().replace(minute=0, second=0, microsecond=0)
           + datetime.timedelta(hours=1))
    input_datetime.set_datetime(
        entity_id=MANUAL_TRIP_DATETIME,
        datetime=nxt.strftime("%Y-%m-%d %H:%M:%S"),
    )


def _reset_destination_results():
    """Put the destination dropdown back to its placeholder and drop the
    mapping. Called after a trip is scheduled, at startup, and by the
    Clear button, so a stale selection can never be submitted."""
    current = (state.getattr(MANUAL_TRIP_DEST_SELECT) or {}).get("options")
    if current != [NO_SEARCH_OPTION]:
        input_select.set_options(
            entity_id=MANUAL_TRIP_DEST_SELECT, options=[NO_SEARCH_OPTION]
        )
    state.set(MANUAL_TRIP_DEST_SENSOR, "0", {
        "mapping": {},
        "query": "",
        "friendly_name": "Destination search results",
        "icon": "mdi:map-search-outline",
    })


@service
def search_destination(query=None):
    """Search for `query` and offer the matches for the user to pick from.

    This is the half of geocoding that geocode() cannot do. That function
    runs unattended against inbound invites, so it has to collapse the
    candidate list to one and does it by picking the nearest to home —
    a reasonable default that is nonetheless a coin flip when two
    branches of the same chain sit at similar distances, and silently
    wrong when the nearest match isn't the one that was meant. Here a
    person is waiting, so the alternatives go on screen instead.
    """
    query = str(query or "").strip()
    if len(query) < 3:
        _set_form_status(
            "warning",
            "Type at least three characters of an address or place name, "
            "then tap Search.",
            "search",
        )
        return

    # zone.home missing is not a reason to refuse — it only costs the
    # distance column and the nearest-first ordering, leaving Nominatim's
    # own relevance order, which is a usable fallback.
    home = state.getattr("zone.home")
    home_coords = (
        (home["latitude"], home["longitude"])
        if home and "latitude" in home else None
    )
    if home_coords is None:
        log.warning("zone.home unavailable — search results won't show distances")

    candidates = geocode_mod.search_candidates(
        query, home_coords, NOMINATIM_UA, MAX_DESTINATION_RESULTS
    )

    # None and [] mean different things and get different messages: the
    # first is worth retrying unchanged, the second never is.
    if candidates is None:
        _reset_destination_results()
        _set_form_status(
            "error",
            "The address lookup service didn't answer. Try again in a "
            "moment — nothing has been scheduled.",
            "search",
        )
        return

    if not candidates:
        _reset_destination_results()
        _set_form_status(
            "warning",
            f"No places found for \"{query}\". Try a street and town, or a "
            f"business name with the town after it.",
            "search",
        )
        return

    wanted_number = _query_house_number(query)
    mapping = {}
    labels = []
    for c in candidates:
        label = _dest_label(c, wanted_number)
        unique = label
        n = 2
        while unique in mapping:
            unique = f"{label} ({n})"
            n += 1
        mapping[unique] = {
            "geo": f"{c['lat']:.6f},{c['lon']:.6f}",
            "display": c["display"],
            # Same as the dropdown label minus the distance, which
            # belongs on screen and not in an email subject.
            "short": _place_name(c),
        }
        labels.append(unique)

    # set_options resets the selection to options[0] — now the best
    # textual match rather than the nearest one, with its alternatives
    # visible underneath it.
    input_select.set_options(entity_id=MANUAL_TRIP_DEST_SELECT, options=labels)
    state.set(MANUAL_TRIP_DEST_SENSOR, str(len(mapping)), {
        "mapping": mapping,
        "query": query,
        "friendly_name": "Destination search results",
        "icon": "mdi:map-search-outline",
    })

    if len(labels) == 1:
        message = "Found one match. Check it, then tap Schedule."
    else:
        message = (
            f"Found {len(labels)} matches, best first. Pick the right one "
            f"before scheduling."
        )
    _set_form_status("success", message, "search")

    log.info(f"Destination search '{query}' -> {len(labels)} results")


@service
def clear_trip_destination():
    """Clear the search box, the results and the banner in one tap.

    Deliberately a button rather than an automation on the search text
    changing: input_text fires a state change on every debounced
    keystroke, so wiping the results reactively would destroy a valid
    selection the moment someone touched the box again.
    """
    input_text.set_value(entity_id=MANUAL_TRIP_LOCATION, value="")
    _reset_destination_results()
    _clear_form_status(force=True)


def _reject(reason, source="schedule"):
    """Refuse a manual scheduling request AND tell the user why.

    Refusals used to be log-only, while the dashboard script cleared the
    form regardless — so a rejected trip looked exactly like a successful
    one. They then became persistent-notification-only, which is barely
    better: the notification sits collapsed behind the sidebar bell.
    Now the form itself says so, in red, at the point of failure.
    """
    log.warning(f"schedule_manual_trip refused: {reason}")
    _notify_user("Trip not scheduled", reason, "tesla_manual_trip_rejected")
    _set_form_status("error", reason, source)


# kind -> (what definitely happened here, what is now stale over there).
#
# One shared template used to serve all three, phrased for the schedule
# case: "The trip is scheduled in Home Assistant... it won't appear in
# their own calendar." On a cancellation both halves are actively false —
# the trip is *gone* here, and it *will* still appear there. Someone
# reading that has no way to tell whether the cancellation took effect at
# all, which is the one thing the message exists to communicate.
_EMAIL_FAILURE_TEXT = {
    "confirmation": (
        "The trip is scheduled in Home Assistant and will be charged for",
        "the invite couldn't be sent, so it won't appear in their calendar",
    ),
    "cancellation": (
        "The trip has been cancelled in Home Assistant and will not be "
        "charged for",
        "the cancellation couldn't be sent, so it will still show in their "
        "calendar",
    ),
    "reschedule": (
        "The new time is saved in Home Assistant and will be charged for",
        "the update couldn't be sent, so their calendar still shows the old "
        "time",
    ),
}


def _email_failed(to_addr, kind):
    """Report an SMTP failure without leaving any doubt about what did
    happen locally.

    Every caller reaches this only AFTER the calendar write has been
    saved, so the local outcome is never in question — say so plainly
    and first, then describe what's stale on the recipient's side.
    """
    log.warning(f"Could not send {kind} email to {to_addr}")
    here, there = _EMAIL_FAILURE_TEXT.get(
        kind,
        ("The change is saved in Home Assistant",
         f"the {kind} email couldn't be sent, so their calendar is out of "
         f"date"),
    )
    message = (
        f"{here}. However, {there} — the email to {to_addr} failed. "
        f"Check the log for the SMTP error."
    )
    _notify_user("Trip email not sent", message, "tesla_manual_trip_email_failed")
    # Warning, not error: the local outcome is exactly what was asked for.
    # Only the copy in the member's own calendar app is out of sync.
    _set_form_status("warning", message, "email")


def _notify_user(title, message, notification_id):
    persistent_notification.create(
        title=title, message=message, notification_id=notification_id
    )


def _upcoming_manual_trips(cal):
    """Return (dtstart, uid, location) tuples for future manual-* events,
    sorted soonest-first. Skips anything not created by
    schedule_manual_trip and skips past events."""
    now = dt_util.now()
    trips = []
    for c in cal.subcomponents:
        if c.name != "VEVENT":
            continue
        uid = str(c.get("UID"))
        if not uid.startswith("manual-"):
            continue
        raw = c.get("DTSTART")
        if raw is None:
            continue
        dtstart = raw.dt
        if not isinstance(dtstart, datetime.datetime):
            continue  # all-day/date-only, not something this scheduler creates
        if dtstart.tzinfo is None:
            dtstart = dtstart.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        if dtstart < now:
            continue
        trips.append((dtstart, uid, str(c.get("LOCATION", ""))))
    trips.sort(key=lambda t: t[0])
    return trips


def _format_trip_label(dtstart, location):
    label = f"{dtstart.strftime('%Y-%m-%d %H:%M')} — {location}"
    return label[:255]  # input_select options have a practical length limit


def refresh_manual_trip_list(cal):
    """Populate input_select.manual_trip_to_cancel with every upcoming
    manual trip, so cancel/reschedule act on whichever one the user
    actually picked. The label -> uid mapping is stored as an attribute
    on sensor.manual_trip_options, since input_select options are plain
    strings with nowhere to carry a uid alongside them.

    Deliberately does NOT call input_select.select_option. The old code
    forced the selection back to labels[0] on every refresh, and this runs
    on a 5-minute cron: pick the third trip, get distracted for two
    minutes, tap "Cancel selected trip", and you cancel the first one
    instead. HA's own set_options already falls back to options[0] when
    the current selection is no longer valid, which is the only case that
    needed handling.

    Returns early when nothing changed — this used to rewrite the
    input_select and the sensor every 5 minutes forever, generating
    state-change events and recorder rows for identical data.
    """
    trips = _upcoming_manual_trips(cal)

    mapping = {}
    labels = []
    for dtstart, uid, location in trips:
        label = _format_trip_label(dtstart, location)
        unique_label = label
        n = 2
        while unique_label in mapping:
            unique_label = f"{label} ({n})"
            n += 1
        mapping[unique_label] = uid
        labels.append(unique_label)

    options = labels if labels else [NO_TRIPS_OPTION]

    current_options = (state.getattr(MANUAL_TRIP_SELECT) or {}).get("options")
    current_mapping = (state.getattr(MANUAL_TRIP_OPTIONS_SENSOR) or {}).get("mapping")
    if current_options == options and current_mapping == mapping:
        return

    input_select.set_options(entity_id=MANUAL_TRIP_SELECT, options=options)
    state.set(MANUAL_TRIP_OPTIONS_SENSOR, str(len(mapping)), {"mapping": mapping})


def _resolve_selection(selection, action):
    """Selection string -> uid, or None with a logged warning."""
    if selection in (NO_TRIPS_OPTION, "unknown", "unavailable", "", None):
        # Not an error worth a notification — there simply are no trips to
        # act on, and the dropdown says so. The form banner is enough.
        log.info(f"{action}: nothing selected ('{selection}')")
        _set_form_status(
            "error",
            "No trip is selected. Pick one from the list first — if the list "
            "is empty, there are no upcoming manual trips to change.",
            "selection",
        )
        return None

    options_state = state.getattr(MANUAL_TRIP_OPTIONS_SENSOR)
    mapping = (options_state or {}).get("mapping", {})
    uid = mapping.get(selection)
    if not uid:
        log.warning(f"{action}: no matching trip for selection '{selection}'")
        message = (
            f"Couldn't match \"{selection}\" to a scheduled trip. The list may "
            f"have refreshed — reopen the dashboard and try again."
        )
        _notify_user("Trip not found", message, "tesla_manual_trip_selection")
        _set_form_status("error", message, "selection")
    return uid


@service
def cancel_manual_trip(selection):
    """Cancel the manually-scheduled trip currently selected in
    input_select.manual_trip_to_cancel, and email the organizer a
    METHOD:CANCEL so their own calendar app drops it too.

    Only ever removes manual- UIDs — trips from a real calendar invite
    can only be removed via an actual inbound METHOD:CANCEL.
    """
    uid = _resolve_selection(selection, "cancel_manual_trip")
    if not uid:
        return

    cal = ics_store.load_or_create_calendar(ICS_PATH)
    existing = ics_store.find_event(cal, uid)

    if not ics_store.remove_event(cal, uid):
        log.warning(f"cancel_manual_trip: could not find event {uid} to remove")
        _set_form_status(
            "error",
            f"\"{selection}\" is no longer in the calendar — nothing was "
            f"cancelled. The trip list has been refreshed.",
            "cancel",
        )
        refresh_manual_trip_list(cal)
        return

    ics_store.save_calendar(cal, ICS_PATH)
    homeassistant.update_entity(entity_id=CALENDAR_ENTITY)
    refresh_manual_trip_list(cal)
    log.info(f"Cancelled manual trip {uid} (selection: '{selection}')")
    _set_form_status(
        "info", f"Cancelled \"{selection}\". Sending the cancellation…", "cancel"
    )

    organizer_map = json_store.load_json_map(ORGANIZER_MAP_PATH, warn=log.warning)
    organizer_email = organizer_map.pop(uid, None)
    if organizer_email is not None:
        json_store.save_json_map(ORGANIZER_MAP_PATH, organizer_map)

    msgid_map = json_store.load_json_map(SENT_INVITE_MSGID_PATH, warn=log.warning)
    in_reply_to = msgid_map.pop(uid, None)
    if in_reply_to is not None:
        json_store.save_json_map(SENT_INVITE_MSGID_PATH, msgid_map)

    # pop_uid, not pop: the alert map is keyed per occurrence now.
    for path in (ALERTED_PATH, ACCEPTED_PATH):
        side_map = json_store.load_json_map(path, warn=log.warning)
        if json_store.pop_uid(side_map, uid):
            json_store.save_json_map(path, side_map)

    if existing is None:
        # Nothing to send — resolve the provisional banner rather than
        # leaving "Sending the cancellation…" on screen forever.
        _set_form_status("success", f"Cancelled \"{selection}\".", "cancel")
        return
    if not organizer_email:
        log.warning(f"cancel_manual_trip: no organizer email on file for {uid}")
        _set_form_status(
            "warning",
            f"Cancelled \"{selection}\" here, but there's no organizer email "
            f"on file for it — no cancellation notice was sent, so it may "
            f"still show in their own calendar.",
            "cancel",
        )
        return

    # A cancellation must carry a SEQUENCE strictly higher than the one
    # the recipient last saw, or clients are entitled to discard it as a
    # stale duplicate — which is exactly what the old same-SEQUENCE
    # CANCEL looked like.
    ics_store.bump_sequence(existing)
    ics_store.set_status(existing, "CANCELLED")
    ics_store.touch_dtstamp(existing)

    # SUMMARY, not LOCATION: LOCATION holds the full address chain a
    # calendar client geolocates from, while SUMMARY already carries the
    # short form written at schedule time. Reading LOCATION here put a
    # six-line address in the cancel/update subject.
    location = _summary_place(existing)
    ok = outbound_email.send_trip_invite_email(
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME,
        _invite_from(organizer_email), organizer_email, existing, "CANCEL",
        _start_text(existing), location, in_reply_to=in_reply_to,
    )
    if ok:
        _set_form_status("success", f"Cancelled \"{selection}\".", "cancel")
    else:
        _email_failed(organizer_email, "cancellation")


@service
def reschedule_manual_trip(selection, new_datetime):
    """Reschedule the manually-scheduled trip currently selected in
    input_select.manual_trip_to_cancel (reused as the trip picker for
    reschedule too).

    Bumps SEQUENCE, shifts DTSTART and DTEND together, refreshes DTSTAMP,
    and re-sends an updated invite so the organizer's own calendar app
    picks up the change — the same UID + higher SEQUENCE pattern real
    calendar clients use.
    """
    uid = _resolve_selection(selection, "reschedule_manual_trip")
    if not uid:
        return

    parsed = _parse_local(new_datetime)
    if parsed is None:
        _reject(f"Could not read the new date/time '{new_datetime}'.", "reschedule")
        return
    if parsed <= dt_util.now():
        _reject(
            f"The new trip time {parsed.strftime('%Y-%m-%d %H:%M')} is in the "
            f"past. Nothing was changed.",
            "reschedule",
        )
        return

    cal = ics_store.load_or_create_calendar(ICS_PATH)
    existing = ics_store.find_event(cal, uid)
    if existing is None:
        log.warning(f"reschedule_manual_trip: event {uid} not found")
        _set_form_status(
            "error",
            f"\"{selection}\" is no longer in the calendar — nothing was "
            f"rescheduled. The trip list has been refreshed.",
            "reschedule",
        )
        refresh_manual_trip_list(cal)
        return

    # Preserve the original duration rather than dropping DTEND.
    old_start_raw = existing.get("DTSTART")
    old_end_raw = existing.get("DTEND")
    duration = datetime.timedelta(minutes=MANUAL_TRIP_DURATION_MIN)
    if old_start_raw is not None and old_end_raw is not None:
        try:
            duration = old_end_raw.dt - old_start_raw.dt
        except Exception:
            pass

    new_seq = ics_store.bump_sequence(existing)
    ics_store.set_times(existing, parsed, parsed + duration)
    ics_store.touch_dtstamp(existing)

    ics_store.save_calendar(cal, ICS_PATH)
    homeassistant.update_entity(entity_id=CALENDAR_ENTITY)
    refresh_manual_trip_list(cal)
    moved_text = f"Moved to {parsed.strftime('%a %d %b, %H:%M')}"
    _set_form_status(
        "info", f"{moved_text}. Sending the update…", "reschedule"
    )

    organizer_map = json_store.load_json_map(ORGANIZER_MAP_PATH, warn=log.warning)
    organizer_email = organizer_map.get(uid)

    if not organizer_email:
        log.warning(f"reschedule_manual_trip: no organizer email on file for {uid}")
        _set_form_status(
            "warning",
            f"Rescheduled to {parsed.strftime('%a %d %b, %H:%M')} here, but "
            f"there's no organizer email on file — no update was sent, so "
            f"their own calendar still shows the old time.",
            "reschedule",
        )
        log.info(f"Rescheduled manual trip {uid} to {parsed} (sequence {new_seq})")
        return

    msgid_map = json_store.load_json_map(SENT_INVITE_MSGID_PATH, warn=log.warning)
    in_reply_to = msgid_map.get(uid)
    # SUMMARY, not LOCATION: LOCATION holds the full address chain a
    # calendar client geolocates from, while SUMMARY already carries the
    # short form written at schedule time. Reading LOCATION here put a
    # six-line address in the cancel/update subject.
    location = _summary_place(existing)
    msg_id = outbound_email.send_trip_invite_email(
        SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_FROM_NAME,
        _invite_from(organizer_email), organizer_email, existing, "REQUEST",
        parsed.strftime("%Y-%m-%d %H:%M"), location, in_reply_to=in_reply_to,
    )
    if msg_id:
        msgid_map[uid] = msg_id
        json_store.save_json_map(SENT_INVITE_MSGID_PATH, msgid_map)
        _set_form_status("success", f"{moved_text}.", "reschedule")
    else:
        _email_failed(organizer_email, "reschedule")

    log.info(f"Rescheduled manual trip {uid} to {parsed} (sequence {new_seq})")


# Offset from check_tesla_invites' */5 so the two don't fire in the same
# second and race each other writing the same input_select. Also runs at
# startup, because sensor.manual_trip_options is created with state.set
# and doesn't survive a restart — without this there's a window of up to
# 5 minutes after boot where cancel/reschedule can't resolve a selection.
@time_trigger("startup")
def init_trip_form_status():
    """Create sensor.tesla_trip_form_status at boot.

    state.set() entities don't survive a restart, and a conditional card
    bound to an entity that doesn't exist logs a warning on every render.
    Startup-only on purpose: this must NOT run on the 5-minute cron, or an
    error raised two minutes after a failed submit would be wiped before
    anyone read it.

    The destination results go with it. Their mapping lives on a
    state.set() sensor, which does not survive a restart, while the
    dropdown's options are restored by HA — so without this reset the
    form comes back showing yesterday's search results that resolve to
    nothing, and Submit refuses them with a message about searching first
    while the results are visibly right there.
    """
    _clear_form_status(force=True)
    _reset_destination_results()
    _reset_trip_datetime()


@time_trigger("startup")
@time_trigger("cron(2-59/5 * * * *)")
@service
def refresh_manual_trip_options():
    """Keep input_select.manual_trip_to_cancel in sync as trips age past
    their start time — schedule/cancel/reschedule already refresh it
    immediately, this catches the passive case."""
    cal = ics_store.load_or_create_calendar(ICS_PATH)
    refresh_manual_trip_list(cal)
