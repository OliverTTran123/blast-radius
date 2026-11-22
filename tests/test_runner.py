"""Behavior tests for runner.py: what a job reports for tests that pass, fail, hang or die.

Runs with the standard library only (scripts/eval.sh runs it):

    python3 -B -m unittest discover -s tests

The runner is pointed at a small made-up checkout, written to a temporary
directory, instead of at sqlglot.
"""

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import runner  # noqa: E402

CHECKS = """
import os
import sys
import time
import unittest

import runner

NOTES = os.environ["RUNNER_TEST_NOTES"]


class TestThings(unittest.TestCase):
    def test_passes(self):
        self.assertEqual(1, 1)

    def test_fails(self):
        self.assertEqual(1, 2)

    def test_errors(self):
        raise RuntimeError("boom")

    def test_subtest_fails(self):
        for number in range(3):
            with self.subTest(number=number):
                self.assertNotEqual(number, 1)

    def test_subtests_pass(self):
        for number in range(3):
            with self.subTest(number=number):
                self.assertLess(number, 3)

    def test_skipped(self):
        self.skipTest("not today")

    def test_skips_a_subtest(self):
        with self.subTest("one"):
            self.skipTest("not this one")

    def test_exits(self):
        sys.exit(2)

    def test_dies(self):
        os._exit(3)

    def test_hangs(self):
        time.sleep(60)

    def test_is_loud(self):
        print("noise from print")
        sys.stdout.flush()
        os.write(1, b"noise straight to the descriptor")

    def test_depth(self):
        with open(os.path.join(NOTES, "depth"), "w") as handle:
            handle.write(str(runner.frames()))

    def test_leaves_a_process(self):
        pid = os.fork()
        if pid == 0:
            time.sleep(60)
            os._exit(0)
        with open(os.path.join(NOTES, "left"), "w") as handle:
            handle.write(str(pid))

    def test_out_of_memory_score(self):
        with open("/proc/self/oom_score_adj") as score:
            mine = score.read()
        with open(os.path.join(NOTES, "oom"), "w") as handle:
            handle.write(mine)

    def test_working_directory(self):
        root = os.environ["RUNNER_TEST_ROOT"]
        self.assertEqual(os.path.realpath(os.getcwd()), os.path.realpath(root))
        self.assertEqual(os.environ["SKIP_INTEGRATION"], "0")


class TestSetUpFails(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        raise RuntimeError("no set-up")

    def test_one(self):
        pass

    def test_two(self):
        pass


class TestCleanUpFails(unittest.TestCase):
    @classmethod
    def tearDownClass(cls):
        raise RuntimeError("no clean-up")

    def test_one(self):
        pass


class TestSetUpOnce(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(os.path.join(NOTES, "set_up"), "a") as handle:
            handle.write("x")

    def test_one(self):
        pass

    def test_two(self):
        pass
"""
BROKEN = "import unittest\n\nraise ImportError('this module does not import')\n"


def running(pid: int) -> bool:
    """True while the process exists and is not a zombie."""
    try:
        state = Path(f"/proc/{pid}/stat").read_text().rpartition(")")[2].split()[0]
    except OSError:
        return False
    return state != "Z"


class RunnerTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.before = (os.getcwd(), list(sys.path), dict(os.environ))
        cls.root = tempfile.mkdtemp(prefix="runner-test-")
        cls.notes = tempfile.mkdtemp(prefix="runner-notes-")
        os.environ["RUNNER_TEST_NOTES"] = cls.notes
        os.environ["RUNNER_TEST_ROOT"] = cls.root
        package = Path(cls.root, "checks")
        package.mkdir()
        (package / "__init__.py").write_text("")
        (package / "test_things.py").write_text(CHECKS)
        (package / "test_broken.py").write_text(BROKEN)
        names = [
            "checks/test_things.py::TestThings::" + name
            for name in (
                "test_passes",
                "test_fails",
                "test_errors",
                "test_subtest_fails",
                "test_subtests_pass",
                "test_skipped",
                "test_skips_a_subtest",
                "test_exits",
                "test_dies",
                "test_hangs",
                "test_is_loud",
                "test_depth",
                "test_leaves_a_process",
                "test_out_of_memory_score",
                "test_working_directory",
                "test_is_not_there",
            )
        ]
        names += ["checks/test_things.py::TestSetUpFails::test_one"]
        names += ["checks/test_things.py::TestSetUpFails::test_two"]
        names += ["checks/test_things.py::TestCleanUpFails::test_one"]
        names += ["checks/test_things.py::TestSetUpOnce::test_one"]
        names += ["checks/test_things.py::TestSetUpOnce::test_two"]
        names += ["checks/test_broken.py::TestBroken::test_one"]
        cls.tests = [
            {"number": number, "id": name, "file": name.split("::")[0], "seconds": 0.001}
            for number, name in enumerate(names)
        ]
        cls.at = {name.split("::", 1)[1]: number for number, name in enumerate(names)}
        (package / "late.py").write_text("LOADED = True\n")
        cls.runner = runner.Runner(cls.root, cls.tests, package="checks")
        with open(os.devnull, "w") as quiet:  # the broken module is reported on stderr
            stderr, sys.stderr = sys.stderr, quiet
            try:
                cls.runner.preload()
            finally:
                sys.stderr = stderr

    @classmethod
    def tearDownClass(cls):
        cwd, path, environment = cls.before
        os.chdir(cwd)
        sys.path[:] = path
        os.environ.clear()
        os.environ.update(environment)
        for name in [name for name in sys.modules if name.split(".")[0] == "checks"]:
            del sys.modules[name]
        shutil.rmtree(cls.root, ignore_errors=True)
        shutil.rmtree(cls.notes, ignore_errors=True)

    def letters(self, *names: str, budget: float = 20.0) -> str | None:
        """The letters of one job that runs these tests, or None when it is unverified."""
        positions = [self.at[name] for name in names]
        self.outcome = self.runner.run([("job", positions, budget)])["job"]
        if self.outcome.letters is None:
            return None
        return "".join(self.outcome.letters[position] for position in positions)

    def test_pass_fail_error(self):
        self.assertEqual(
            self.letters(
                "TestThings::test_passes", "TestThings::test_fails", "TestThings::test_errors"
            ),
            "PFF",
        )

    def test_a_failed_subtest_fails_the_test(self):
        self.assertEqual(self.letters("TestThings::test_subtest_fails"), "F")
        self.assertEqual(self.outcome.extra["subtests"], 3)
        self.assertIn("AssertionError", self.outcome.extra["error"])
        self.assertEqual(self.letters("TestThings::test_subtests_pass"), "P")

    def test_skips(self):
        self.assertEqual(self.letters("TestThings::test_skipped"), "S")
        # A skipped subtest does not skip its test.
        self.assertEqual(self.letters("TestThings::test_skips_a_subtest"), "P")

    def test_exit_inside_a_test_is_a_failure(self):
        self.assertEqual(self.letters("TestThings::test_exits", "TestThings::test_passes"), "FP")

    def test_a_child_that_dies_is_unverified(self):
        self.assertIsNone(self.letters("TestThings::test_passes", "TestThings::test_dies"))
        self.assertIn("status 3", self.outcome.why)

    def test_a_child_out_of_time_is_unverified(self):
        started = time.monotonic()
        self.assertIsNone(
            self.letters("TestThings::test_passes", "TestThings::test_hangs", budget=0.5)
        )
        self.assertIn("time", self.outcome.why)
        self.assertLess(time.monotonic() - started, 10)

    def test_set_up_class_failure_fails_every_test_of_the_class(self):
        self.assertEqual(
            self.letters(
                "TestThings::test_passes", "TestSetUpFails::test_one", "TestSetUpFails::test_two"
            ),
            "PFF",
        )

    def test_clean_up_failure_fails_the_class(self):
        self.assertEqual(
            self.letters("TestThings::test_passes", "TestCleanUpFails::test_one"), "PF"
        )

    def test_set_up_class_runs_once_for_a_class(self):
        Path(self.notes, "set_up").unlink(missing_ok=True)
        self.assertEqual(self.letters("TestSetUpOnce::test_one", "TestSetUpOnce::test_two"), "PP")
        self.assertEqual(Path(self.notes, "set_up").read_text(), "x")

    def test_tests_the_runner_cannot_find_do_not_run(self):
        self.assertEqual(
            self.runner.unknown[self.at["TestThings::test_is_not_there"]], "no such test method"
        )
        self.assertIn("cannot be imported", self.runner.unknown[self.at["TestBroken::test_one"]])
        self.assertEqual(
            self.letters(
                "TestThings::test_is_not_there", "TestBroken::test_one", "TestThings::test_passes"
            ),
            "NNP",
        )

    def test_a_test_starts_at_the_asked_depth(self):
        # This process is already deeper than pytest's 40 frames when it forks, so ask
        # for a depth no call chain reaches by itself and expect exactly that.
        wanted, runner.TEST_DEPTH = runner.TEST_DEPTH, 200
        try:
            self.assertEqual(self.letters("TestThings::test_depth"), "P")
        finally:
            runner.TEST_DEPTH = wanted
        self.assertEqual(Path(self.notes, "depth").read_text(), "200")
        self.assertGreaterEqual(wanted, 37)  # pytest-xdist calls a test from 37 frames down

    def test_checkout_is_the_working_directory(self):
        self.assertEqual(self.letters("TestThings::test_working_directory"), "P")

    def test_every_module_of_the_package_is_imported_ahead(self):
        # No test module imports checks.late, and the module that does not import is passed over.
        self.assertIn("checks.late", sys.modules)
        self.assertNotIn("checks.test_broken", sys.modules)

    def test_nothing_a_test_prints_reaches_stdout(self):
        with tempfile.TemporaryFile() as capture:
            sys.stdout.flush()
            saved = os.dup(1)
            os.dup2(capture.fileno(), 1)
            try:
                letters = self.letters("TestThings::test_is_loud")
            finally:
                os.dup2(saved, 1)
                os.close(saved)
            capture.seek(0)
            self.assertEqual(capture.read(), b"")
        self.assertEqual(letters, "P")

    def test_a_process_left_behind_is_ended(self):
        self.assertEqual(self.letters("TestThings::test_leaves_a_process"), "P")
        left = int(Path(self.notes, "left").read_text())
        deadline = time.monotonic() + 5
        while running(left) and time.monotonic() < deadline:
            time.sleep(0.02)
        self.assertFalse(running(left))

    def test_jobs_side_by_side_keep_their_own_answers(self):
        jobs = [
            ("a", [self.at["TestThings::test_passes"]], 20.0),
            ("b", [self.at["TestThings::test_fails"]], 20.0),
            ("c", [self.at["TestThings::test_hangs"]], 0.5),
            ("d", [self.at["TestThings::test_passes"], self.at["TestThings::test_errors"]], 20.0),
            ("e", [], 20.0),
        ]
        done = self.runner.run(jobs, parallel=3)
        self.assertEqual(done["a"].letters, {self.at["TestThings::test_passes"]: "P"})
        self.assertEqual(done["b"].letters, {self.at["TestThings::test_fails"]: "F"})
        self.assertIsNone(done["c"].letters)
        self.assertEqual(sorted(done["d"].letters.values()), ["F", "P"])
        self.assertEqual(done["e"].letters, {})

    def test_a_job_that_cannot_be_started_is_unverified(self):
        def no_more_processes():
            raise BlockingIOError(11, "Resource temporarily unavailable")

        passes = self.at["TestThings::test_passes"]
        fork, os.fork = os.fork, no_more_processes
        try:
            done = self.runner.run([("a", [passes], 20.0), ("b", [passes], 20.0)])
        finally:
            os.fork = fork
        self.assertIsNone(done["a"].letters)
        self.assertIsNone(done["b"].letters)
        self.assertIn("could not be started", done["a"].why)
        self.assertEqual(self.letters("TestThings::test_passes"), "P")  # and it works again after

    def test_a_child_is_the_first_to_go_when_memory_runs_out(self):
        self.assertEqual(self.letters("TestThings::test_out_of_memory_score"), "P")
        self.assertEqual(Path(self.notes, "oom").read_text().strip(), "1000")

    def test_prepare_runs_in_the_child_and_its_failure_is_unverified(self):
        def prepare(key):
            if key == "bad":
                raise RuntimeError("the patch did not go in")
            os.environ["RUNNER_TEST_PREPARED"] = "yes"

        passes = self.at["TestThings::test_passes"]
        done = self.runner.run([("good", [passes], 20.0), ("bad", [passes], 20.0)], prepare=prepare)
        self.assertEqual(done["good"].letters, {passes: "P"})
        self.assertIsNone(done["bad"].letters)
        self.assertNotIn("RUNNER_TEST_PREPARED", os.environ)  # the parent is untouched


if __name__ == "__main__":
    unittest.main()
