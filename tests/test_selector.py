"""Behavior tests for the selector: a patch goes in, the selected tests come out.

They build a real, tiny map with the map builder's own code from a made-up
source file and made-up coverage, then run the selector against it. Run with:

    python3 -B -m unittest discover -s tests
"""

from __future__ import annotations

import contextlib
import difflib
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tools" / "build_map"))

import assemble  # noqa: E402
import selector  # noqa: E402

SOURCE = """\
import os

TABLE = {
    "a": 1,
    "b": 2,
}


def first(a, b):
    \"\"\"Docstring.\"\"\"
    if a:
        if b:
            x = 1
        # a comment on its own line
    elif b:
        x = 2
    y = call(
        a,
        b,
    )
    return y


def second():
    return 0
"""
# Line numbers:  1 import   3-6 TABLE   9 def first   10 docstring   11 if a
#   12 if b   13 x = 1   14 comment   15 elif b   16 x = 2   17-20 y = call(...)
#   21 return y   24 def second   25 return 0

IMPORTED = [1, 3, 4, 5, 6, 9, 24]
RAN = {
    0: [11, 12, 13, 17, 18, 19, 20, 21],  # first(True, True)
    1: [11, 12, 17, 18, 19, 20, 21],  # first(True, False)
    2: [11, 15, 16, 17, 18, 19, 20, 21],  # first(False, True)
    3: [25],  # second()
    4: [],  # only imports the module
}
IDS = [f"tests/test_{'one' if n < 3 else 'two'}.py::T::test_{n}" for n in range(5)]
DEMO = "sqlglot/demo.py"
TYPED = "sqlglot/typed.py"
LATE = "sqlglot/late.py"
LATE_SOURCE = "def f(x):\n    if x:\n        return 1\n    return 2\n"


def mask(lines: list[int]) -> int:
    return sum(1 << line for line in lines)


def tests_json(ids: list[str]) -> list[dict]:
    return [
        {"number": number, "id": test_id, "file": test_id.split("::")[0], "seconds": 0.5}
        for number, test_id in enumerate(ids)
    ]


def build_map(directory: Path) -> None:
    sets = assemble.SetTable()
    hits = [(number, mask(IMPORTED + lines)) for number, lines in RAN.items()]
    demo = assemble.build_file(SOURCE, hits, dict(hits), sets)["chunk"]
    demo.update(any_line=sets.add(0b11111), readers=sets.add(0), no_test_context=[])
    typed = assemble.build_file("VALUE = 1\n", [(3, mask([1]))], {3: mask([1])}, sets)["chunk"]
    typed.update(any_line=sets.add(0b01000), readers=sets.add(0b10000), no_test_context=[])
    # Test 1 runs `return 1` only in the whole-suite pass, never when run alone.
    late_hits = [(0, mask([1, 2, 4])), (1, mask([1, 2, 3]))]
    late_alone = {0: mask([1, 2, 4]), 1: mask([1, 2])}
    late = assemble.build_file(LATE_SOURCE, late_hits, late_alone, sets)["chunk"]
    late.update(any_line=sets.add(0b00011), readers=sets.add(0), no_test_context=[])
    opens = {
        "tests/fixtures/a.sql": sets.add(0b00110),
        "tests/test_two.py": sets.add(0b11000),
        "tests/helpers.py": sets.add(0b10001),
    }
    chunks = {
        DEMO: assemble.pack(demo),
        TYPED: assemble.pack(typed),
        LATE: assemble.pack(late),
        "#tests": assemble.pack(IDS),
        "#opens": assemble.pack(opens),
    }
    assemble.write_map(directory, chunks, sets, len(IDS), {})


def patch(path: str, old: str, new: str) -> str:
    """A git-style unified diff that turns old into new."""
    diff = difflib.unified_diff(
        old.splitlines(keepends=True), new.splitlines(keepends=True), f"a/{path}", f"b/{path}"
    )
    return f"diff --git a/{path} b/{path}\nindex 1111111..2222222 100644\n" + "".join(diff)


def replaced(line: int, text: str) -> str:
    lines = SOURCE.splitlines(keepends=True)
    lines[line - 1] = text + "\n"
    return "".join(lines)


def inserted_after(line: int, text: str) -> str:
    lines = SOURCE.splitlines(keepends=True)
    lines.insert(line, text + "\n")
    return "".join(lines)


def deleted(line: int) -> str:
    lines = SOURCE.splitlines(keepends=True)
    del lines[line - 1]
    return "".join(lines)


class SelectorCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.directory = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.directory.cleanup)
        build_map(Path(cls.directory.name))
        cls.selector = selector.Selector(cls.directory.name, tests_json(IDS))

    def select(self, text: str, chooser: selector.Selector | None = None) -> set[int]:
        with contextlib.redirect_stderr(io.StringIO()) as self.log:
            return set(selector.bits((chooser or self.selector).select("c", text)))

    def edit(self, new_source: str) -> set[int]:
        return self.select(patch(DEMO, SOURCE, new_source))


class TestChangedLines(SelectorCase):
    def test_statement_in_a_function_selects_the_tests_that_ran_it(self):
        self.assertEqual(self.edit(replaced(13, "            x = 9")), {0})
        self.assertEqual(self.edit(replaced(16, "        x = 9")), {2})
        self.assertEqual(self.edit(replaced(21, "    return -y")), {0, 1, 2})
        self.assertEqual(self.edit(replaced(25, "    return 1")), {3})

    def test_compound_header_selects_every_test_that_reached_it(self):
        self.assertEqual(self.edit(replaced(12, "        if not b:")), {0, 1})
        self.assertEqual(self.edit(replaced(11, "    if not a:")), {0, 1, 2})
        self.assertEqual(self.edit(replaced(15, "    elif not b:")), {2})

    def test_continuation_line_goes_to_its_statement(self):
        self.assertEqual(self.edit(replaced(18, "        b,")), {0, 1, 2})
        self.assertEqual(self.edit(replaced(20, "    ) + 1")), {0, 1, 2})

    def test_import_time_statement_selects_every_test_that_ran_the_file(self):
        everyone = {0, 1, 2, 3, 4}
        self.assertEqual(self.edit(replaced(4, '    "a": 7,')), everyone)
        self.assertEqual(self.edit(replaced(9, "def first(a, b=1):")), everyone)
        self.assertEqual(self.edit(replaced(1, "import os, sys")), everyone)

    def test_statement_no_test_ran_selects_nothing_and_says_so(self):
        self.assertEqual(self.edit(replaced(10, '    """Other."""')), set())
        self.assertIn("never executed", self.log.getvalue())

    def test_deleted_line_selects_the_tests_that_ran_it(self):
        self.assertEqual(self.edit(deleted(13)), {0})

    def test_statement_reached_only_after_other_tests_selects_the_whole_file(self):
        # Only test 1 ever ran `return 1`, and only after other tests. Later tests
        # may rely on what it did, so the file's other test is selected as well.
        changed = LATE_SOURCE.replace("return 1", "return 3")
        self.assertEqual(self.select(patch(LATE, LATE_SOURCE, changed)), {0, 1})
        changed = LATE_SOURCE.replace("return 2", "return 3")
        self.assertEqual(self.select(patch(LATE, LATE_SOURCE, changed)), {0})

    def test_comment_line_selects_for_the_code_on_both_sides(self):
        self.assertEqual(self.edit(replaced(14, "        y = 0")), {0, 1, 2})


class TestInsertedLines(SelectorCase):
    def test_between_two_statements_of_a_block(self):
        self.assertEqual(self.edit(inserted_after(20, "    y += 1")), {0, 1, 2})

    def test_inside_a_statement_that_spans_lines(self):
        self.assertEqual(self.edit(inserted_after(18, "        # why")), {0, 1, 2})

    def test_after_an_inner_block_is_widened_to_the_block_it_may_join(self):
        # New code after `x = 1` may sit in the `if a:` body, which test 1 reaches
        # although it ran neither `x = 1` nor the `elif`.
        self.assertEqual(self.edit(inserted_after(13, "        z = 0")), {0, 1, 2})

    def test_right_after_a_def_line_selects_every_test_that_ran_the_file(self):
        self.assertEqual(self.edit(inserted_after(9, "    a = not a")), {0, 1, 2, 3, 4})

    def test_between_two_functions_selects_every_test_that_ran_the_file(self):
        self.assertEqual(self.edit(inserted_after(21, "    # done")), {0, 1, 2, 3, 4})

    def test_at_the_top_and_bottom_of_the_file(self):
        self.assertEqual(self.edit("# header\n" + SOURCE), {0, 1, 2, 3, 4})
        self.assertEqual(self.edit(SOURCE + "LAST = 1\n"), {0, 1, 2, 3, 4})


class TestOtherPaths(SelectorCase):
    everyone = {0, 1, 2, 3, 4}

    def test_file_read_as_text_always_selects_its_readers(self):
        self.assertEqual(self.select(patch(TYPED, "VALUE = 1\n", "VALUE = 2\n")), {3, 4})

    def test_fixture_selects_the_tests_that_opened_it(self):
        self.assertEqual(self.select(patch("tests/fixtures/a.sql", "x;\n", "y;\n")), {1, 2})

    def test_test_module_selects_its_own_tests_and_its_importers(self):
        self.assertEqual(self.select(patch("tests/test_two.py", "a\n", "b\n")), {3, 4})
        self.assertEqual(self.select(patch("tests/helpers.py", "a\n", "b\n")), {0, 4})

    def test_unknown_paths_select_every_test(self):
        for path in ("tests/fixtures/new.sql", "sqlglot/new.py", "sqlglot/py.typed", "setup.py"):
            with self.subTest(path):
                self.assertEqual(self.select(patch(path, "a\n", "b\n")), self.everyone)

    def test_line_beyond_the_mapped_file_selects_every_test(self):
        longer = SOURCE + "\n" * 40 + "x = 1\n"
        self.assertEqual(self.select(patch(DEMO, longer, longer + "y = 2\n")), self.everyone)

    def test_one_unknown_file_makes_the_whole_patch_every_test(self):
        both = patch(DEMO, SOURCE, replaced(25, "    return 1")) + patch("setup.py", "a\n", "b\n")
        self.assertEqual(self.select(both), self.everyone)

    def test_several_files_are_unioned(self):
        both = patch(DEMO, SOURCE, replaced(25, "    return 1")) + patch(
            "tests/fixtures/a.sql", "x;\n", "y;\n"
        )
        self.assertEqual(self.select(both), {1, 2, 3})


class TestPatchesThatAreNotPlainEdits(SelectorCase):
    everyone = {0, 1, 2, 3, 4}
    header = f"diff --git a/{DEMO} b/{DEMO}\n"

    def test_new_deleted_renamed_binary_and_mode_changes_select_every_test(self):
        bodies = {
            "new file": "new file mode 100644\nindex 0000000..1111111\n--- /dev/null\n"
            f"+++ b/{DEMO}\n@@ -0,0 +1 @@\n+x = 1\n",
            "deleted file": "deleted file mode 100644\nindex 1111111..0000000\n"
            f"--- a/{DEMO}\n+++ /dev/null\n@@ -1 +0,0 @@\n-x = 1\n",
            "rename": "similarity index 100%\nrename from sqlglot/demo.py\n"
            "rename to sqlglot/d.py\n",
            "binary": "index 1111111..2222222 100644\nBinary files a/x and b/x differ\n",
            "mode": "old mode 100644\nnew mode 100755\n",
        }
        for name, body in bodies.items():
            with self.subTest(name):
                self.assertEqual(self.select(self.header + body), self.everyone)

    def test_renamed_file_without_git_headers_selects_every_test(self):
        text = (
            "--- a/sqlglot/demo.py\n+++ b/sqlglot/other.py\n"
            "@@ -25 +25 @@\n-    return 0\n+    return 1\n"
        )
        self.assertEqual(self.select(text), self.everyone)

    def test_damaged_patches_select_every_test(self):
        good = patch(DEMO, SOURCE, replaced(25, "    return 1"))
        for name, text in {
            "empty": "",
            "no hunks": self.header + f"--- a/{DEMO}\n+++ b/{DEMO}\n",
            "cut short": good[: good.rindex("-    return 0")],
            "bad header": good.replace("@@ -22,4 +22,4 @@", "@@ -22,x +22,4 @@"),
        }.items():
            with self.subTest(name):
                self.assertEqual(self.select(text), self.everyone)


class TestPatchParsing(unittest.TestCase):
    def parse(self, text: str) -> list[tuple[str, list[int], list[tuple[int, int]]]]:
        changes, reason = selector.parse_patch(text)
        self.assertIsNotNone(changes, reason)
        return [(change.path, change.removed, change.inserted) for change in changes]

    def test_replacement_insertion_and_deletion_in_one_hunk(self):
        text = (
            "--- a/f.py\n+++ b/f.py\n@@ -10,7 +10,7 @@ def f():\n"
            " ten\n-eleven\n+ELEVEN\n twelve\n+new\n thirteen\n-fourteen\n fifteen\n sixteen\n"
        )
        self.assertEqual(self.parse(text), [("f.py", [11, 14], [(12, 13)])])

    def test_counts_default_to_one_and_zero_old_lines_means_after(self):
        self.assertEqual(self.parse("--- a/f\n+++ b/f\n@@ -5 +5 @@\n-a\n+b\n"), [("f", [5], [])])
        text = "--- a/f\n+++ b/f\n@@ -12,0 +13,2 @@\n+a\n+b\n"
        self.assertEqual(self.parse(text), [("f", [], [(12, 13)])])

    def test_removed_line_that_looks_like_a_file_header_stays_in_its_hunk(self):
        text = "--- a/q.sql\n+++ b/q.sql\n@@ -3,3 +3,2 @@\n SELECT 1;\n--- a comment\n SELECT 2;\n"
        self.assertEqual(self.parse(text), [("q.sql", [4], [])])

    def test_no_newline_marker_and_crlf_are_tolerated(self):
        text = (
            "--- a/f\r\n+++ b/f\r\n@@ -2,2 +2,2 @@\r\n a\r\n"
            "-b\r\n\\ No newline at end of file\r\n+c\r\n"
        )
        self.assertEqual(self.parse(text), [("f", [3], [])])

    def test_several_hunks_and_files(self):
        text = (
            "diff --git a/one b/one\nindex 1..2 100644\n--- a/one\n+++ b/one\n"
            "@@ -1,2 +1,2 @@\n-a\n+A\n b\n@@ -40,2 +40,3 @@\n x\n+y\n z\n"
            "diff --git a/two b/two\nindex 3..4 100644\n--- a/two\n+++ b/two\n@@ -7 +7 @@\n-q\n+Q\n"
        )
        self.assertEqual(self.parse(text), [("one", [1], [(40, 41)]), ("two", [7], [])])


class TestTestNumbering(SelectorCase):
    def test_tests_are_matched_by_node_id_not_by_position(self):
        reordered = selector.Selector(self.directory.name, tests_json(IDS[::-1]))
        chosen = self.select(patch(DEMO, SOURCE, replaced(13, "            x = 9")), reordered)
        self.assertEqual(chosen, {4})  # map test 0 is the fifth test of this run

    def test_a_test_the_map_has_never_seen_is_always_selected(self):
        with contextlib.redirect_stderr(io.StringIO()) as said:
            extra = selector.Selector(
                self.directory.name, tests_json([*IDS, "tests/test_new.py::T::t"])
            )
        self.assertIn("1 tests are not in the map", said.getvalue())
        chosen = self.select(patch(DEMO, SOURCE, replaced(10, '    """Other."""')), extra)
        self.assertEqual(chosen, {5})


class TestSplit(unittest.TestCase):
    def cost(self, workers: list[list[int]], durations: list[int]) -> int:
        return max(sum(durations[n] for n in worker) for worker in workers) + 2000 * len(workers)

    def test_nothing_selected_is_no_workers(self):
        self.assertEqual(selector.split([], []), [])

    def test_a_small_selection_stays_on_one_worker(self):
        durations = [900, 100, 100, 0]
        self.assertEqual(selector.split([0, 1, 2, 3], durations), [[0, 1, 2, 3]])

    def test_long_tests_are_spread_when_that_is_cheaper(self):
        durations = [30_000, 28_000, 5_000, 0, 0]
        workers = selector.split([0, 1, 2, 3, 4], durations)
        self.assertEqual(sorted(n for worker in workers for n in worker), [0, 1, 2, 3, 4])
        self.assertEqual(self.cost(workers, durations), 36_000)  # 30 s + 3 workers

    def test_an_extra_worker_is_only_used_when_it_pays_for_itself(self):
        # 20 one-minute tests: 7 workers cost 180 s + 14 s, 8 workers 180 s + 16 s.
        self.assertEqual(len(selector.split(list(range(20)), [60_000] * 20)), 7)

    def test_never_more_than_eight_workers_and_no_empty_ones(self):
        durations = [60_000] * 100
        workers = selector.split(list(range(100)), durations)
        self.assertEqual(len(workers), 8)
        self.assertTrue(all(workers))
        self.assertEqual(sorted(n for worker in workers for n in worker), list(range(100)))


class TestWholeProgram(unittest.TestCase):
    def test_prints_one_answer_line_per_patch_and_nothing_else(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            build_map(root / "map")
            (root / "data" / "commits").mkdir(parents=True)
            tests = tests_json(IDS)
            (root / "data" / "tests.json").write_text(json.dumps({"tests": tests, "skipped": []}))
            commits = {
                "aaaa": patch(DEMO, SOURCE, replaced(13, "            x = 9")),
                "bbbb": patch(DEMO, SOURCE, replaced(10, '    """Other."""')),
                "cccc": patch("setup.py", "a\n", "b\n"),
            }
            for commit, text in commits.items():
                (root / "data" / "commits" / f"{commit}.patch").write_text(text)
            (root / "data" / "commits" / "notes.txt").write_text("not a patch")
            previous = selector.DATA, selector.MAP
            selector.DATA, selector.MAP = str(root / "data"), str(root / "map")
            try:
                with (
                    contextlib.redirect_stdout(io.StringIO()) as out,
                    contextlib.redirect_stderr(io.StringIO()),
                ):
                    selector.main()
            finally:
                selector.DATA, selector.MAP = previous
        answers = [json.loads(line) for line in out.getvalue().splitlines()]
        self.assertEqual([answer["commit"] for answer in answers], ["aaaa", "bbbb", "cccc"])
        self.assertEqual(answers[0]["workers"], [[0]])
        self.assertEqual(answers[1]["workers"], [])
        self.assertEqual(sorted(answers[2]["workers"][0]), [0, 1, 2, 3, 4])
        self.assertTrue(all(set(answer) == {"commit", "workers"} for answer in answers))

    def test_an_unreadable_map_falls_back_to_every_test(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / "data" / "commits").mkdir(parents=True)
            (root / "data" / "tests.json").write_text(json.dumps({"tests": tests_json(IDS)}))
            (root / "data" / "commits" / "aaaa.patch").write_text("anything")
            previous = selector.DATA, selector.MAP
            selector.DATA, selector.MAP = str(root / "data"), str(root / "missing")
            try:
                with (
                    contextlib.redirect_stdout(io.StringIO()) as out,
                    contextlib.redirect_stderr(io.StringIO()) as err,
                ):
                    selector.main()
            finally:
                selector.DATA, selector.MAP = previous
        answer = json.loads(out.getvalue())
        self.assertEqual(sorted(n for worker in answer["workers"] for n in worker), [0, 1, 2, 3, 4])
        self.assertIn("map could not be read", err.getvalue())


if __name__ == "__main__":
    unittest.main()
