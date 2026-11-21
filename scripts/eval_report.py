#!/usr/bin/env python3
"""Per-commit table and CHECK line for scripts/eval.sh.

Usage: eval_report.py <eval_out_dir> <runs>

score.py is the only arbiter of cost here. Each per-commit number comes from
its evaluate_run on that commit's line alone, and the per-commit sum is checked
against the total score.py printed for the whole run. This script and score.py
are the only code that reads sample/answers.json.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import score  # noqa: E402  the frozen scorer at the repo root

SAMPLE = ROOT / "sample"
SEARCH_NODE_BUDGET = 2_000_000
SEARCH_ITEM_LIMIT = 200


class Failure(Exception):
    pass


def parse_fixed(text: str, decimals: int) -> int:
    """'1210.470' with decimals=3 is 1210470, without going through a float."""
    whole, _, fraction = text.partition(".")
    if len(fraction) != decimals or not (whole + fraction).isdigit():
        raise Failure(f"expected {decimals} decimals in {text!r}")
    return int(whole) * 10**decimals + int(fraction)


def format_fixed(value: int, decimals: int) -> str:
    return f"{value // 10**decimals}.{value % 10**decimals:0{decimals}d}"


def read_score(path: Path) -> dict:
    """The numbers score.py printed for one run."""
    fields: dict = {"missed": []}
    for line in path.read_text("utf-8").splitlines():
        words = line.split()
        if line.startswith("Simulated:"):
            fields["simulated_ms"] = parse_fixed(words[1], 3)
        elif line.startswith("Misses:"):
            fields["misses"] = int(words[1])
        elif line.startswith("  missed "):
            fields["missed"].append(words[1])
        elif line.startswith("CPU:"):
            fields["cpu_us"] = parse_fixed(words[1], 6)
        elif line.startswith("Total:"):
            fields["total_us"] = parse_fixed(words[1], 6)
    absent = {"simulated_ms", "misses", "cpu_us", "total_us"} - fields.keys()
    if absent or fields["misses"] != len(fields["missed"]):
        raise Failure(f"could not read score.py's report in {path.name}")
    return fields


def line_cost(
    commit: str, workers: list[list[int]], tests: list[dict], failing: list[int]
) -> tuple[int, bool]:
    """(cost in ms without the miss penalty, missed) for one commit, as score.py counts it."""
    line = json.dumps({"commit": commit, "workers": workers}).encode("utf-8") + b"\n"
    result = score.evaluate_run(line, tests, {commit: {"failing": failing}})
    if not result["ok"]:
        raise Failure(f"score.py rejected the line for {commit}: {result['code']}")
    missed = bool(result["missed_commits"])
    return result["simulated_milliseconds"] - score.MISS_PENALTY_MILLISECONDS * missed, missed


class _OutOfBudget(Exception):
    pass


def smallest_largest_load(items: list[tuple[int, int]], count: int) -> tuple[list[list[int]], bool]:
    """Split (test, ms) items, longest first, over count workers with the smallest
    possible largest load. Returns the workers and whether that is proven."""
    lower_bound = max(items[0][1], -(-sum(ms for _, ms in items) // count))
    loads = [0] * count
    workers: list[list[int]] = [[] for _ in range(count)]
    for test, ms in items:
        lightest = loads.index(min(loads))
        loads[lightest] += ms
        workers[lightest].append(test)
    best_load, best_workers = max(loads), [list(worker) for worker in workers]
    if best_load == lower_bound:
        return best_workers, True
    if len(items) > SEARCH_ITEM_LIMIT:
        return best_workers, False

    loads = [0] * count
    workers = [[] for _ in range(count)]
    nodes = 0

    def place(index: int) -> None:
        nonlocal best_load, best_workers, nodes
        if best_load == lower_bound:
            return
        if index == len(items):
            best_load, best_workers = max(loads), [list(worker) for worker in workers]
            return
        nodes += 1
        if nodes > SEARCH_NODE_BUDGET:
            raise _OutOfBudget
        test, ms = items[index]
        tried: set[int] = set()
        for worker in range(count):
            if loads[worker] in tried or loads[worker] + ms >= best_load:
                continue
            tried.add(loads[worker])
            loads[worker] += ms
            workers[worker].append(test)
            place(index + 1)
            workers[worker].pop()
            loads[worker] -= ms

    try:
        place(0)
    except _OutOfBudget:
        return best_workers, False
    return best_workers, True


def oracle_workers(failing: list[int], duration_ms: dict[int, int]) -> tuple[list[list[int]], bool]:
    """The cheapest workers that hold exactly the failing tests, and whether that is proven."""
    if not failing:
        return [], True
    items = sorted(
        ((test, duration_ms[test]) for test in failing), key=lambda item: (-item[1], item[0])
    )
    best_cost: int | None = None
    best: list[list[int]] = []
    all_proven = True
    for count in range(1, min(score.MAXIMUM_WORKERS, len(items)) + 1):
        fee = score.WORKER_COST_MILLISECONDS * count
        if best_cost is not None and items[0][1] + fee >= best_cost:
            break  # more workers cannot pay for themselves from here on
        workers, proven = smallest_largest_load(items, count)
        all_proven = all_proven and proven
        cost = max(sum(duration_ms[test] for test in worker) for worker in workers) + fee
        if best_cost is None or cost < best_cost:
            best_cost, best = cost, workers
    return best, all_proven


def main(arguments: list[str]) -> int:
    out, run_count = Path(arguments[0]), int(arguments[1])
    tests = json.loads((SAMPLE / "tests.json").read_text("utf-8"))["tests"]
    answers = json.loads((SAMPLE / "answers.json").read_text("utf-8"))["commits"]
    # Only used to search for the oracle split; its cost is then read back from evaluate_run.
    duration_ms = {test["number"]: score._duration_milliseconds(test["seconds"]) for test in tests}

    outputs = [(out / f"run{run}.jsonl").read_bytes() for run in range(1, run_count + 1)]
    if len(set(outputs)) != 1:
        raise Failure("the runs printed different output for the same input")
    runs = [read_score(out / f"run{run}.score") for run in range(1, run_count + 1)]

    rows = []
    for line in outputs[0].decode("utf-8").splitlines():
        entry = json.loads(line)
        commit, workers = entry["commit"], entry["workers"]
        failing = answers[commit]["failing"]
        cost, missed = line_cost(commit, workers, tests, failing)
        best, proven = oracle_workers(failing, duration_ms)
        oracle, oracle_missed = line_cost(commit, best, tests, failing)
        if oracle_missed:
            raise Failure(f"the oracle split for {commit} leaves out a failing test")
        rows.append(
            {
                "commit": commit,
                "selected": sum(len(worker) for worker in workers),
                "workers": sum(1 for worker in workers if worker),
                "cost": cost,
                "oracle": oracle,
                "proven": proven,
                "failing": len(failing),
                "missed": missed,
            }
        )
    rows.sort(key=lambda row: row["commit"])

    table_total = sum(row["cost"] + score.MISS_PENALTY_MILLISECONDS * row["missed"] for row in rows)
    if table_total != runs[0]["simulated_ms"]:
        raise Failure("the per-commit table does not add up to score.py's simulated total")
    if sorted(row["commit"] for row in rows if row["missed"]) != runs[0]["missed"]:
        raise Failure("the per-commit table disagrees with score.py about the misses")

    header = ("commit", "selected", "workers", "cost_s", "oracle_s", "gap_s", "failing", "miss")
    lines = []
    for row in rows:
        lines.append(
            (
                row["commit"],
                str(row["selected"]),
                str(row["workers"]),
                format_fixed(row["cost"], 3),
                format_fixed(row["oracle"], 3) + ("" if row["proven"] else "?"),
                format_fixed(row["cost"] - row["oracle"], 3),
                str(row["failing"]),
                "MISS" if row["missed"] else "no",
            )
        )
    cost_sum = sum(row["cost"] for row in rows)
    oracle_sum = sum(row["oracle"] for row in rows)
    misses = sum(row["missed"] for row in rows)
    lines.append(
        (
            "total",
            str(sum(row["selected"] for row in rows)),
            "",
            format_fixed(cost_sum, 3),
            format_fixed(oracle_sum, 3),
            format_fixed(cost_sum - oracle_sum, 3),
            str(sum(row["failing"] for row in rows)),
            str(misses),
        )
    )
    (out / "per_commit.tsv").write_text(
        "".join("\t".join(cells) + "\n" for cells in [header, *lines]), "utf-8"
    )
    widths = [
        max(len(cells[column]) for cells in [header, *lines]) for column in range(len(header))
    ]
    for cells in [header, *lines]:
        padded = (cell.rjust(width) for cell, width in zip(cells, widths, strict=True))
        print("  ".join(padded), file=sys.stderr)
    print(
        f"cost {format_fixed(cost_sum, 3)} s + {misses} x 300 s"
        f" = simulated {format_fixed(table_total, 3)} s"
        f" | oracle {format_fixed(oracle_sum, 3)} s ('?' marks a split not proven optimal)",
        file=sys.stderr,
    )

    by_total = sorted(runs, key=lambda fields: fields["total_us"])
    middle = by_total[len(by_total) // 2]
    cpu_median = sorted(fields["cpu_us"] for fields in runs)[len(runs) // 2]
    board = (middle["total_us"] + 50_000) // 100_000
    print(
        f"cpu per run: {', '.join(format_fixed(fields['cpu_us'], 6) for fields in runs)} s"
        f" | board score (middle total, rounded to a tenth): {board // 10}.{board % 10}",
        file=sys.stderr,
    )
    print(
        f"CHECK simulated={format_fixed(middle['simulated_ms'], 3)} misses={middle['misses']}"
        f" cpu_median={format_fixed(cpu_median, 6)}"
        f" total_median={format_fixed(middle['total_us'], 6)}"
        f" missed_commits=[{','.join(middle['missed'])}]"
    )
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main(sys.argv[1:]))
    except Failure as failure:
        print(f"eval: FAILED: {failure}", file=sys.stderr)
        print(f"CHECK FAILED {failure}")
        sys.exit(1)
