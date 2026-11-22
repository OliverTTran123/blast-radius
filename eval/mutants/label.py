#!/usr/bin/env python3
"""Labels a mutant: which tests fail when the pinned sqlglot carries its edit.

Runs inside the labeling image (eval/mutants/Dockerfile) with the repository
mounted read-only at /repo and a scratch directory at /out:

    label.py run <id>        label one mutant from manifest.json
    label.py run --base N    the unmutated code, which has to come out clean
    label.py collect <dir>   gather the labels into <dir>/answers.json and labels.json

A run copies the pinned checkout, makes the mutant's one-line edit as
manifest.json describes it (not by applying the diff, so a badly written diff
cannot hide; the edited file's sha256 must match the manifest), and runs the
whole suite the way README.md says the organizers did: the lock file's
packages, the checkout on PYTHONPATH, SKIP_INTEGRATION=0. pytest runs with
-n 4, and a test that hangs is failed after TIMEOUT seconds.

What counts as failing is README.md's definition: pytest reports a failure, an
error or a timeout for the test, one of its subtests fails, or the test's file
cannot be imported. See label_plugin.py.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path("/repo")
HERE = REPO / "eval" / "mutants"
PINNED = Path("/sqlglot")
TIMEOUT = 300  # seconds for one test; the slowest takes about 30
PYTEST = ["-n", "4", "-q", "-p", "label_plugin", "-p", "no:cacheprovider"]
PYTEST += ["--timeout", str(TIMEOUT), "--continue-on-collection-errors"]


def edit(work: Path, entry: dict) -> None:
    """Make the mutant's edit in the working copy and check the result against the manifest."""
    path = work / entry["path"]
    lines = path.read_bytes().split(b"\n")
    index = entry["line"] - 1
    new = entry["new"].encode("utf-8")
    if entry["added"]:
        lines.insert(index, new)
    else:
        if lines[index] != entry["old"].encode("utf-8"):
            raise SystemExit(
                f"{entry['id']}: line {entry['line']} of {entry['path']} is not as recorded"
            )
        lines[index] = new
    data = b"\n".join(lines)
    if hashlib.sha256(data).hexdigest() != entry["sha256"]:
        raise SystemExit(f"{entry['id']}: the edited {entry['path']} does not match the manifest")
    path.write_bytes(data)


def run(options: argparse.Namespace) -> int:
    name = f"base{options.base}" if options.base else options.id
    out = Path(options.out)
    tests = json.loads((REPO / "sample" / "tests.json").read_text("utf-8"))["tests"]
    work = Path("/work")
    shutil.copytree(PINNED, work / "sqlglot")
    (work / "plugin").mkdir()
    shutil.copy(HERE / "label_plugin.py", work / "plugin" / "label_plugin.py")
    if not options.base:
        manifest = json.loads((HERE / "manifest.json").read_text("utf-8"))
        edit(work / "sqlglot", next(e for e in manifest["mutants"] if e["id"] == options.id))

    report = out / f"{name}.report.json"
    report.unlink(missing_ok=True)
    environment = dict(os.environ)
    environment.update(
        PYTHONPATH=f"{work / 'sqlglot'}:{work / 'plugin'}",
        SKIP_INTEGRATION="0",
        LABEL_OUT=str(report),
    )
    started = time.monotonic()
    with open(out / f"{name}.log", "wb") as log:
        status = subprocess.run(
            [sys.executable, "-m", "pytest", *PYTEST],
            cwd=work / "sqlglot",
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
        ).returncode
    seconds = time.monotonic() - started
    tail = (out / f"{name}.log").read_text("utf-8", "replace").strip().splitlines()[-1:]
    if not report.exists():
        raise SystemExit(f"{name}: pytest wrote no report (exit {status}): {tail}")

    seen = json.loads(report.read_text("utf-8"))
    number = {test["id"]: test["number"] for test in tests}
    failing = {number[node] for node in seen["failed"] if node in number}
    for node in seen["uncollected"]:  # a file, or a directory of files, that did not import
        failing.update(
            t["number"] for t in tests if t["file"] == node or t["file"].startswith(node + "/")
        )
    label = {
        "name": name,
        "failing": sorted(failing),
        "failed_but_not_in_tests_json": sorted(n for n in seen["failed"] if n not in number),
        "uncollected": seen["uncollected"],
        "tests_that_reported": seen["ran"],
        "pytest_exit_status": status,
        "seconds": round(seconds, 1),
        "summary": tail[0] if tail else "",
    }
    (out / f"{name}.json").write_text(json.dumps(label, indent=1) + "\n", "utf-8")
    print(f"{name}: {len(failing)} failing in {seconds:.0f} s | {label['summary']}")
    return 0


def collect(options: argparse.Namespace) -> int:
    """answers.json in the kit's own format, and labels.json with how each label was made."""
    out, target = Path(options.out), Path(options.directory)
    manifest = json.loads((target / "manifest.json").read_text("utf-8"))
    labels = {path.stem: json.loads(path.read_text("utf-8")) for path in sorted(out.glob("*.json"))}
    labels = {name: label for name, label in labels.items() if "failing" in label}
    missing = [entry["id"] for entry in manifest["mutants"] if entry["id"] not in labels]
    if missing:
        raise SystemExit(f"no label yet for {len(missing)} mutants: {missing[:8]}")
    baselines = {name: label for name, label in labels.items() if name.startswith("base")}
    answers = {
        entry["id"]: {"failing": labels[entry["id"]]["failing"], "kind": entry["kind"]}
        for entry in manifest["mutants"]
    }
    (target / "answers.json").write_text(json.dumps({"commits": answers}, indent=1) + "\n", "utf-8")
    facts = {
        "labeled_with": {"pytest": PYTEST, "containers_at_once": 4, "cpus_each": 4},
        "unmutated": baselines,
        "mutants": {entry["id"]: labels[entry["id"]] for entry in manifest["mutants"]},
    }
    (target / "labels.json").write_text(json.dumps(facts, indent=1, sort_keys=True) + "\n", "utf-8")
    broken = sum(1 for label in answers.values() if label["failing"])
    print(f"collected {len(answers)} labels: {broken} mutants break at least one test")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    one = modes.add_parser("run")
    one.add_argument("id", nargs="?")
    one.add_argument("--base", type=int, default=0, help="label the unmutated code as base<N>")
    one.add_argument("--out", default="/out")
    gather = modes.add_parser("collect")
    gather.add_argument("directory", help="eval/mutants, writable")
    gather.add_argument("--out", default="/out")
    options = parser.parse_args()
    if options.mode == "collect":
        return collect(options)
    if bool(options.id) == bool(options.base):
        parser.error("give a mutant id or --base N")
    return run(options)


if __name__ == "__main__":
    sys.exit(main())
