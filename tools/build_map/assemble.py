#!/usr/bin/env python3
"""Turns the records collect.py wrote into the compact map the selector reads.

Usage:
  assemble.py --tests /data/tests.json --iso /work/iso0 [...] --suite /work/suite0 [...] --out /out

Everything here comes from the pinned sqlglot commit: its source, and what its
own tests executed and opened. Nothing reads the sample commits or their
expected results.

Per sqlglot source file the map holds
  - the statement table from statements.py: the owner of each line, and each
    statement's last line, parent and whether it runs at import time
  - for every statement inside a function, the set of tests that executed any
    line of it, in either pass, and a flag when some test only executes it
    after other tests have run (see build_file)
  - the set of tests that executed any line of the file
  - the set of tests that opened the file as text instead of importing it
  - the lines that ran with no test context, during start-up, collection and
    module imports (recorded for inspection; the selector goes by the
    statement table to decide what runs at import)
  - the file itself, byte for byte, so the selector can apply a patch to it
    and see whether the syntax tree changed
and for every file under tests/, the set of tests that opened or imported it.

Output directory:
  index.json      format, provenance, where each chunk and source is, a
                  fingerprint of each source's syntax tree, the width of a
                  test set
  files.bin       zlib-compressed JSON chunks: one per source file, plus
                  "#tests" (node IDs in bit order) and "#opens" (tests/ paths)
  sets.bin        zlib-compressed table of the distinct test sets, each a
                  fixed-width little-endian bit mask (bit n = n-th node ID)
  sources.bin     every sqlglot/**/*.py at the pinned commit, each compressed
                  with zlib on its own
  sqlglot-LICENSE sqlglot's MIT licence, which has to travel with its source
  summary.json    what the build measured, for people
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
import platform
import sys
import warnings
import zlib
from importlib import metadata
from pathlib import Path

from statements import statement_table

REPO = Path("/sqlglot")
FORMAT = 2


def from_hex(masks: dict[str, str]) -> dict[str, int]:
    return {file: int(mask, 16) for file, mask in masks.items()}


def merge_into(target: dict[str, int], masks: dict[str, int]) -> None:
    for file, mask in masks.items():
        target[file] = target.get(file, 0) | mask


def count_bits(masks: dict[str, int]) -> int:
    return sum(mask.bit_count() for mask in masks.values())


def ranges(mask: int) -> list[list[int]]:
    """A line mask as [first, last] runs of consecutive lines."""
    runs: list[list[int]] = []
    line = 0
    while mask >> line:
        if mask >> line & 1:
            start = line
            while mask >> line & 1:
                line += 1
            runs.append([start, line - 1])
        else:
            line += 1
    return runs


def load_isolated(directories: list[Path], tests: list[dict]) -> dict[int, dict]:
    """Per test number, the union of its records over the isolated passes."""
    merged: dict[int, dict] = {}
    for test in tests:
        entry = {"lines": {}, "before": {}, "child_only": {}, "opens": set(), "outcomes": []}
        entry.update(seconds=[], unreadable=[])
        for directory in directories:
            path = directory / f"{test['number']:04d}" / "result.json"
            if not path.exists():
                entry["outcomes"].append("no record")
                continue
            record = json.loads(path.read_text("utf-8"))
            if record["id"] != test["id"]:
                raise SystemExit(f"{path} is for {record['id']}, not {test['id']}")
            merge_into(entry["lines"], from_hex(record["lines"]))
            merge_into(entry["before"], from_hex(record["before_test"]))
            merge_into(entry["child_only"], from_hex(record["child_only"]))
            entry["opens"].update(tuple(item) for item in record["opens"])
            entry["outcomes"].append(record["outcome"])
            entry["seconds"].append(record["seconds"])
            entry["unreadable"].extend(record["unreadable"])
        merged[test["number"]] = entry
    return merged


def load_suite(directories: list[Path], tests: list[dict]) -> dict:
    """Per test number, what it ran in the whole-suite passes.

    Class set-up runs inside the first test of a class and class clean-up inside
    the last, so set-up and teardown lines are shared by the whole class.
    """
    number_of = {test["id"]: test["number"] for test in tests}
    lines: dict[int, dict[str, int]] = {test["number"]: {} for test in tests}
    opens: dict[int, set] = {test["number"]: set() for test in tests}
    before: dict[str, int] = {}
    children: dict[str, int] = {}
    outcomes: list[dict[str, str]] = []
    unknown_contexts: set[str] = set()
    for directory in directories:
        record = json.loads((directory / "suite.json").read_text("utf-8"))
        outcomes.append(record["outcomes"])
        merge_into(before, from_hex(record["before_tests"]))
        merge_into(children, from_hex(record["children"]))
        shared: dict[str, dict[str, int]] = {}
        for test_id, phases in record["tests"].items():
            if test_id not in number_of:
                unknown_contexts.add(test_id)
                continue
            for phase, masks in phases.items():
                merge_into(lines[number_of[test_id]], from_hex(masks))
                if phase != "run":
                    merge_into(shared.setdefault(test_id.rsplit("::", 1)[0], {}), from_hex(masks))
        for test in tests:
            merge_into(lines[test["number"]], shared.get(test["id"].rsplit("::", 1)[0], {}))
        for test_id, found in record["opens"].items():
            if test_id in number_of:
                opens[number_of[test_id]].update(tuple(item) for item in found)
    return {
        "lines": lines,
        "opens": opens,
        "before": before,
        "children": children,
        "outcomes": outcomes,
        "unknown_contexts": sorted(unknown_contexts),
    }


class SetTable:
    """Distinct test sets, each stored once. A set is an int with bit n for test n."""

    def __init__(self) -> None:
        self.index: dict[int, int] = {}
        self.sets: list[int] = []

    def add(self, tests: int) -> int:
        if tests == 0:
            return -1
        if tests not in self.index:
            self.index[tests] = len(self.sets)
            self.sets.append(tests)
        return self.index[tests]


IN_FUNCTION, IMPORT_TIME, ORDER_DEPENDENT = 0, 1, 2
COUNTS = ("statements", "import_time", "in_function", "in_function_executed", "order_dependent")


def build_file(
    source: str, hits: list[tuple[int, int]], alone: dict[int, int], sets: SetTable
) -> dict:
    """The chunk for one source file.

    hits is (test number, line mask) for every test that ran the file in any
    pass, alone the same masks from the isolated passes only. Each statement
    becomes [first line, last line, parent's first line, flag, test set]:

      IN_FUNCTION      the set is the tests that executed any line of it
      IMPORT_TIME      it runs when the module is imported, so it matters to
                       every test that ran the file (no set of its own)
      ORDER_DEPENDENT  inside a function, but some test only executes it when
                       other tests ran first. Those tests are the first to
                       reach it after the state changed; later tests can rely
                       on what it did without executing it. Treated like
                       IMPORT_TIME. The set is kept for inspection.
    """
    table = statement_table(source)
    parents: dict[int, int] = {}
    stack: list[tuple[int, int]] = []
    statements = []
    counts = dict.fromkeys(COUNTS, 0)
    for start in sorted(table["spans"]):
        end = table["spans"][start]
        while stack and stack[-1][1] < start:
            stack.pop()
        parents[start] = stack[-1][0] if stack else 0
        stack.append((start, end))
        counts["statements"] += 1
        if start in table["import_time"]:
            counts["import_time"] += 1
            statements.append([start, end, parents[start], IMPORT_TIME, -1])
            continue
        span = ((1 << (end + 1)) - 1) ^ ((1 << start) - 1)
        executed_by = executed_alone_by = 0
        for number, mask in hits:
            if mask & span:
                executed_by |= 1 << number
                if alone.get(number, 0) & span:
                    executed_alone_by |= 1 << number
        flag = IN_FUNCTION if executed_by == executed_alone_by else ORDER_DEPENDENT
        counts["in_function"] += 1
        counts["in_function_executed"] += executed_by != 0
        counts["order_dependent"] += flag == ORDER_DEPENDENT
        statements.append([start, end, parents[start], flag, sets.add(executed_by)])
    chunk = {"lines": table["lines"], "owner": table["owner"], "statements": statements}
    return {"chunk": chunk, "counts": counts}


def pack(value: object) -> bytes:
    return json.dumps(value, separators=(",", ":")).encode("utf-8")


def concatenate(parts: dict[str, bytes]) -> tuple[bytes, dict[str, list[int]]]:
    """Each part compressed on its own, end to end, and where each one sits."""
    offsets: dict[str, list[int]] = {}
    blob = bytearray()
    for name in sorted(parts):
        packed = zlib.compress(parts[name], 9)
        offsets[name] = [len(blob), len(packed)]
        blob += packed
    return bytes(blob), offsets


def tree_digest(source: bytes) -> str:
    """A fingerprint of a module's syntax tree with positions left out.

    The selector computes the same thing for a patched file (see syntax_tree and
    tree_digest in selector.py) and compares. ast.dump's text can differ between
    Python versions, so the index records which Python made these.
    """
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            tree = ast.dump(ast.parse(source))
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        return ""  # never equal to a real fingerprint
    return hashlib.sha256(tree.encode("utf-8")).hexdigest()


def write_map(
    out: Path,
    chunks: dict[str, bytes],
    sets: SetTable,
    count: int,
    provenance: dict,
    sources: dict[str, bytes],
) -> None:
    """Write index.json and the three .bin files. The same input gives the same bytes."""
    width = (count + 7) // 8
    out.mkdir(parents=True, exist_ok=True)
    table = b"".join(tests.to_bytes(width, "little") for tests in sets.sets)
    (out / "sets.bin").write_bytes(zlib.compress(table, 9))
    files, chunk_offsets = concatenate(chunks)
    (out / "files.bin").write_bytes(files)
    packed_sources, source_offsets = concatenate(sources)
    (out / "sources.bin").write_bytes(packed_sources)
    index = {
        "format": FORMAT,
        **provenance,
        "tests": count,
        "set_bytes": width,
        "sets": len(sets.sets),
        "chunks": chunk_offsets,
        # path -> [offset, length, fingerprint of the file's syntax tree]
        "sources": {
            name: [*where, tree_digest(sources[name])] for name, where in source_offsets.items()
        },
        "tree_python": ".".join(str(part) for part in sys.version_info[:3]),
    }
    (out / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", "utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--tests", required=True)
    parser.add_argument("--iso", nargs="+", required=True, type=Path)
    parser.add_argument("--suite", nargs="*", default=[], type=Path)
    parser.add_argument("--out", required=True, type=Path)
    options = parser.parse_args()

    tests = json.loads(Path(options.tests).read_text("utf-8"))["tests"]
    if [test["number"] for test in tests] != list(range(len(tests))):
        raise SystemExit("test numbers in tests.json are not 0..N-1 in order")
    count = len(tests)
    isolated = load_isolated(options.iso, tests)
    suite = load_suite(options.suite, tests)

    combined: dict[int, dict[str, int]] = {}
    suite_only: dict[int, dict[str, int]] = {}
    for number in range(count):
        alone, together = isolated[number]["lines"], suite["lines"][number]
        merged = dict(alone)
        merge_into(merged, together)
        combined[number] = merged
        extra = {file: mask & ~alone.get(file, 0) for file, mask in together.items()}
        suite_only[number] = {file: mask for file, mask in extra.items() if mask}

    sources = sorted(
        str(path.relative_to(REPO)) for path in (REPO / "sqlglot").rglob("*.py") if path.is_file()
    )
    digest = hashlib.sha256()
    sets = SetTable()
    chunks: dict[str, bytes] = {}
    totals = dict.fromkeys(COUNTS, 0)
    order_dependent: dict[str, list[int]] = {}
    readers: dict[str, int] = {}
    opened: dict[str, int] = {}
    elsewhere: dict[str, int] = {}
    imported: dict[int, set[str]] = {number: set() for number in range(count)}
    for number in range(count):
        for kind, path in isolated[number]["opens"] | suite["opens"][number]:
            if not (REPO / path).is_file():
                continue  # a directory-relative open from temp-dir clean-up, not a repo file
            if kind == "I":
                imported[number].add(path)
            if path.startswith("tests/"):
                opened[path] = opened.get(path, 0) | 1 << number
            elif path.startswith("sqlglot/") and path.endswith(".py"):
                if kind == "R":
                    readers[path] = readers.get(path, 0) | 1 << number
            else:
                elsewhere[path] = elsewhere.get(path, 0) | 1 << number

    no_test_context = dict(suite["before"])
    for number in range(count):
        merge_into(no_test_context, isolated[number]["before"])

    imported_without_lines = 0
    lines_without_import = 0
    source_bytes: dict[str, bytes] = {}
    for file in sources:
        source_bytes[file] = (REPO / file).read_bytes()
        source = source_bytes[file].decode("utf-8")
        digest.update(file.encode("utf-8") + b"\0" + source_bytes[file] + b"\0")
        hits = [(n, combined[n][file]) for n in range(count) if combined[n].get(file)]
        alone = {n: isolated[n]["lines"].get(file, 0) for n, _ in hits}
        built = build_file(source, hits, alone, sets)
        late = [row[0] for row in built["chunk"]["statements"] if row[3] == ORDER_DEPENDENT]
        if late:
            order_dependent[file] = late
        # "Ran any line of the file" also counts a test that imported it. Importing
        # executes the top level, so the two records should agree; count where they don't.
        any_line = 0
        for number in range(count):
            ran, loaded = bool(combined[number].get(file)), file in imported[number]
            if ran or loaded:
                any_line |= 1 << number
            imported_without_lines += loaded and not ran and built["counts"]["statements"] > 0
            lines_without_import += ran and not loaded
        built["chunk"]["any_line"] = sets.add(any_line)
        built["chunk"]["readers"] = sets.add(readers.get(file, 0))
        built["chunk"]["no_test_context"] = ranges(no_test_context.get(file, 0))
        for key, value in built["counts"].items():
            totals[key] += value
        chunks[file] = pack(built["chunk"])

    chunks["#tests"] = pack([test["id"] for test in tests])
    chunks["#opens"] = pack({path: sets.add(tests_) for path, tests_ in sorted(opened.items())})
    width = (count + 7) // 8
    provenance = {
        "sqlglot_commit": os.environ.get("SQLGLOT_COMMIT", "unknown"),
        "sources_sha256": digest.hexdigest(),
    }
    write_map(options.out, chunks, sets, count, provenance, source_bytes)
    (options.out / "sqlglot-LICENSE").write_bytes((REPO / "LICENSE").read_bytes())

    not_passed = {
        tests[n]["id"]: isolated[n]["outcomes"]
        for n in range(count)
        if set(isolated[n]["outcomes"]) != {"passed"}
    }
    suite_not_passed = sorted(
        {t["id"] for t in tests for o in suite["outcomes"] if o.get(t["id"]) != "passed"}
    )
    empty = [tests[n]["id"] for n in range(count) if not combined[n]]
    with_children = {
        tests[n]["id"]: count_bits(isolated[n]["child_only"])
        for n in range(count)
        if count_bits(isolated[n]["child_only"])
    }
    union_alone: dict[str, int] = {}
    for number in range(count):
        merge_into(union_alone, isolated[number]["lines"])
    union_all: dict[str, int] = {}
    for number in range(count):
        merge_into(union_all, combined[number])
    suite_children_unattributed = {
        file: (mask & ~union_alone.get(file, 0)).bit_count()
        for file, mask in suite["children"].items()
        if mask & ~union_alone.get(file, 0)
    }
    suite_start_unattributed = {
        file: (mask & ~union_all.get(file, 0)).bit_count()
        for file, mask in suite["before"].items()
        if mask & ~union_all.get(file, 0)
    }
    suite_only_by_file: dict[str, int] = {}
    for extra in suite_only.values():
        for file, mask in extra.items():
            suite_only_by_file[file] = suite_only_by_file.get(file, 0) + mask.bit_count()
    in_image = ("files.bin", "sets.bin", "sources.bin", "index.json")
    sizes = {name: (options.out / name).stat().st_size for name in in_image}
    summary = {
        "built_with": {
            "python": platform.python_version(),
            "coverage": metadata.version("coverage"),
            "pytest": metadata.version("pytest"),
            "isolated_passes": len(options.iso),
            "suite_passes": len(options.suite),
        },
        "tests": {
            "in_tests_json": count,
            "with_a_record_in_every_isolated_pass": sum(
                "no record" not in isolated[n]["outcomes"] for n in range(count)
            ),
            "not_passed_when_run_alone": not_passed,
            "not_passed_in_suite": suite_not_passed,
            "suite_contexts_not_in_tests_json": suite["unknown_contexts"],
            "with_no_sqlglot_line": empty,
            "with_unreadable_data_files": {
                tests[n]["id"]: isolated[n]["unreadable"]
                for n in range(count)
                if isolated[n]["unreadable"]
            },
            "slowest_alone_seconds": sorted(
                ((max(isolated[n]["seconds"], default=0), tests[n]["id"]) for n in range(count)),
                reverse=True,
            )[:5],
        },
        "child_processes": {
            "tests_with_lines_only_a_child_ran": len(with_children),
            "lines_only_a_child_ran": with_children,
            "suite_child_lines_no_isolated_test_ran": suite_children_unattributed,
        },
        "passes_compared": {
            "test_lines_alone": sum(count_bits(isolated[n]["lines"]) for n in range(count)),
            "test_lines_only_in_suite": sum(count_bits(extra) for extra in suite_only.values()),
            "tests_with_lines_only_in_suite": sum(1 for extra in suite_only.values() if extra),
            "lines_only_in_suite_by_file": dict(
                sorted(suite_only_by_file.items(), key=lambda item: -item[1])[:25]
            ),
            "suite_start_up_lines_no_test_ran": suite_start_unattributed,
        },
        "imports": {
            "imported_but_no_line_recorded": imported_without_lines,
            "lines_recorded_but_no_import_seen": lines_without_import,
        },
        "source_files": {
            "count": len(sources),
            "bytes": sum(len(data) for data in source_bytes.values()),
            "lines": sum(len(data.decode("utf-8").splitlines()) for data in source_bytes.values()),
            "executed_by_some_test": sum(1 for file in sources if union_all.get(file)),
            "lines_executed_by_some_test": count_bits(union_all),
            "lines_executed_with_no_test_context": count_bits(no_test_context),
            "read_as_text_by_tests": {
                file: tests_.bit_count() for file, tests_ in sorted(readers.items())
            },
        },
        "statements": totals,
        "order_dependent_statements": order_dependent,
        "files_under_tests_opened": {p: t.bit_count() for p, t in sorted(opened.items())},
        "other_repo_files_opened": {p: t.bit_count() for p, t in sorted(elsewhere.items())},
        "sets": {"distinct": len(sets.sets), "bytes_each": width},
        "bytes": {**sizes, "total": sum(sizes.values())},
    }
    (options.out / "summary.json").write_text(json.dumps(summary, indent=1, sort_keys=True) + "\n")
    print(
        json.dumps({key: summary[key] for key in ("statements", "sets", "bytes")}), file=sys.stderr
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
