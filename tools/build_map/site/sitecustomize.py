"""Start-up hook for every Python process in the map builder's image.

Python imports a module named sitecustomize at start-up when one is on the
path. This one does nothing unless the environment asks:

  BRMAP_OUT               record the repository files the process opens
  COVERAGE_PROCESS_START  measure the process with coverage.py (the manual
                          sub-process set-up from coverage.py's documentation)

That is how the pool workers and `python -c` children of a test get measured
and charged to that test, without editing any of sqlglot's files.
"""

import os

if os.environ.get("BRMAP_OUT"):
    import brmap_audit

    brmap_audit.install()

if os.environ.get("COVERAGE_PROCESS_START"):
    import coverage

    coverage.process_startup()
