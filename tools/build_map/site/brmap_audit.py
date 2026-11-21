"""Records which repository files a Python process opens, with sys.addaudithook.

install() is called by the pytest plugin in the test process and by
sitecustomize in every process, which is what reaches pool workers and other
child interpreters. Nothing in sqlglot is edited.

Each first-seen (kind, path, test) is appended at once to
$BRMAP_OUT/opens.<pid>.tsv with a single os.write, so a process that ends
through os._exit still leaves its record. Kinds:

  I  read by the import system (the module was imported)
  R  opened for reading by anything else (a fixture, or source read as text)
  W  opened for writing
"""

import os
import sys

REPO = os.environ.get("BRMAP_REPO", "/sqlglot")

# Set by the pytest plugin while a test runs. Empty during start-up and collection.
current_test = ""

_prefix = REPO.rstrip("/") + "/"
_seen: set[tuple[str, str, str]] = set()
_fd = -1


def _hook(event: str, args: tuple) -> None:
    if event != "open" or _fd < 0:
        return
    path, mode = args[0], args[1]
    if isinstance(path, int):
        return  # an already-open descriptor
    try:
        path = os.path.abspath(os.fsdecode(path))
    except (TypeError, ValueError):
        return
    if not path.startswith(_prefix) or "/__pycache__/" in path:
        return
    try:
        caller = sys._getframe(1).f_code.co_filename
    except ValueError:
        caller = ""
    if caller.startswith("<frozen importlib"):
        kind = "I"
    elif isinstance(mode, str) and set(mode) & set("wax+"):
        kind = "W"
    else:
        kind = "R"
    key = (kind, path[len(_prefix) :], current_test)
    if key not in _seen:
        _seen.add(key)
        os.write(_fd, ("\t".join(key) + "\n").encode("utf-8"))


def install() -> None:
    """Start recording into $BRMAP_OUT. Safe to call more than once."""
    global _fd
    out = os.environ.get("BRMAP_OUT")
    if _fd >= 0 or not out:
        return
    # Opened before the hook exists, so the log itself is never recorded.
    _fd = os.open(
        os.path.join(out, f"opens.{os.getpid()}.tsv"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    sys.addaudithook(_hook)
