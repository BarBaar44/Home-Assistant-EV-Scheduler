"""
tesla_ics_store — shared pyscript module
-------------------------------------------
Load/save an .ics file as an icalendar Calendar object, plus VEVENT
helpers (add/replace/remove/find/prune, organizer extraction, and the
iMIP bookkeeping properties DTSTAMP/SEQUENCE/STATUS) used by both the
mailbox-polling path and the manual-trip-scheduling path in
tesla_calendar.py. tesla_trip_energy.py imports it read-only, for
occurrence_starts().

Must live at /config/pyscript/modules/tesla_ics_store.py. Import from an
app with:
    import tesla_ics_store as ics_store

All functions take the target path as an explicit argument rather than
reading it from a module-level constant, since a shared module has no
app_config of its own — the calling app owns that configuration.

RECURRING EVENTS (1 Oct). A recurring invite is one UID spread over
several VEVENTs: a MASTER carrying RRULE (and possibly EXDATE/RDATE), plus
zero or more OVERRIDES, each carrying RECURRENCE-ID naming the original
occurrence it replaces (a moved or edited single instance). Everything in
this module that used to key on UID alone now keys on (UID, RECURRENCE-ID)
or works on the whole series:

* replace_series()   — a REQUEST carrying the master replaces every
                       component of that UID. upsert_event() alone let an
                       override overwrite the master.
* upsert_event()     — matches on (UID, RECURRENCE-ID), so an override
                       for one instance sits next to the master.
* cancel_occurrence()— a CANCEL with RECURRENCE-ID drops that instance
                       only (removes the override, adds EXDATE to the
                       master). remove_event() used to drop the series.
* prune_past_events()— a master is judged by the end of its LAST
                       occurrence, not its first. An unbounded series is
                       never pruned. Anything whose end cannot be worked
                       out is kept: deleting a live series is the failure
                       that matters, a stale one costs a few bytes.

Recurrence is expanded with dateutil, which icalendar itself depends on.
Expansion is done in the DTSTART's own wall-clock time, so a weekly 09:00
meeting stays at 09:00 across the DST change.
"""
import datetime
from icalendar import Calendar
from icalendar.prop import vRecur
from dateutil.rrule import rrulestr
import homeassistant.util.dt as dt_util
import tesla_file_io

PRODID = "-//Home Assistant//Tesla Calendar//EN"


def new_calendar(method=None):
    cal = Calendar()
    cal.add("prodid", PRODID)
    cal.add("version", "2.0")
    if method:
        cal.add("method", method)
    return cal


def load_or_create_calendar(path):
    """Load the .ics, or start a fresh calendar if it's missing.

    A file that exists but does not parse (truncated by an unclean write,
    hand-edited badly, disk corruption) used to raise on EVERY subsequent
    run, permanently wedging the invite pipeline — and because messages
    are flagged TeslaProcessed on the server, anything handled during the
    failing run was gone for good. Now a bad file is moved aside to
    <path>.corrupt and we start clean, so the next poll works and the
    original is still there to inspect.
    """
    try:
        raw = tesla_file_io.read_file(path)
    except FileNotFoundError:
        return new_calendar()
    except Exception:
        return new_calendar()

    try:
        return Calendar.from_ical(raw)
    except Exception:
        try:
            tesla_file_io.rename_file(path, path + ".corrupt")
        except Exception:
            pass
        return new_calendar()


def save_calendar(cal, path):
    tesla_file_io.ensure_dir(path)
    tesla_file_io.write_file(path, cal.to_ical())


# --------------------------------------------------------------------
# Time helpers
# --------------------------------------------------------------------

def _aware(value):
    """Tz-aware datetime for a DTSTART/DTEND/RECURRENCE-ID value. A
    date-only value becomes local midnight; a naive datetime is taken as
    local wall time (never as UTC, see learnings: dt_util.as_local)."""
    if isinstance(value, datetime.datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
        return value
    return dt_util.start_of_local_day(value)


def recurrence_key(component):
    """RECURRENCE-ID as a whole-second Unix timestamp, or None for a
    master or a plain single event. A timestamp so that a TZID form and a
    UTC form of the same instant compare equal."""
    raw = component.get("RECURRENCE-ID")
    if raw is None:
        return None
    return int(_aware(raw.dt).timestamp())


def is_recurring(component):
    return component.get("RRULE") is not None


def _duration(component):
    """DTEND - DTSTART, else DURATION, else zero (all-day: one day)."""
    start_raw = component.get("DTSTART")
    end_raw = component.get("DTEND")
    if start_raw is not None and end_raw is not None:
        try:
            return _aware(end_raw.dt) - _aware(start_raw.dt)
        except Exception:
            pass
    dur_raw = component.get("DURATION")
    if dur_raw is not None:
        try:
            return dur_raw.dt
        except Exception:
            pass
    if start_raw is not None and not isinstance(start_raw.dt, datetime.datetime):
        return datetime.timedelta(days=1)
    return datetime.timedelta(0)


def _first_rrule(component):
    raw = component.get("RRULE")
    if isinstance(raw, list):
        return raw[0] if raw else None
    return raw


def _rule(component):
    """(dateutil rule over NAIVE wall-clock times, tzinfo to attach) for
    a master VEVENT, or None if it has no usable RRULE.

    Expanded naive, in the DTSTART's own zone, then the zone reattached:
    that keeps wall-clock time across DST, and sidesteps dateutil's
    refusal to mix an aware DTSTART with a date-only or floating UNTIL.
    A UTC UNTIL is converted into the same wall-clock frame first.
    """
    recur = _first_rrule(component)
    start_raw = component.get("DTSTART")
    if recur is None or start_raw is None:
        return None
    start = _aware(start_raw.dt)
    tz = start.tzinfo
    parts = vRecur(dict(recur))
    until = parts.get("UNTIL")
    if until:
        u = until[0] if isinstance(until, list) else until
        if isinstance(u, datetime.datetime):
            if u.tzinfo is not None:
                u = u.astimezone(tz)
            u = u.replace(tzinfo=None)
        else:
            u = datetime.datetime.combine(u, datetime.time(23, 59, 59))
        parts["UNTIL"] = [u]
    text = parts.to_ical().decode()
    return rrulestr(text, dtstart=start.replace(tzinfo=None)), tz


def _rdates(component):
    """Every RDATE as an aware datetime (rare, but legal alongside RRULE)."""
    raw = component.get("RDATE")
    if raw is None:
        return []
    groups = raw if isinstance(raw, list) else [raw]
    out = []
    for group in groups:
        for item in getattr(group, "dts", []):
            value = item.dt
            if isinstance(value, tuple):  # PERIOD form
                value = value[0]
            out.append(_aware(value))
    return out


def occurrence_starts(component, window_start, window_end):
    """Aware start times of a master's occurrences in [window_start,
    window_end]. DTSTART itself is always included when it is in the
    window (RFC 5545 counts it as the first instance even when it does
    not match the rule; dateutil does not). EXDATE is not applied: this
    feeds a lookup index, where an extra key is harmless. Returns [] on
    any expansion failure; callers fall back to their old behaviour.
    For an all-day series the starts are local midnights.
    """
    start_raw = component.get("DTSTART")
    if start_raw is None:
        return []
    first = _aware(start_raw.dt)
    out = []
    if window_start <= first <= window_end:
        out.append(first)
    try:
        built = _rule(component)
    except Exception:
        built = None
    if built is None:
        return out
    rule, tz = built
    lo = window_start.astimezone(tz).replace(tzinfo=None)
    hi = window_end.astimezone(tz).replace(tzinfo=None)
    try:
        naive = rule.between(lo, hi, inc=True)
    except Exception:
        return out
    seen = {int(first.timestamp())}
    for n in naive:
        occ = n.replace(tzinfo=tz)
        ts = int(occ.timestamp())
        if ts not in seen:
            seen.add(ts)
            out.append(occ)
    for rd in _rdates(component):
        ts = int(rd.timestamp())
        if window_start <= rd <= window_end and ts not in seen:
            seen.add(ts)
            out.append(rd)
    out.sort()
    return out


def series_end(component):
    """Tz-aware end of the LAST occurrence of a master, or None when the
    series never ends (no UNTIL, no COUNT) or the end can't be worked
    out. None means "keep it" to every caller."""
    try:
        built = _rule(component)
    except Exception:
        return None
    if built is None:
        return None
    rule, tz = built
    recur = _first_rrule(component)
    if not recur.get("UNTIL") and not recur.get("COUNT"):
        return None  # unbounded
    try:
        last_naive = rule[-1]
    except IndexError:
        last_naive = None  # rule yields nothing past DTSTART
    except Exception:
        return None
    candidates = [_aware(component.get("DTSTART").dt)]
    if last_naive is not None:
        candidates.append(last_naive.replace(tzinfo=tz))
    candidates.extend(_rdates(component))
    return max(candidates) + _duration(component)


# --------------------------------------------------------------------
# VEVENT CRUD
# --------------------------------------------------------------------

def _same(c, uid, rid):
    return (
        c.name == "VEVENT"
        and str(c.get("UID")) == uid
        and recurrence_key(c) == rid
    )


def remove_event(cal, uid):
    """Remove EVERY component with this UID: a plain event, or a whole
    recurring series (master plus all overrides)."""
    before = len(cal.subcomponents)
    cal.subcomponents = [
        c for c in cal.subcomponents
        if not (c.name == "VEVENT" and str(c.get("UID")) == uid)
    ]
    return len(cal.subcomponents) != before


def upsert_event(cal, new_event):
    """Add or replace ONE component, matched on (UID, RECURRENCE-ID).
    An override therefore lands next to its master instead of over it.
    A lower SEQUENCE than the stored copy is stale and ignored."""
    uid = str(new_event.get("UID"))
    rid = recurrence_key(new_event)
    new_seq = int(new_event.get("SEQUENCE", 0))

    for i, c in enumerate(cal.subcomponents):
        if _same(c, uid, rid):
            old_seq = int(c.get("SEQUENCE", 0))
            if new_seq < old_seq:
                return False  # stale/out-of-order update, ignore
            cal.subcomponents[i] = new_event
            return True

    cal.add_component(new_event)
    return True


def replace_series(cal, components):
    """Apply one REQUEST's components for ONE UID.

    If the set contains the master (no RECURRENCE-ID), it is the whole
    series as the organizer now sees it (RFC 5546: a series update
    carries every override that still applies), so every stored
    component of that UID is replaced by this set. Overrides that the
    organizer dropped therefore disappear instead of lingering.

    Without a master it is an update to individual instances only, and
    each override is upserted on its own.

    The SEQUENCE guard compares masters: a set whose master is older
    than the stored master is a stale delivery and changes nothing.
    Returns True if the calendar changed.
    """
    if not components:
        return False
    uid = str(components[0].get("UID"))
    masters = [c for c in components if recurrence_key(c) is None]

    if not masters:
        changed = False
        for c in components:
            if upsert_event(cal, c):
                changed = True
        return changed

    new_seq = int(masters[0].get("SEQUENCE", 0))
    old = find_event(cal, uid)
    if old is not None and new_seq < int(old.get("SEQUENCE", 0)):
        return False

    remove_event(cal, uid)
    for c in components:
        cal.add_component(c)
    return True


def cancel_occurrence(cal, uid, rid_prop):
    """Cancel ONE instance of a series. `rid_prop` is the CANCEL's
    RECURRENCE-ID property (icalendar vDDDTypes).

    Removes a stored override for that instance, and adds the instance to
    the master's EXDATE so the expanded series skips it. Adds the EXDATE
    in the same form as the RECURRENCE-ID it came with, which is what
    calendar clients match on. Returns True if anything changed.
    """
    rid = int(_aware(rid_prop.dt).timestamp())
    before = len(cal.subcomponents)
    cal.subcomponents = [c for c in cal.subcomponents if not _same(c, uid, rid)]
    changed = len(cal.subcomponents) != before

    master = find_event(cal, uid)
    if master is not None and is_recurring(master):
        if rid not in _exdate_keys(master):
            master.add("exdate", rid_prop.dt)
            changed = True
    return changed


def _exdate_keys(component):
    raw = component.get("EXDATE")
    if raw is None:
        return set()
    groups = raw if isinstance(raw, list) else [raw]
    keys = set()
    for group in groups:
        for item in getattr(group, "dts", []):
            keys.add(int(_aware(item.dt).timestamp()))
    return keys


def find_event(cal, uid):
    """Return the MASTER (or plain) VEVENT for uid, or None. Overrides
    are skipped: every caller wants the component that carries the
    series' SEQUENCE, organizer and RRULE. Falls back to the first
    override when a UID has no master at all (an invite to a single
    instance of someone else's series)."""
    fallback = None
    for c in cal.subcomponents:
        if c.name == "VEVENT" and str(c.get("UID")) == uid:
            if recurrence_key(c) is None:
                return c
            if fallback is None:
                fallback = c
    return fallback


def all_uids(cal):
    """Every VEVENT UID currently in the calendar — used to prune the
    side-car JSON maps (organizer / sent-message-id / alert state) so they
    don't accumulate entries for events that simply happened."""
    return {
        str(c.get("UID"))
        for c in cal.subcomponents
        if c.name == "VEVENT" and c.get("UID") is not None
    }


def get_event_end(component):
    """Best-effort tz-aware end time for ONE VEVENT instance.

    Falls back to DTSTART when there's no DTEND. All-day (date-only)
    values are treated as ending at the end of that local day.
    Returns None if the event has neither property. Ignores RRULE: for
    a series, use series_end().
    """
    for prop in ("DTEND", "DTSTART"):
        raw = component.get(prop)
        if raw is None:
            continue
        value = raw.dt
        if isinstance(value, datetime.datetime):
            if value.tzinfo is None:
                value = value.replace(tzinfo=dt_util.DEFAULT_TIME_ZONE)
            return value
        # date-only: end of that day, local
        return dt_util.start_of_local_day(value) + datetime.timedelta(days=1)
    return None


def prune_past_events(cal, cutoff):
    """Drop VEVENTs that finished before `cutoff` (a tz-aware datetime).

    Nothing used to remove past events, so tesla.ics grew forever and was
    fully re-parsed and re-serialised on every 5-minute poll, and re-read
    end to end by the Remote Calendar integration.

    A recurring master is judged by series_end(), the end of its LAST
    occurrence. It used to be judged by its first, so a weekly meeting
    that started more than retention_days ago was deleted while it was
    still running. An unbounded series, or one whose end can't be
    computed, is kept. Overrides are judged on their own instance.

    Returns the number of components removed.
    """
    kept = []
    removed = 0
    for c in cal.subcomponents:
        if c.name != "VEVENT":
            kept.append(c)
            continue
        if is_recurring(c) and recurrence_key(c) is None:
            end = series_end(c)
        else:
            end = get_event_end(c)
        if end is not None and end < cutoff:
            removed += 1
            continue
        kept.append(c)
    if removed:
        cal.subcomponents = kept
    return removed


def get_organizer_email(component):
    """Extract the plain email address from a VEVENT's ORGANIZER property."""
    return _address_of(component.get("ORGANIZER"))


def get_attendee_emails(component):
    """Every ATTENDEE address on a VEVENT, lowercased."""
    raw = component.get("ATTENDEE")
    if raw is None:
        return []
    values = raw if isinstance(raw, list) else [raw]
    out = []
    for value in values:
        addr = _address_of(value)
        if addr:
            out.append(addr.lower())
    return out


def _address_of(value):
    if value is None:
        return None
    addr = str(value).strip()
    if addr.lower().startswith("mailto:"):
        addr = addr[7:]
    addr = addr.strip().strip("<>").strip()
    return addr or None


# --------------------------------------------------------------------
# iMIP bookkeeping properties
#
# Every VEVENT in an iTIP message needs a DTSTAMP (RFC 5545 requires it —
# strict clients reject the object outright, lenient ones downgrade it to
# a plain imported event with no scheduling identity). Updates and
# cancellations additionally need a SEQUENCE strictly higher than the one
# the recipient last saw, or clients are entitled to discard them as
# stale — which is exactly what a same-SEQUENCE CANCEL looks like.
# --------------------------------------------------------------------

def touch_dtstamp(event):
    """Set DTSTAMP to now (UTC), replacing any existing value."""
    event.pop("DTSTAMP", None)
    event.add("dtstamp", dt_util.utcnow())


def bump_sequence(event):
    """Increment SEQUENCE and return the new value."""
    new_seq = int(event.get("SEQUENCE", 0)) + 1
    event.pop("SEQUENCE", None)
    event.add("sequence", new_seq)
    return new_seq


def set_status(event, status):
    event.pop("STATUS", None)
    event.add("status", status)


def set_times(event, start, end):
    """Replace DTSTART/DTEND with tz-aware values."""
    event.pop("DTSTART", None)
    event.pop("DTEND", None)
    event.add("dtstart", start)
    event.add("dtend", end)
