#!/usr/bin/env python3
"""Runs sqlglot's tests under coverage.py and writes what each test touched.

Runs inside the map builder's image. Two passes:

  isolated  Every test in tests.json in its own process tree, pinned to one
            CPU. Whatever the tree executes or opens belongs to that test and
            to no other: its imports, the caches it fills, class set-up, the
            pool workers it spawns and any `python -c` child. This is the
            attribution the selector relies on.

  suite     The whole suite once, in one process, in collection order, with a
            coverage context per test. A test can run lines here that it does
            not run alone, because of state an earlier test left behind.

Usage:
  collect.py isolated --tests /data/tests.json --work /work/iso [--jobs 16] [--seed 0]
  collect.py suite    --tests /data/tests.json --work /work/suite [--seed 0]

Line sets are written as hexadecimal bit masks: bit n is set when line n ran.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = "/sqlglot"
PACKAGE_PREFIX = REPO + "/sqlglot/"
PYTEST = [sys.executable, "-m", "pytest", "-p", "brmap_plugin", "-p", "no:cacheprovider", "-q"]
LINE_BITS = """
    SELECT context.context, file.path, line_bits.numbits
    FROM line_bits
    JOIN file ON file.id = line_bits.file_id
    JOIN context ON context.id = line_bits.context_id
"""


def measured_environment(out: Path, seed: int) -> dict[str, str]:
    """The environment that turns on coverage and the open record in every process."""
    environment = dict(os.environ)
    environment.update(
        COVERAGE_PROCESS_START="/tool/coveragerc",
        COVERAGE_FILE=str(out / "cov"),
        BRMAP_OUT=str(out),
        PYTHONHASHSEED=str(seed),
    )
    return environment


def running_in_group(group: int) -> bool:
    """True while some process of the group is still running. A zombie is not."""
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        try:
            stat = Path("/proc", entry, "stat").read_text()
        except OSError:
            continue  # it ended while we were looking
        state, _parent, process_group = stat.rpartition(")")[2].split()[:3]
        if int(process_group) == group and state != "Z":
            return True
    return False


def run_measured(command: list[str], out: Path, seed: int, timeout: int) -> tuple[int, int, float]:
    """Run one pytest process tree to the end. Returns (pid, exit status, seconds)."""
    started = time.monotonic()
    with open(out / "pytest.log", "wb") as log:
        process = subprocess.Popen(
            command,
            cwd=REPO,
            env=measured_environment(out, seed),
            stdout=log,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        try:
            status = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
            status = -signal.SIGKILL
    # Helpers such as multiprocessing's resource tracker outlive pytest by a moment
    # and write their coverage file as they exit. Wait until the whole group is done.
    deadline = time.monotonic() + 120
    while running_in_group(process.pid) and time.monotonic() < deadline:
        time.sleep(0.05)
    return process.pid, status, time.monotonic() - started


def read_line_bits(path: Path) -> list[tuple[str, str, int]]:
    """(context, repo-relative file, line mask) rows of one coverage.py data file."""
    connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        rows = connection.execute(LINE_BITS).fetchall()
    finally:
        connection.close()
    return [
        (context, file[len(REPO) + 1 :], int.from_bytes(numbits, "little"))
        for context, file, numbits in rows
        if file.startswith(PACKAGE_PREFIX)
    ]


def read_tsv(out: Path, pattern: str) -> list[list[str]]:
    rows = []
    for path in sorted(out.glob(pattern)):
        for line in path.read_text("utf-8").splitlines():
            rows.append(line.split("\t"))
    return rows


def hex_masks(masks: dict[str, int]) -> dict[str, str]:
    return {file: format(mask, "x") for file, mask in sorted(masks.items()) if mask}


def merge(target: dict[str, int], file: str, mask: int) -> None:
    target[file] = target.get(file, 0) | mask


def outcome_of(reports: list[list[str]]) -> str:
    """One word for a test from its setup, call, teardown and subtest reports."""
    if any(outcome == "failed" for _, _, outcome, _ in reports):
        return "failed"
    if any(when == "call" and outcome == "passed" for _, when, outcome, _ in reports):
        return "passed"
    if any(outcome == "skipped" for _, _, outcome, _ in reports):
        return "skipped"
    return "not run"


def is_from(path: Path, pid: int) -> bool:
    """coverage.py names a data file <base>.<host>.pid<pid>.<random>."""
    return f".pid{pid}." in path.name


def summarize_isolated(test: dict, out: Path, pid: int, status: int, seconds: float) -> dict:
    """Everything one test's process tree executed and opened."""
    lines: dict[str, int] = {}
    before_test: dict[str, int] = {}
    main: dict[str, int] = {}
    unreadable = []
    data_files = sorted(out.glob("cov.*"))
    for path in data_files:
        try:
            rows = read_line_bits(path)
        except sqlite3.Error as error:
            unreadable.append(f"{path.name}: {error}")
            continue
        # The pytest process is the one we started. Every other file is a child's.
        is_main = is_from(path, pid)
        for context, file, mask in rows:
            merge(lines, file, mask)
            if is_main:
                merge(main, file, mask)
                if context == "":
                    merge(before_test, file, mask)
    child_only = {file: mask & ~main.get(file, 0) for file, mask in lines.items()}
    reports = [row for row in read_tsv(out, "outcomes.*.tsv") if row[0] == test["id"]]
    opens = sorted({(kind, path) for kind, path, _ in read_tsv(out, "opens.*.tsv")})
    return {
        "number": test["number"],
        "id": test["id"],
        "outcome": outcome_of(reports),
        "status": status,
        "seconds": round(seconds, 3),
        "data_files": len(data_files),
        "main_data_files": sum(is_from(path, pid) for path in data_files),
        "unreadable": unreadable,
        "lines": hex_masks(lines),
        "before_test": hex_masks(before_test),
        "child_only": hex_masks(child_only),
        "opens": opens,
    }


def run_isolated_test(test: dict, work: Path, cpu: int, seed: int, timeout: int) -> dict:
    out = work / f"{test['number']:04d}"
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    command = ["taskset", "-c", str(cpu), *PYTEST, test["id"]]
    pid, status, seconds = run_measured(command, out, seed, timeout)
    record = summarize_isolated(test, out, pid, status, seconds)
    (out / "result.json").write_text(json.dumps(record, separators=(",", ":")), "utf-8")
    for path in out.iterdir():
        keep_log = path.name == "pytest.log" and record["outcome"] != "passed"
        if path.name != "result.json" and not keep_log:
            path.unlink()
    return record


def isolated(options: argparse.Namespace) -> int:
    tests = json.loads(Path(options.tests).read_text("utf-8"))["tests"]
    if options.only:
        wanted = {int(number) for number in options.only.split(",")}
        tests = [test for test in tests if test["number"] in wanted]
    work = Path(options.work)
    work.mkdir(parents=True, exist_ok=True)
    todo = [test for test in tests if not (work / f"{test['number']:04d}" / "result.json").exists()]
    # Longest first, so the two half-minute tests do not become the tail.
    todo.sort(key=lambda test: -test["seconds"])
    cpus = sorted(os.sched_getaffinity(0))[: options.jobs]
    free: queue.Queue[int] = queue.Queue()
    for cpu in cpus:
        free.put(cpu)
    print(
        f"isolated: {len(todo)} of {len(tests)} tests to run on {len(cpus)} CPUs", file=sys.stderr
    )

    def job(test: dict) -> dict:
        cpu = free.get()
        try:
            return run_isolated_test(test, work, cpu, options.seed, options.timeout)
        finally:
            free.put(cpu)

    started = time.monotonic()
    counts: dict[str, int] = {}
    with ThreadPoolExecutor(len(cpus)) as pool:
        for done, future in enumerate(as_completed([pool.submit(job, t) for t in todo]), start=1):
            record = future.result()
            counts[record["outcome"]] = counts.get(record["outcome"], 0) + 1
            if record["outcome"] != "passed":
                print(f"isolated: {record['outcome']}: {record['id']}", file=sys.stderr)
            if done % 100 == 0 or done == len(todo):
                elapsed = time.monotonic() - started
                print(f"isolated: {done}/{len(todo)} in {elapsed:.0f} s {counts}", file=sys.stderr)
    return 0


def summarize_suite(out: Path, pid: int) -> dict:
    """Per-test, per-phase lines from the one process that ran the whole suite."""
    phases: dict[str, dict[str, dict[str, int]]] = {}
    before_tests: dict[str, int] = {}
    children: dict[str, int] = {}
    unreadable = []
    data_files = sorted(out.glob("cov.*"))
    for path in data_files:
        try:
            rows = read_line_bits(path)
        except sqlite3.Error as error:
            unreadable.append(f"{path.name}: {error}")
            continue
        is_main = is_from(path, pid)
        for context, file, mask in rows:
            if not is_main:
                merge(children, file, mask)
            elif context == "":
                merge(before_tests, file, mask)
            else:
                test, _, phase = context.rpartition("|")
                merge(phases.setdefault(test, {}).setdefault(phase, {}), file, mask)
    reports: dict[str, list[list[str]]] = {}
    for row in read_tsv(out, "outcomes.*.tsv"):
        reports.setdefault(row[0], []).append(row)
    opens: dict[str, set[tuple[str, str]]] = {}
    for kind, path, test in read_tsv(out, "opens.*.tsv"):
        opens.setdefault(test, set()).add((kind, path))
    return {
        "data_files": len(data_files),
        "main_data_files": sum(is_from(path, pid) for path in data_files),
        "unreadable": unreadable,
        "outcomes": {test: outcome_of(rows) for test, rows in sorted(reports.items())},
        "tests": {
            test: {phase: hex_masks(masks) for phase, masks in sorted(by_phase.items())}
            for test, by_phase in sorted(phases.items())
        },
        "before_tests": hex_masks(before_tests),
        "children": hex_masks(children),
        "opens": {test: sorted(found) for test, found in sorted(opens.items())},
    }


def suite(options: argparse.Namespace) -> int:
    out = Path(options.work)
    shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)
    # A few CPUs are plenty: the suite is serial, and this keeps sqlglot's own pools small.
    cpus = ",".join(str(cpu) for cpu in sorted(os.sched_getaffinity(0))[:4])
    print(f"suite: running the whole suite on CPUs {cpus}", file=sys.stderr)
    pid, status, seconds = run_measured(["taskset", "-c", cpus, *PYTEST], out, options.seed, 3600)
    record = summarize_suite(out, pid)
    record.update(status=status, seconds=round(seconds, 3))
    (out / "suite.json").write_text(json.dumps(record, separators=(",", ":")), "utf-8")
    counts: dict[str, int] = {}
    for outcome in record["outcomes"].values():
        counts[outcome] = counts.get(outcome, 0) + 1
    log_tail = (out / "pytest.log").read_text("utf-8", "replace").splitlines()[-1:]
    print(f"suite: exit {status} in {seconds:.0f} s {counts} | {log_tail}", file=sys.stderr)
    for path in out.glob("cov.*"):
        path.unlink()
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    for name in ("isolated", "suite"):
        mode = modes.add_parser(name)
        mode.add_argument("--tests", required=True, help="tests.json from the kit")
        mode.add_argument("--work", required=True, help="directory for this pass's records")
        mode.add_argument("--seed", type=int, default=0, help="PYTHONHASHSEED for the tests")
    modes.choices["isolated"].add_argument("--jobs", type=int, default=16)
    modes.choices["isolated"].add_argument("--timeout", type=int, default=1800)
    modes.choices["isolated"].add_argument("--only", help="comma-separated test numbers")
    options = parser.parse_args()
    return isolated(options) if options.mode == "isolated" else suite(options)


if __name__ == "__main__":
    sys.exit(main())
