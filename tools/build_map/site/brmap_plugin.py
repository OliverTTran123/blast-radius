"""pytest plugin for the map builder. Load it with `-p brmap_plugin`.

For each test it
  - makes sure the file-open audit hook is installed and tells it which test
    is running, so opens can be charged to that test
  - switches coverage.py's measurement context to "<node id>|setup", "|run"
    and "|teardown" (lines measured before the first test keep the empty
    context: that is start-up, collection and module imports)
  - appends every report's outcome to $BRMAP_OUT/outcomes.<pid>.tsv

It changes nothing about how the tests run, and edits none of sqlglot's files.
"""

import os

import pytest

import brmap_audit

try:
    import coverage
except ImportError:  # the plugin still records opens and outcomes without it
    coverage = None

brmap_audit.install()


def _enter(item: pytest.Item, phase: str) -> None:
    brmap_audit.current_test = item.nodeid
    measuring = coverage.Coverage.current() if coverage is not None else None
    if measuring is not None:
        measuring.switch_context(f"{item.nodeid}|{phase}")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_setup(item: pytest.Item) -> None:
    _enter(item, "setup")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_call(item: pytest.Item) -> None:
    _enter(item, "run")


@pytest.hookimpl(tryfirst=True)
def pytest_runtest_teardown(item: pytest.Item) -> None:
    _enter(item, "teardown")


def pytest_runtest_logreport(report: pytest.TestReport) -> None:
    out = os.environ.get("BRMAP_OUT")
    if not out:
        return
    line = f"{report.nodeid}\t{report.when}\t{report.outcome}\t{report.duration:.3f}\n"
    descriptor = os.open(
        os.path.join(out, f"outcomes.{os.getpid()}.tsv"),
        os.O_WRONLY | os.O_CREAT | os.O_APPEND,
        0o644,
    )
    try:
        os.write(descriptor, line.encode("utf-8"))
    finally:
        os.close(descriptor)
