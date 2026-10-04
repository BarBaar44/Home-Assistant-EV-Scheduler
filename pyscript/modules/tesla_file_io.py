"""
tesla_file_io — shared pyscript module
----------------------------------------
Raw file read/write primitives, wrapped in @pyscript_executor since bare
open() is blocked in pyscript's interpreted code (independent of
allow_all_imports).

This is a pyscript MODULE, not an app — it must live at
/config/pyscript/modules/tesla_file_io.py (not under apps/) and has no
app_config of its own. Import it from an app with:
    import tesla_file_io

Pyscript gives every file its own separate global context, even within
the same app package — sibling files can't call each other's functions
without an explicit import. The modules/ folder is the one place pyscript
allows genuine cross-file imports, which is why this shared plumbing lives
here instead of just being split into another file under apps/.

Writes are ATOMIC (temp file + fsync + os.replace). Everything this
project persists — the entire calendar state in tesla.ics, plus five JSON
maps — goes through write_file(). A restart, OOM kill or full disk in the
middle of a plain "wb" write would truncate the file, and a truncated
tesla.ics used to wedge the whole pipeline permanently (invites already
flagged TeslaProcessed are never re-fetched). os.replace() is atomic on
the same filesystem, so a reader either sees the whole old file or the
whole new one.

`with` is deliberately avoided here — variables assigned inside a `with`
block did not stay visible outside it in pyscript's interpreter (see
Calendar_integration.md gotchas). Explicit open()/try/finally/close()
instead.
"""


@pyscript_executor
def read_file(path):
    f = open(path, "rb")
    try:
        data = f.read()
    finally:
        f.close()
    return data


@pyscript_executor
def write_file(path, data):
    """Atomically write `data` to `path`.

    Writes to `<path>.tmp` first, flushes and fsyncs so the bytes are
    genuinely on disk, then renames over the target. The temp file lives
    in the same directory as the target so os.replace() stays on one
    filesystem (it is not atomic across mount points).
    """
    import os

    tmp = path + ".tmp"
    f = open(tmp, "wb")
    try:
        f.write(data)
        f.flush()
        os.fsync(f.fileno())
    finally:
        f.close()
    os.replace(tmp, path)


@pyscript_executor
def file_exists(path):
    import os

    return os.path.exists(path)


@pyscript_executor
def rename_file(src, dst):
    """Move a file aside (used to quarantine a corrupt .ics rather than
    losing it silently). Overwrites dst if it exists."""
    import os

    os.replace(src, dst)


@pyscript_executor
def ensure_dir(path):
    """Create the parent directory of `path` if it doesn't exist."""
    import os

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
