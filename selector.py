"""Static test selector for the Blast Radius track.

Reads /data/commits/*.patch and /data/tests.json and prints, for every patch,
the tests to run and how to split them over workers. It decides from a map
built ahead of time from the pinned sqlglot commit (map/, made by
tools/build_map): which tests executed each statement, which tests ran any
line of each file, and which tests opened each file under tests/. It never
runs sqlglot, and the kind of edit a patch makes is never looked at: only
where it is.

How a changed path is handled:

  sqlglot/**/*.py  Each changed or deleted old line goes to the innermost
                   statement that owns it and selects the tests that executed
                   that statement. A statement that runs at import time (module
                   level, a class body, a def header) selects every test that
                   ran any line of the file, and so does a statement that some
                   test only reaches after other tests have run. New lines
                   inserted between two old lines, and old lines that hold no
                   code, select for the code on both sides. Tests that read the
                   file as text are always selected.
  tests/**         Every test that opened or imported the file, and every test
                   whose own file it is.
  anything else    Every test. So does anything this program cannot place: a
                   new, deleted or renamed file, a path the map does not know,
                   a line beyond the mapped file, a patch it cannot parse. A
                   miss costs 300 s, so unknown means broad.

Only answers go to stdout. Reasons and warnings go to stderr.
"""

from __future__ import annotations

import json
import os
import re
import sys
import zlib

DATA = "/data"
MAP = os.path.join(os.path.dirname(os.path.abspath(__file__)), "map")
WORKER_MS = 2_000
MAX_WORKERS = 8
HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def log(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------- patches


class FileChange:
    """One edited file of a patch, as old-file line numbers.

    removed   old lines that were changed or deleted
    inserted  (line above, line below) around each run of purely new lines
    """

    __slots__ = ("path", "removed", "inserted")

    def __init__(self, path: str) -> None:
        self.path = path
        self.removed: list[int] = []
        self.inserted: list[tuple[int, int]] = []


def header_path(text: str, prefix: str) -> str | None:
    """The path in a '--- a/x' or '+++ b/x' line, or None when it names no plain path."""
    path = text.split("\t")[0].rstrip()
    if path == "/dev/null" or path.startswith('"'):
        return None
    return path[len(prefix) :] if path.startswith(prefix) else path


def parse_patch(text: str) -> tuple[list[FileChange] | None, str]:
    """The plain edits of a unified diff, or (None, reason) when it holds anything else."""
    lines = text.split("\n")
    changes: list[FileChange] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        if line.startswith("diff --git "):
            index += 1
            while index < len(lines) and not lines[index].startswith(("--- ", "diff --git ")):
                if lines[index] and not lines[index].startswith("index "):
                    return None, f"not a plain edit ({lines[index].strip()[:40]})"
                index += 1
            if index >= len(lines) or not lines[index].startswith("--- "):
                return None, "a file section without content"
            continue
        if not line.startswith("--- "):
            index += 1
            continue
        if index + 1 >= len(lines) or not lines[index + 1].startswith("+++ "):
            return None, "a file header without its second line"
        old, new = header_path(line[4:], "a/"), header_path(lines[index + 1][4:], "b/")
        if old is None or new is None or old != new:
            return None, "a new, deleted or renamed file"
        change = FileChange(old)
        index += 2
        while index < len(lines) and lines[index].startswith("@@"):
            match = HUNK.match(lines[index])
            if match is None:
                return None, "a hunk header that does not parse"
            old_left = 1 if match[2] is None else int(match[2])
            new_left = 1 if match[4] is None else int(match[4])
            # With no old lines the header names the line before the insertion.
            cursor = int(match[1]) if old_left else int(match[1]) + 1
            removed_run: list[int] = []
            added_run = False
            index += 1
            while index < len(lines) and (old_left or new_left):
                tag = lines[index][:1]
                if tag == "-":
                    removed_run.append(cursor)
                    cursor += 1
                    old_left -= 1
                elif tag == "+":
                    added_run = True
                    new_left -= 1
                elif tag in (" ", ""):
                    # Context ends a run. An empty line is context that lost its space.
                    if removed_run:
                        change.removed.extend(removed_run)
                    elif added_run:
                        change.inserted.append((cursor - 1, cursor))
                    removed_run, added_run = [], False
                    cursor += 1
                    old_left -= 1
                    new_left -= 1
                elif tag != "\\":  # "\ No newline at end of file" belongs to the line above
                    return None, "an unexpected line inside a hunk"
                index += 1
            if old_left or new_left:
                return None, "a hunk that does not match its header"
            if removed_run:
                change.removed.extend(removed_run)
            elif added_run:
                change.inserted.append((cursor - 1, cursor))
            if index < len(lines) and lines[index].startswith("\\"):
                index += 1
        if not change.removed and not change.inserted:
            return None, "a file section without hunks"
        changes.append(change)
    if not changes:
        return None, "no file changes found"
    return changes, ""


# ------------------------------------------------------------------------- map


class Map:
    """Lazy reader for map/ (the layout is described in tools/build_map/assemble.py)."""

    def __init__(self, directory: str) -> None:
        self.directory = directory
        with open(os.path.join(directory, "index.json"), encoding="utf-8") as handle:
            self.index = json.load(handle)
        if self.index["format"] != 1:
            raise ValueError(f"map format {self.index['format']} is not supported")
        with open(os.path.join(directory, "files.bin"), "rb") as handle:
            self.blob = handle.read()
        self.width: int = self.index["set_bytes"]
        self.table: bytes | None = None
        self.chunks: dict[str, object] = {}

    def chunk(self, name: str):
        """The parsed chunk of that name, or None when the map has none."""
        if name not in self.chunks:
            where = self.index["chunks"].get(name)
            if where is None:
                self.chunks[name] = None
            else:
                packed = self.blob[where[0] : where[0] + where[1]]
                self.chunks[name] = json.loads(zlib.decompress(packed))
        return self.chunks[name]

    def tests(self, set_id: int) -> int:
        """A test set as a bit mask over the map's own test order."""
        if set_id < 0:
            return 0
        if self.table is None:
            with open(os.path.join(self.directory, "sets.bin"), "rb") as handle:
                self.table = zlib.decompress(handle.read())
        start = set_id * self.width
        return int.from_bytes(self.table[start : start + self.width], "little")


class SourceFile:
    """One mapped sqlglot source file: line owners and statements."""

    def __init__(self, chunk: dict) -> None:
        self.lines: int = chunk["lines"]
        self.owner: list[int] = chunk["owner"]
        # first line -> (last line, parent's first line, flag, test set id). The flag is
        # 0 inside a function, 1 when the statement runs at import time, 2 when some
        # test only reaches it after other tests ran. Non-zero means the whole file's tests.
        self.statements = {row[0]: (row[1], row[2], row[3], row[4]) for row in chunk["statements"]}
        self.any_line: int = chunk["any_line"]
        self.readers: int = chunk["readers"]

    def enclosing(self, line: int) -> list[int]:
        """The statements around a line of code, innermost first."""
        chain = []
        statement = self.owner[line]
        while statement:
            chain.append(statement)
            statement = self.statements[statement][1]
        return chain


def bits(mask: int) -> list[int]:
    positions = []
    while mask:
        lowest = mask & -mask
        positions.append(lowest.bit_length() - 1)
        mask ^= lowest
    return positions


# -------------------------------------------------------------------- selector


class Selector:
    def __init__(self, directory: str, tests: list[dict]) -> None:
        self.map = Map(directory)
        self.everything = (1 << len(tests)) - 1
        self.sources: dict[str, SourceFile | None] = {}
        self.opens: dict[str, int] = self.map.chunk("#opens")
        self.own_file: dict[str, int] = {}
        for position, test in enumerate(tests):
            self.own_file[test["file"]] = self.own_file.get(test["file"], 0) | 1 << position

        # The map numbers tests in its own order. Go by node ID, so a tests.json
        # that numbers them differently still selects the right tests.
        map_ids: list[str] = self.map.chunk("#tests")
        ids = [test["id"] for test in tests]
        self.same_order = map_ids == ids
        position_of = {test_id: position for position, test_id in enumerate(ids)}
        self.position = [position_of.get(test_id, -1) for test_id in map_ids]
        known = set(map_ids)
        # A test the map has never seen could fail on anything, so it always runs.
        self.unmapped = sum(1 << position_of[test_id] for test_id in ids if test_id not in known)
        if self.unmapped:
            log(f"{len(bits(self.unmapped))} tests are not in the map and are always selected")

    def tests(self, set_id: int) -> int:
        """A set from the map as a bit mask over this run's tests."""
        mask = self.map.tests(set_id)
        if self.same_order:
            return mask
        return sum(1 << self.position[bit] for bit in bits(mask) if self.position[bit] >= 0)

    def source(self, path: str) -> SourceFile | None:
        if path not in self.sources:
            chunk = self.map.chunk(path)
            self.sources[path] = SourceFile(chunk) if chunk is not None else None
        return self.sources[path]

    def select(self, commit: str, text: str) -> int:
        """The tests to run for one patch, as a bit mask over this run's tests."""
        changes, reason = parse_patch(text)
        if changes is None:
            log(f"{commit}: every test: the patch holds {reason}")
            return self.everything
        selected = self.unmapped
        for change in changes:
            part = self.for_change(commit, change)
            if part is None:
                return self.everything
            selected |= part
        return selected

    def for_change(self, commit: str, change: FileChange) -> int | None:
        """The tests for one edited file, or None when that has to be every test."""
        path = change.path
        if path.startswith("sqlglot/") and path.endswith(".py"):
            source = self.source(path)
            if source is None:
                log(f"{commit}: every test: {path} is not in the map")
                return None
            return self.for_source(commit, path, source, change)
        if path.startswith("tests/"):
            if path not in self.opens:
                log(f"{commit}: every test: no test is known to open {path}")
                return None
            part = self.tests(self.opens[path]) | self.own_file.get(path, 0)
            log(f"{commit}: {path} is opened or imported by {part.bit_count()} tests")
            return part
        log(f"{commit}: every test: {path} is outside sqlglot/**/*.py and tests/")
        return None

    def for_source(
        self, commit: str, path: str, source: SourceFile, change: FileChange
    ) -> int | None:
        last = max(change.removed + [below - 1 for _, below in change.inserted])
        if last > source.lines:
            log(f"{commit}: every test: {path} has {source.lines} lines, the patch names {last}")
            return None
        statements: set[int] = set()
        for line in change.removed:
            if source.owner[line]:
                statements.add(source.owner[line])
            else:  # a blank or comment line: whatever replaces it sits between its neighbours
                statements.update(self.around(source, line - 1, line + 1))
        for above, below in change.inserted:
            statements.update(self.around(source, above, below))

        selected = self.tests(source.readers)
        if selected:
            log(f"{commit}: {path} is read as text by {selected.bit_count()} tests")
        for statement in sorted(statements):
            _, _, whole_file, set_id = source.statements[statement]
            where = f"{commit}: {path}:{statement}"
            if whole_file:
                part = self.tests(source.any_line)
                why = "runs at import" if whole_file == 1 else "is only reached after other tests"
                log(f"{where} {why}: the {part.bit_count()} tests that ran the file")
            elif set_id >= 0:
                part = self.tests(set_id)
                log(f"{where} was executed by {part.bit_count()} tests")
            else:
                part = 0
                log(f"{where} was never executed by a test, selecting nothing")
            selected |= part
        return selected

    @staticmethod
    def around(source: SourceFile, above: int, below: int) -> list[int]:
        """The statements that new code between two old lines can belong to.

        The nearest code above and below each sit somewhere inside the statement
        that holds both of them. New code between them is reached by a test only
        if that test reached one of the two branches under that common statement,
        so each side is widened to the branch it is in. With nothing in common
        the two sides are separate top-level statements, which run at import.
        """
        while above >= 1 and not source.owner[above]:
            above -= 1
        while below <= source.lines and not source.owner[below]:
            below += 1
        before = source.enclosing(above) if above >= 1 else []
        after = source.enclosing(below) if below <= source.lines else []
        common = set(before) & set(after)
        picked = []
        for chain in (before, after):
            branch = None
            for statement in chain:
                if statement in common:
                    if branch is None:
                        branch = statement  # the line is the common statement's own
                    break
                branch = statement
            if branch is not None:
                picked.append(branch)
        return picked


# ------------------------------------------------------------------- splitting


def split(numbers: list[int], duration_ms: list[int]) -> list[list[int]]:
    """Longest first onto the lightest worker, for the worker count that costs least.

    Cost is the largest load plus 2 s for every worker, in whole milliseconds.
    """
    if not numbers:
        return []
    items = sorted(
        ((duration_ms[number], number) for number in numbers), key=lambda i: (-i[0], i[1])
    )
    total, longest = sum(ms for ms, _ in items), items[0][0]
    best_cost = -1
    best: list[list[int]] = []
    for count in range(1, min(MAX_WORKERS, len(items)) + 1):
        fee = WORKER_MS * count
        # No split over this many workers can beat this, so skip it if it cannot win.
        if best_cost >= 0 and max(longest, -(-total // count)) + fee >= best_cost:
            continue
        loads = [0] * count
        workers: list[list[int]] = [[] for _ in range(count)]
        for ms, number in items:
            lightest = loads.index(min(loads))
            loads[lightest] += ms
            workers[lightest].append(number)
        if best_cost < 0 or max(loads) + fee < best_cost:
            best_cost, best = max(loads) + fee, workers
    return [worker for worker in best if worker]


def main() -> None:
    with open(os.path.join(DATA, "tests.json"), encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    numbers = [test["number"] for test in tests]
    duration_ms = [int(test["seconds"] * 1000 + 0.5) for test in tests]
    everything = (1 << len(tests)) - 1
    try:
        selector: Selector | None = Selector(MAP, tests)
    except Exception as error:  # an unreadable map must not reject the run
        log(f"every test on every commit: the map could not be read: {error!r}")
        selector = None

    answers = []
    splits: dict[int, list[list[int]]] = {}
    directory = os.path.join(DATA, "commits")
    for name in sorted(os.listdir(directory)):
        if not name.endswith(".patch"):
            continue
        commit = name[: -len(".patch")]
        selected = everything
        if selector is not None:
            try:
                with open(
                    os.path.join(directory, name), encoding="utf-8", errors="replace"
                ) as handle:
                    selected = selector.select(commit, handle.read())
            except Exception as error:  # one odd patch must not reject the run either
                log(f"{commit}: every test: {error!r}")
        if selected not in splits:
            positions = split(bits(selected), duration_ms)
            splits[selected] = [[numbers[position] for position in worker] for worker in positions]
        workers = splits[selected]
        answers.append(json.dumps({"commit": commit, "workers": workers}, separators=(",", ":")))
        log(f"{commit}: {sum(len(worker) for worker in workers)} tests in {len(workers)} workers")
    sys.stdout.write("".join(answer + "\n" for answer in answers))


if __name__ == "__main__":
    main()
