"""
json_store (shared pyscript module, formerly tesla_json_store)
--------------------------------------------------------------
Generic load/save for a small JSON dict on disk: the side car maps
(organizer, accepted, sent Message-IDs, alert dedup, failed messages),
the household map and the geocode/route caches are all "a small JSON
dict", so one loader/saver covers the lot.

Lives at /config/pyscript/modules/json_store.py.
    import json_store

Writes inherit atomic_io.write_file()'s atomicity.

Callers pass a `warn` function for read errors, since a module has no
pyscript `log` of its own outside a triggered app context.

OCCURRENCE KEYS. Some maps are keyed per OCCURRENCE, "<uid>|occ=<unix
start>", because a recurring series shares one UID. The helpers below
treat a plain UID key and its occurrence keys as one family, so a
cancellation or retention prune that names the UID clears all of them.
"""
import json
import atomic_io

OCC_SEP = "|occ="


def occurrence_key(uid, start):
    """UID plus the occurrence start as a whole second Unix timestamp
    (timezone representation does not matter)."""
    return f"{uid}{OCC_SEP}{int(start.timestamp())}"


def uid_of(key):
    """The UID part of a plain or occurrence key."""
    return str(key).split(OCC_SEP, 1)[0]


def load_json_map(path, warn=None):
    """Load a JSON object from path as a dict. Empty dict if missing or
    unreadable; `warn(msg)` is called on errors other than "not there"."""
    try:
        raw = atomic_io.read_file(path)
        return json.loads(raw.decode("utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as e:
        if warn:
            warn(f"Could not read {path}, starting fresh: {e}")
        return {}


def save_json_map(path, data):
    atomic_io.ensure_dir(path)
    atomic_io.write_file(path, json.dumps(data).encode("utf-8"))


def prune_to_keys(mapping, keep_keys):
    """Drop entries whose UID isn't in `keep_keys`. Mutates `mapping`,
    returns True if anything was removed."""
    stale = [k for k in mapping if uid_of(k) not in keep_keys]
    for k in stale:
        mapping.pop(k, None)
    return bool(stale)


def pop_uid(mapping, uid):
    """Remove the plain key `uid` and every occurrence key of it.
    Returns True if anything was removed."""
    stale = [k for k in mapping if uid_of(k) == uid]
    for k in stale:
        mapping.pop(k, None)
    return bool(stale)


def pop_uids_from_files(paths, uids, warn=None):
    """pop_uid() every UID in `uids` from every map file in `paths`,
    saving only the files that changed. Used for the `linked_maps` of a
    calendar: maps owned by another app (trip energy alerts, sent
    Message-IDs) that must forget an event when it is cancelled or pruned."""
    if not uids:
        return
    for path in paths:
        mapping = load_json_map(path, warn=warn)
        changed = False
        for uid in uids:
            if pop_uid(mapping, uid):
                changed = True
        if changed:
            save_json_map(path, mapping)


def prune_files_to_keys(paths, keep_keys, warn=None):
    """prune_to_keys() for every map file in `paths`."""
    for path in paths:
        mapping = load_json_map(path, warn=warn)
        if prune_to_keys(mapping, keep_keys):
            save_json_map(path, mapping)


def prune_expired(mapping, now_ts, max_age_seconds, ts_key="ts"):
    """Drop cache entries older than max_age_seconds. Entries are dicts
    carrying a unix timestamp under `ts_key`; malformed ones go too."""
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
