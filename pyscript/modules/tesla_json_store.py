"""
tesla_json_store — shared pyscript module
---------------------------------------------
Generic load/save for a small JSON dict on disk. Used for the UID ->
organizer-email map, the UID -> last-sent-Message-ID map, the UID ->
last-alerted-failure map, the UID -> accepted-sequence map, the household
map, and the geocode/route caches — all of them are just "a small JSON
dict", so one generic loader/saver covers the lot.

Must live at /config/pyscript/modules/tesla_json_store.py. Import from
an app with:
    import tesla_json_store as json_store

Writes inherit tesla_file_io.write_file()'s atomicity, so a crash
mid-save can no longer leave a half-written map behind.

Callers pass a `log` function for warnings, since this module has no
pyscript `log` global of its own outside a triggered app context and
shouldn't assume one — pass Python's logging.warning, pyscript's
log.warning, or None to suppress.

OCCURRENCE KEYS (1 Oct). The trip-energy alert map is keyed per
OCCURRENCE, not per UID: "<uid>|occ=<unix start>". A recurring series
shares one UID, so a UID key meant a failure alerted for this week's
instance silently suppressed the same alert for next week's. The helpers
below treat a plain UID key and its occurrence keys as one family, so a
cancellation or retention prune that names the UID clears all of them.
"""
import json
import tesla_file_io

OCC_SEP = "|occ="


def occurrence_key(uid, start):
    """Per-occurrence key: UID plus the occurrence start as a whole-second
    Unix timestamp (timezone representation does not matter)."""
    return f"{uid}{OCC_SEP}{int(start.timestamp())}"


def uid_of(key):
    """The UID part of a plain or occurrence key."""
    return str(key).split(OCC_SEP, 1)[0]


def load_json_map(path, warn=None):
    """Load a JSON object from path as a dict. Empty dict if missing or
    unreadable. `warn(msg)` is called (if provided) on read/parse errors
    other than the file simply not existing yet."""
    try:
        raw = tesla_file_io.read_file(path)
        return json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        if warn:
            warn(f"Could not read {path}, starting fresh: {e}")
        return {}


def save_json_map(path, data):
    tesla_file_io.ensure_dir(path)
    tesla_file_io.write_file(path, json.dumps(data).encode("utf-8"))


def prune_to_keys(mapping, keep_keys):
    """Drop entries whose UID isn't in `keep_keys`. Mutates `mapping`,
    returns True if anything was removed.

    These maps are keyed by event UID (or by occurrence key, see
    uid_of()) and only ever lost entries on an explicit cancellation —
    an event that simply happened left its entry behind forever. Called
    after the calendar's own retention prune, with the set of UIDs still
    present. Matching on uid_of() keeps an occurrence key alive for as
    long as its series is.
    """
    stale = [k for k in mapping if uid_of(k) not in keep_keys]
    for k in stale:
        mapping.pop(k, None)
    return bool(stale)


def pop_uid(mapping, uid):
    """Remove the plain key `uid` and every occurrence key of it.
    Returns True if anything was removed. The cancellation counterpart
    of prune_to_keys()."""
    stale = [k for k in mapping if uid_of(k) == uid]
    for k in stale:
        mapping.pop(k, None)
    return bool(stale)


def prune_expired(mapping, now_ts, max_age_seconds, ts_key="ts"):
    """Drop cache entries older than max_age_seconds. Entries are dicts
    carrying a unix timestamp under `ts_key`; anything malformed or
    missing a timestamp is dropped too. Returns True if anything went."""
    stale = []
    for key, value in mapping.items():
        if not isinstance(value, dict):
            stale.append(key)
            continue
        ts = value.get(ts_key)
        if not isinstance(ts, (int, float)) or now_ts - ts > max_age_seconds:
            stale.append(key)
    for key in stale:
        mapping.pop(key, None)
    return bool(stale)
