"""Summary of a runner self-check, and map/never_verify.json from it.

Usage: selfcheck_report.py <selfcheck.json> <never_verify.json, or - to write none>

The self-check (runner.py selfcheck) ran every test alone on the unpatched
code in the scored image. This prints what it measured and writes the list of
tests the runner must never judge by running them, with the reasons.
"""

import json
import sys
from pathlib import Path

CHEAP = 0.05  # recorded seconds below which a test counts as cheap here


def median(values: list[float]) -> float:
    ordered = sorted(values)
    return ordered[len(ordered) // 2] if ordered else float("nan")


def main(report_path: str, target: str) -> int:
    report = json.loads(Path(report_path).read_text("utf-8"))
    tests, never = report["tests"], report["never_verify"]
    startup, forks = report["startup"], report["forks"]

    print(f"python {report['python']} | flags {report['flags']}")
    print(
        f"parent start-up: {startup['process_cpu']:.3f} s CPU in the process,"
        f" {startup['cgroup_cpu']} s for the container so far, {startup['seconds']} s wall"
        f" | {startup['threads']} thread(s), {startup['rss_mb']} MB,"
        f" {startup['sqlglot_modules']} sqlglot modules loaded"
    )
    print(
        f"empty fork: {1000 * (forks['cgroup_cpu_each'] or 0):.2f} ms CPU each"
        f" | empty job through the runner: {1000 * (forks['cgroup_cpu_each_empty_job'] or 0):.2f}"
        f" ms CPU each ({forks['count']} of each)"
    )
    print(f"whole pass: {len(tests)} tests, {report['whole_pass']}")

    letters: dict[str, int] = {}
    for record in tests.values():
        letters[str(record["letter"])] = letters.get(str(record["letter"]), 0) + 1
    print(f"outcomes (None is a run that did not end normally): {dict(sorted(letters.items()))}")
    print(f"subtests run: {sum(record.get('subtests', 0) for record in tests.values())}")

    print()
    print(f"NEVER_VERIFY: {len(never)} of {len(tests)} tests")
    kinds: dict[str, list[str]] = {}
    for name, reasons in never.items():
        for reason in reasons:
            kinds.setdefault(reason.split(":")[0].split(" (")[0], []).append(name)
    for kind, names in sorted(kinds.items(), key=lambda item: -len(item[1])):
        seconds = sum(tests[name]["recorded_seconds"] for name in names)
        print(f"  {len(names):4d} tests, {seconds:7.2f} recorded s: {kind}")
    recorded = sum(tests[name]["recorded_seconds"] for name in never)
    total = sum(record["recorded_seconds"] for record in tests.values())
    print(f"  they hold {recorded:.1f} of the {total:.1f} recorded seconds")

    print()
    print("runner CPU against recorded seconds, for tests that passed and may be verified:")
    usable = {
        name: record
        for name, record in tests.items()
        if record["letter"] == "P" and name not in never
    }
    bands = [(0, 0.005), (0.005, 0.02), (0.02, CHEAP), (CHEAP, 0.2), (0.2, 0.5), (0.5, 1e9)]
    print("  recorded s      tests  recorded  child CPU  test CPU  child/rec  median ratio")
    for low, high in bands:
        rows = [r for r in usable.values() if low <= r["recorded_seconds"] < high]
        if not rows:
            continue
        rec = sum(r["recorded_seconds"] for r in rows)
        child = sum(r["cpu"] for r in rows)
        inner = sum(r.get("test_cpu", 0.0) for r in rows)
        ratios = [r["cpu"] / r["recorded_seconds"] for r in rows if r["recorded_seconds"] > 0]
        label = f"{low:g} to {high:g}" if high < 1e9 else f"{low:g} and up"
        print(
            f"  {label:<14} {len(rows):6d} {rec:9.2f} {child:10.2f} {inner:9.2f}"
            f" {child / rec if rec else float('nan'):10.2f} {median(ratios):13.2f}"
        )
    cheap = [r for r in usable.values() if r["recorded_seconds"] < CHEAP]
    rec = sum(r["recorded_seconds"] for r in cheap)
    child = sum(r["cpu"] for r in cheap)
    print(
        f"  cheap tests (recorded under {CHEAP} s): {len(cheap)} tests, {rec:.2f} recorded s,"
        f" {child:.2f} s child CPU: ratio {child / rec:.2f},"
        f" median child CPU {1000 * median([r['cpu'] for r in cheap]):.1f} ms"
    )

    lazy: dict[str, int] = {}
    own = 0
    for record in tests.values():
        for name in record.get("imported", []):
            top = name.split(".")[0]
            if top in ("sqlglot", "tests"):
                own += top == "sqlglot"
            else:
                lazy[name] = lazy.get(name, 0) + 1
    print()
    print(f"sqlglot modules first imported inside a test, over all tests: {own}")
    common = sorted(lazy.items(), key=lambda item: -item[1])[:25]
    print(f"other modules first imported inside a test (module: tests): {dict(common)}")

    if target != "-":
        document = {
            "what": "Tests the runner never judges by running them. Made by scripts/selfcheck.sh.",
            "python": report["python"],
            "tests": never,
        }
        Path(target).write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", "utf-8")
        print(f"\nwrote {target}: {len(never)} tests")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1], sys.argv[2]))
