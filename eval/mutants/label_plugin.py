"""pytest plugin for labeling. Load it with `-p label_plugin`.

Writes to $LABEL_OUT, as JSON, what README.md counts as failing:

  failed      node IDs with any failed report: set-up, the test itself,
              teardown, or one of its subtests. pytest 9 prints a test whose
              subtest failed as PASSED beside SUBFAILED lines, but the failed
              subtest's report carries the test's node ID, so it lands here.
              A test that times out or whose worker dies fails the same way.
  uncollected files that could not be imported. Every test in such a file fails.
  ran         how many distinct tests reported at all, to catch a run that
              stopped early

Under pytest-xdist every report is replayed on the controller, which is the
only process that writes the file.
"""

import json
import os

failed: set[str] = set()
uncollected: set[str] = set()
ran: set[str] = set()


def pytest_runtest_logreport(report):
    ran.add(report.nodeid)
    if report.outcome == "failed":
        failed.add(report.nodeid)


def pytest_collectreport(report):
    if report.failed:
        uncollected.add(report.nodeid)


def pytest_sessionfinish(session, exitstatus):
    out = os.environ.get("LABEL_OUT")
    if not out or hasattr(session.config, "workerinput"):
        return
    record = {
        "failed": sorted(failed),
        "uncollected": sorted(uncollected),
        "ran": len(ran),
        "exit_status": int(exitstatus),
    }
    with open(out, "w", encoding="utf-8") as handle:
        json.dump(record, handle)
