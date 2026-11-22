#!/usr/bin/env python3
"""Which sqlglot classes each test uses, one test per process.

Runs inside the labeling image, where sqlglot is the pinned tree at /sqlglot.
Writes <work>/<number>/used.json (a list of qualified class names) and
<work>/hierarchy.json (every class, its bases, and whether it is an enum or a
metaclass). A test that does not finish is left without used.json; the
assembler treats that as unknown and does not narrow from it.

  class_collect.py --tests /data/tests.json --work /work --jobs 16
"""

from __future__ import annotations

import argparse
import enum
import importlib
import json
import os
import pkgutil
import queue
import signal
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

REPO = "/sqlglot"
PYTEST = [
    sys.executable,
    "-m",
    "pytest",
    "-p",
    "class_plugin",
    "-p",
    "no:cacheprovider",
    "-q",
    "--tb=no",
]


def hierarchy() -> dict:
    """Every sqlglot class currently importable, with its bases and two flags."""
    import sqlglot

    modules = [sqlglot]
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if info.name.endswith(".__main__"):
            continue
        modules.append(importlib.import_module(info.name))
    found: dict[str, type] = {}
    for module in modules:
        pending = list(vars(module).values())
        while pending:
            value = pending.pop()
            if not isinstance(value, type):
                continue
            name = getattr(value, "__module__", "") or ""
            if name != "sqlglot" and not name.startswith("sqlglot."):
                continue
            qual = f"{name}.{value.__qualname__}"
            if qual in found:
                continue
            found[qual] = value
            pending.extend(item for item in vars(value).values() if isinstance(item, type))
    described = {}
    for qual, cls in sorted(found.items()):
        bases = []
        for base in cls.__mro__[1:]:
            module = getattr(base, "__module__", "") or ""
            if base is object or (module != "sqlglot" and not module.startswith("sqlglot.")):
                continue
            bases.append(f"{module}.{base.__qualname__}")
        described[qual] = {
            "bases": bases,
            "enum": bool(issubclass(cls, enum.Enum)),
            "meta": bool(issubclass(cls, type)),
        }
    return described


def environment(out: Path) -> dict[str, str]:
    kept = dict(os.environ)
    kept.update(
        CLASS_OUT=str(out / "used.json"),
        PYTHONHASHSEED="0",
        PYTHONDONTWRITEBYTECODE="1",
        SKIP_INTEGRATION="0",
        OMP_NUM_THREADS="1",
        OPENBLAS_NUM_THREADS="1",
        MKL_NUM_THREADS="1",
        NUMEXPR_NUM_THREADS="1",
    )
    return kept


def run_one(test: dict, work: Path, cpu: int, timeout: int) -> tuple[int, str, float]:
    out = work / f"{test['number']:04d}"
    out.mkdir(parents=True, exist_ok=True)
    started = time.monotonic()
    with open(out / "pytest.log", "wb") as log:
        process = subprocess.Popen(
            ["taskset", "-c", str(cpu), *PYTEST, test["id"]],
            cwd=REPO,
            env=environment(out),
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
    used = out / "used.json"
    outcome = "passed" if status == 0 and used.exists() else "failed"
    if outcome == "passed":
        log_path = out / "pytest.log"
        if log_path.exists():
            log_path.unlink()
    elif used.exists():
        # A failed test's record is incomplete. Leave it missing so a rerun does it again.
        used.unlink()
    return test["number"], outcome, time.monotonic() - started


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tests", required=True)
    parser.add_argument("--work", required=True)
    parser.add_argument("--jobs", type=int, default=16)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--only", default="")
    options = parser.parse_args()
    tests = json.loads(Path(options.tests).read_text(encoding="utf-8"))["tests"]
    if options.only:
        wanted = {int(number) for number in options.only.split(",")}
        tests = [test for test in tests if test["number"] in wanted]
    work = Path(options.work)
    work.mkdir(parents=True, exist_ok=True)
    if not (work / "hierarchy.json").exists():
        (work / "hierarchy.json").write_text(
            json.dumps(hierarchy(), separators=(",", ":"), sort_keys=True), encoding="utf-8"
        )
        print(f"hierarchy: {work / 'hierarchy.json'}", file=sys.stderr)
    todo = [test for test in tests if not (work / f"{test['number']:04d}" / "used.json").exists()]
    todo.sort(key=lambda test: -test["seconds"])
    cpus = sorted(os.sched_getaffinity(0))[: options.jobs]
    print(f"classes: {len(todo)} of {len(tests)} tests on {len(cpus)} CPUs", file=sys.stderr)
    free: queue.Queue[int] = queue.Queue()
    for cpu in cpus:
        free.put(cpu)
    started = time.monotonic()
    counts: dict[str, int] = {}

    def job(test: dict) -> tuple[int, str, float]:
        cpu = free.get()
        try:
            return run_one(test, work, cpu, options.timeout)
        finally:
            free.put(cpu)

    with ThreadPoolExecutor(max(len(cpus), 1)) as pool:
        futures = [pool.submit(job, test) for test in todo]
        for done, future in enumerate(as_completed(futures), start=1):
            number, outcome, seconds = future.result()
            counts[outcome] = counts.get(outcome, 0) + 1
            if outcome != "passed":
                print(f"classes: failed #{number} after {seconds:.0f}s", file=sys.stderr)
            if done % 100 == 0 or done == len(todo):
                print(
                    f"classes: {done}/{len(todo)} in {time.monotonic() - started:.0f}s {counts}",
                    file=sys.stderr,
                )
    return 0 if counts.get("failed", 0) == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
