"""
atomic_io (shared pyscript module, formerly tesla_file_io)
----------------------------------------------------------
Raw file read/write primitives, wrapped in @pyscript_executor since bare
open() is blocked in pyscript's interpreted code (independent of
allow_all_imports). Nothing in here is EV specific.

This is a pyscript MODULE, not an app: it lives at
/config/pyscript/modules/atomic_io.py and has no app_config of its own.
    import atomic_io

Writes are ATOMIC (temp file + fsync + os.replace). A restart, OOM kill
or full disk halfway through a plain "wb" write would truncate the file.
A reader now sees either the whole old file or the whole new one.

`with` is deliberately avoided: variables assigned inside a `with` block
did not stay visible outside it in pyscript's interpreter. Explicit
open()/try/finally/close() instead.

Every function here is self contained. Code inside a @pyscript_executor
function must never call another function defined in a pyscript parsed
file: it gets an unrun coroutine back instead of a result.
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
    """Atomically write `data` (bytes) to `path`, via <path>.tmp in the
    same directory so os.replace() stays on one filesystem."""
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
    """Move a file aside. Overwrites dst if it exists."""
    import os

    os.replace(src, dst)


@pyscript_executor
def ensure_dir(path):
    """Create the parent directory of `path` if it doesn't exist."""
    import os

    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)
