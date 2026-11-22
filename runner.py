"""Runs sqlglot's own tests on the pinned checkout, so a candidate can be checked.

The scored image holds sqlglot/ and tests/ of the pinned commit and every
package of requirements.lock (see the Dockerfile). A parent process imports
sqlglot and every test module once. For each job it forks a child, which runs
the chosen test methods with unittest, not pytest, and writes one letter per
test to a pipe:

  P  ran and passed
  F  failed: a failure or an error in the test, in one of its subtests, or in
     the set-up or clean-up of its class or module
  S  skipped
  N  did not run

Only P says a test is fine. The parent gives every child a wall-clock budget.
A child that runs out of it, dies, exits with a status other than 0 or sends
anything but a complete answer counts for nothing: its job comes back
unverified, and the caller keeps every test of it.

What is done so that a test behaves here as it did for the labeler:
  - The checkout is first on sys.path and is the working directory, and
    SKIP_INTEGRATION=0, as README.md says the tests were labeled.
  - Every test module is imported before any test runs, as pytest does when it
    collects, and tests run in tests.json order, which is pytest's. Every
    sqlglot module is imported too, which is how the labeler's process looked
    once its tests had asked for every dialect.
  - unittest.TestSuite runs the tests, so setUpClass and class clean-ups run
    once per class, as under pytest.
  - A test method starts TEST_DEPTH Python frames down the stack. pytest calls
    one from 32 frames down and from 37 under pytest-xdist, so a test never has
    more room before a RecursionError here than it had there.

`python runner.py selfcheck` runs every test alone on the unpatched code and
prints what it saw as JSON. scripts/selfcheck.sh runs it under the scorer's
flags and writes map/never_verify.json from it.
"""

from __future__ import annotations

import argparse
import contextlib
import gc
import importlib
import json
import os
import pkgutil
import select
import signal
import sys
import time
import unittest

PINNED = "/app/pinned"
TEST_DEPTH = 40
# The numeric libraries start a thread per CPU when they load. One thread each
# keeps the parent single-threaded, which fork wants, and the process count low.
ONE_THREAD = ("OPENBLAS_NUM_THREADS", "OMP_NUM_THREADS", "MKL_NUM_THREADS")
PR_SET_CHILD_SUBREAPER = 36
LETTERS = frozenset("PFSN")


def log(message: str) -> None:
    print(message, file=sys.stderr)


def frames() -> int:
    """How many Python frames deep the caller is."""
    frame, count = sys._getframe(1), 0
    while frame is not None:
        count += 1
        frame = frame.f_back
    return count


def lowered(extra: int, call, *arguments):
    """call(*arguments), from `extra` frames further down the stack."""
    if extra <= 0:
        return call(*arguments)
    return lowered(extra - 1, call, *arguments)


class Record(unittest.TestResult):
    """Which tests of one run started, failed or were skipped."""

    def __init__(self, position_of: dict[int, int], of_owner: dict[str, list[int]]) -> None:
        super().__init__()
        self.position_of = position_of  # id(test case) -> its position in tests.json
        self.of_owner = of_owner  # "module.Class" and "module" -> positions in this run
        self.started: set[int] = set()
        self.failed: set[int] = set()
        self.skipped: set[int] = set()
        self.subtests = 0
        self.first_error = ""

    def startTest(self, test) -> None:
        super().startTest(test)
        self.started.add(self.position_of[id(test)])

    def fail(self, test, error) -> None:
        if error is not None and not self.first_error:
            self.first_error = f"{error[0].__name__}: {error[1]}"[:300]
        position = self.position_of.get(id(test))
        if position is not None:
            self.failed.add(position)
            return
        # Not a test of ours: unittest's stand-in for a class or module whose set-up
        # or clean-up failed, described as "setUpClass (module.Class)". Every test
        # of that class or module fails with it. If the name is not one we know,
        # every test of the run does.
        described = str(getattr(test, "description", ""))
        owner = described[described.rfind("(") + 1 : -1] if described.endswith(")") else ""
        self.failed.update(self.of_owner.get(owner) or self.position_of.values())

    def addError(self, test, err) -> None:
        self.fail(test, err)

    def addFailure(self, test, err) -> None:
        self.fail(test, err)

    def addSubTest(self, test, subtest, err) -> None:
        self.subtests += 1
        if err is not None:
            self.fail(test, err)

    def addUnexpectedSuccess(self, test) -> None:
        self.fail(test, None)

    def addSkip(self, test, reason) -> None:
        # A skipped subtest leaves its test as it is. A skipped class never starts its tests.
        position = self.position_of.get(id(test))
        if position is not None:
            self.skipped.add(position)

    def addExpectedFailure(self, test, err) -> None:
        pass

    def addSuccess(self, test) -> None:
        pass

    def letter(self, position: int) -> str:
        if position in self.failed:
            return "F"
        if position in self.skipped:
            return "S"
        return "P" if position in self.started else "N"


class Watch:
    """What a self-check child notes about its tests: processes started, modules imported."""

    EVENTS = ("subprocess.Popen", "os.fork", "os.forkpty", "os.posix_spawn", "os.system", "os.exec")

    def __init__(self) -> None:
        import multiprocessing.process

        self.starts: set[str] = set()
        self.modules = set(sys.modules)
        base = multiprocessing.process.BaseProcess
        original = base.start

        def start(process):
            # multiprocessing starts a worker without an audit event when it does not fork.
            self.starts.add(f"multiprocessing {type(process).__name__}")
            return original(process)

        base.start = start
        sys.addaudithook(self.audit)

    def audit(self, event: str, arguments: tuple) -> None:
        if event in self.EVENTS:
            self.starts.add(event)

    def report(self) -> dict:
        return {
            "starts": sorted(self.starts),
            "imported": sorted(set(sys.modules) - self.modules),
        }


class Outcome:
    """How one job ended. letters is None when the job is unverified."""

    __slots__ = ("letters", "why", "cpu", "seconds", "extra")

    def __init__(self, letters, why: str, cpu: float, seconds: float, extra: dict) -> None:
        self.letters: dict[int, str] | None = letters  # position -> P, F, S or N
        self.why = why  # for an unverified job, what went wrong
        self.cpu = cpu  # user + system seconds of the child and the children it waited for
        self.seconds = seconds
        self.extra = extra


class Child:
    __slots__ = ("key", "positions", "pid", "fd", "deadline", "started", "data")

    def __init__(self, key, positions: list[int], pid: int, fd: int, budget: float) -> None:
        self.key = key
        self.positions = positions
        self.pid = pid
        self.fd = fd
        self.started = time.monotonic()
        self.deadline = self.started + budget
        self.data = b""


class Runner:
    """The parent: everything imported once, then one forked child per job."""

    def __init__(self, root: str, tests: list[dict], package: str | None = "sqlglot") -> None:
        self.root = root
        self.tests = tests
        self.package = package  # every module of it is imported ahead of the tests
        self.cases: list[tuple[type, str] | None] = [None] * len(tests)
        self.unknown: dict[int, str] = {}  # position -> why this test cannot be run here
        self.between = 0  # frames between run_here and a test method
        self.spare: dict[int, tuple[int, object]] = {}  # children reaped ahead of their turn
        self.watching = False  # children also report what Watch saw
        self.keep_stderr = False  # children keep stderr, for debugging

    # ------------------------------------------------------------- the parent

    def preload(self, extra_modules: tuple[str, ...] = ()) -> None:
        """Import the pinned sqlglot and every test module, and find every test."""
        os.environ["SKIP_INTEGRATION"] = "0"
        for name in ONE_THREAD:
            os.environ.setdefault(name, "1")
        if self.root not in sys.path:
            sys.path.insert(0, self.root)
        os.chdir(self.root)

        modules: dict[str, object] = {}
        for file in sorted({test["file"] for test in self.tests}):
            try:
                modules[file] = importlib.import_module(file[: -len(".py")].replace("/", "."))
            except BaseException as error:  # whatever stops an import stops these tests only
                modules[file] = error
                log(f"runner: {file} cannot be imported: {error!r}")
        for position, test in enumerate(self.tests):
            target = modules[test["file"]]
            if isinstance(target, BaseException):
                self.unknown[position] = f"its file cannot be imported ({type(target).__name__})"
                continue
            _, *owners, method = test["id"].split("::")
            for name in owners:
                target = getattr(target, name, None)
            is_case = isinstance(target, type) and issubclass(target, unittest.TestCase)
            if not owners or not is_case or not callable(getattr(target, method, None)):
                self.unknown[position] = "no such test method"
                continue
            self.cases[position] = (target, method)

        # sqlglot loads a dialect's modules when a test first asks for the dialect. A
        # child that had to do that again in every job would spend more on imports
        # than on its tests, so the parent loads every module of the package now.
        ahead = list(extra_modules)
        if self.package is not None:
            top = importlib.import_module(self.package)
            walk = pkgutil.walk_packages(top.__path__, prefix=self.package + ".")
            ahead += [info.name for info in walk if not info.name.endswith(".__main__")]
        for name in ahead:
            try:
                importlib.import_module(name)
            except Exception as error:
                log(f"runner: {name} cannot be imported ahead of time: {error!r}")

        # How far below run_here a test method starts when nothing is added.
        seen: list[int] = []

        class Depth(unittest.TestCase):
            def test(self) -> None:
                seen.append(frames())

        here = frames()
        lowered(0, unittest.TestSuite([Depth("test")]).run, unittest.TestResult())
        self.between = seen[0] - here

        # A process a child leaves behind becomes ours to reap, whatever started us.
        try:
            import ctypes

            ctypes.CDLL(None).prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0)
        except (OSError, AttributeError):
            pass
        # Children share the parent's memory until they write to it. Frozen objects
        # are left alone by the collector, so fewer pages get copied.
        gc.collect()
        gc.freeze()

    def run(
        self, jobs: list[tuple[object, list[int], float]], parallel: int = 2, prepare=None
    ) -> dict[object, Outcome]:
        """Run each job (key, positions, wall budget in seconds) in a child of its own.

        prepare(key), when given, is called in the child before its tests run.
        """
        waiting = list(reversed(jobs))
        active: dict[int, Child] = {}
        done: dict[object, Outcome] = {}
        while waiting or active:
            while waiting and len(active) < parallel:
                key, positions, budget = waiting.pop()
                try:
                    child = self.start(key, positions, budget, prepare)
                except OSError as error:  # no process or no descriptor to be had
                    done[key] = Outcome(None, f"could not be started ({error!r})", 0.0, 0.0, {})
                    continue
                active[child.fd] = child
            if not active:
                continue
            patience = min(child.deadline for child in active.values()) - time.monotonic()
            ready, _, _ = select.select(list(active), [], [], max(0.0, patience))
            for fd in ready:
                child = active[fd]
                chunk = os.read(fd, 1 << 16)
                child.data += chunk
                # A complete answer ends with a newline. End of file without one is a crash.
                if not chunk or child.data.endswith(b"\n"):
                    del active[fd]
                    done[child.key] = self.finish(child, False, active)
            now = time.monotonic()
            for child in [child for child in active.values() if now >= child.deadline]:
                del active[child.fd]
                done[child.key] = self.finish(child, True, active)
        return done

    def start(self, key, positions: list[int], budget: float, prepare) -> Child:
        positions = sorted(positions)
        read_fd, write_fd = os.pipe()
        sys.stdout.flush()
        sys.stderr.flush()
        try:
            pid = os.fork()
        except OSError:
            os.close(read_fd)
            os.close(write_fd)
            raise
        if pid == 0:
            self.child(key, positions, read_fd, write_fd, prepare)  # does not return
        os.close(write_fd)
        return Child(key, positions, pid, read_fd, budget)

    def finish(self, child: Child, timed_out: bool, active: dict[int, Child]) -> Outcome:
        os.close(child.fd)
        if timed_out:
            self.kill(child.pid)
        status, usage = self.reap(child.pid)
        self.kill(child.pid)  # whatever the child left running in its group
        still_running = {other.pid for other in active.values()}
        while True:  # and whatever it left that has already ended
            try:
                pid, other_status, other_usage = os.wait4(-1, os.WNOHANG)
            except ChildProcessError:
                break
            if pid == 0:
                break
            if pid in still_running:
                self.spare[pid] = (other_status, other_usage)

        cpu = usage.ru_utime + usage.ru_stime
        seconds = time.monotonic() - child.started
        code = os.waitstatus_to_exitcode(status)
        if timed_out:
            return Outcome(None, "ran out of its time", cpu, seconds, {})
        if code != 0:
            return Outcome(None, f"ended with status {code}", cpu, seconds, {})
        try:
            answer = json.loads(child.data)
            letters = answer["o"]
            if len(letters) != len(child.positions) or not LETTERS.issuperset(letters):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return Outcome(None, "sent no complete answer", cpu, seconds, {})
        found = dict(zip(child.positions, letters, strict=True))
        return Outcome(found, "", cpu, seconds, answer.get("x", {}))

    @staticmethod
    def kill(pid: int) -> None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)  # the child leads its own process group

    def reap(self, pid: int) -> tuple[int, object]:
        if pid in self.spare:
            return self.spare.pop(pid)
        _, status, usage = os.wait4(pid, 0)
        return status, usage

    # -------------------------------------------------------------- the child

    def child(self, key, positions: list[int], read_fd: int, write_fd: int, prepare) -> None:
        status = 70
        try:
            os.close(read_fd)
            os.setsid()
            # If memory runs out, this child and what it starts go first, not the parent.
            with contextlib.suppress(OSError), open("/proc/self/oom_score_adj", "w") as handle:
                handle.write("1000")
            null = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1) if self.keep_stderr else (0, 1, 2):
                os.dup2(null, fd)  # stdout carries the selector's answers and nothing else
            watch = Watch() if self.watching else None
            if prepare is not None:
                prepare(key)
            answer = self.run_here(positions)
            if watch is not None:
                answer["x"].update(watch.report())
            data = json.dumps(answer, separators=(",", ":")).encode("utf-8") + b"\n"
            while data:
                data = data[os.write(write_fd, data) :]
            status = 0
        except BaseException as error:  # the parent takes a silent child as unverified
            if self.keep_stderr:
                log(f"runner: child for {key!r}: {error!r}")
        finally:
            os._exit(status)

    def run_here(self, positions: list[int]) -> dict:
        """Run these tests in this process, in the order given. One letter each."""
        cases = []
        position_of: dict[int, int] = {}
        of_owner: dict[str, list[int]] = {}
        for position in positions:
            found = self.cases[position]
            if found is None:
                continue
            owner, method = found
            case = owner(method)
            cases.append(case)
            position_of[id(case)] = position
            for name in (owner.__module__, f"{owner.__module__}.{owner.__qualname__}"):
                of_owner.setdefault(name, []).append(position)
        record = Record(position_of, of_owner)
        started = time.process_time()
        lowered(TEST_DEPTH - frames() - self.between, unittest.TestSuite(cases).run, record)
        return {
            "o": "".join(record.letter(position) for position in positions),
            "x": {
                "test_cpu": round(time.process_time() - started, 6),  # this process only
                "subtests": record.subtests,
                "error": record.first_error,
            },
        }


# ----------------------------------------------------------------- self-check


def cgroup_cpu() -> float | None:
    """CPU seconds every process of this container has used so far, or None outside one."""
    try:
        with open("/sys/fs/cgroup/cpu.stat", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("usage_usec "):
                    return int(line.split()[1]) / 1e6
    except OSError:
        pass
    return None


def status_field(name: str) -> int:
    """A number from /proc/self/status: Threads, or VmRSS in kB."""
    with open("/proc/self/status", encoding="ascii") as handle:
        for line in handle:
            if line.startswith(name + ":"):
                return int(line.split()[1])
    return 0


def spent(before: float | None) -> float | None:
    now = cgroup_cpu()
    return None if now is None or before is None else round(now - before, 6)


def empty_forks(count: int) -> dict:
    """What a fork costs when the child does nothing at all."""
    before, started = cgroup_cpu(), time.monotonic()
    for _ in range(count):
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
    used = spent(before)
    return {
        "count": count,
        "cgroup_cpu_each": None if used is None else round(used / count, 6),
        "seconds_each": round((time.monotonic() - started) / count, 6),
    }


def map_reasons(map_directory: str, tests: list[dict]) -> dict[int, list[str]]:
    """Tests the map itself says must not be judged by a run in a child."""
    import selector

    chooser = selector.Selector(map_directory, tests)
    late = readers = 0
    for name in chooser.map.index["chunks"]:
        if name.startswith("#"):
            continue
        source = chooser.source(name)
        readers |= chooser.tests(source.readers)
        for _, _, flag, set_id in source.statements.values():
            if flag == 2:
                late |= chooser.tests(set_id)
    reasons: dict[int, list[str]] = {}
    for position in selector.bits(late):
        reasons.setdefault(position, []).append(
            "ran a statement that a test only reaches after other tests (the dialect registry)"
        )
    for position in selector.bits(readers):
        reasons.setdefault(position, []).append(
            "reads sqlglot source files as text, where a patch made in memory does not show"
        )
    return reasons


def selfcheck(options: argparse.Namespace) -> int:
    """Run every test alone on the unpatched code and print what was seen, as JSON."""
    container_before = cgroup_cpu()
    with open(options.tests, encoding="utf-8") as handle:
        tests = json.load(handle)["tests"]
    started = time.monotonic()
    runner = Runner(options.root, tests)
    runner.preload(tuple(name for name in options.preload.split(",") if name))
    startup = {
        "process_cpu": round(time.process_time(), 6),  # counted from the start of the process
        "cgroup_cpu": cgroup_cpu(),  # the same for the whole container
        "seconds": round(time.monotonic() - started, 3),
        "threads": status_field("Threads"),
        "rss_mb": round(status_field("VmRSS") / 1024, 1),
        "modules": len(sys.modules),
        "sqlglot_modules": sum(1 for name in sys.modules if name.split(".")[0] == "sqlglot"),
        "frames_between": runner.between,
        "cgroup_cpu_before_python_main": container_before,
    }
    log(f"selfcheck: start-up {startup}")
    forks = empty_forks(options.forks)
    before = cgroup_cpu()
    runner.run([(n, [], 10.0) for n in range(options.forks)], parallel=1)
    used = spent(before)
    forks["cgroup_cpu_each_empty_job"] = None if used is None else round(used / options.forks, 6)
    log(f"selfcheck: empty forks {forks}")

    chosen = [
        position
        for position, test in enumerate(tests)
        if not options.only or any(part in test["id"] for part in options.only.split(","))
    ]
    runner.watching = True
    runner.keep_stderr = options.keep_stderr
    before, started = cgroup_cpu(), time.monotonic()
    # The slowest first, so they do not become the tail.
    order = sorted(chosen, key=lambda position: -tests[position]["seconds"])
    jobs = [(position, [position], options.budget) for position in order]
    outcomes = runner.run(jobs, parallel=options.jobs)
    whole = {"cgroup_cpu": spent(before), "seconds": round(time.monotonic() - started, 1)}
    log(f"selfcheck: {len(chosen)} tests {whole}")

    reasons = map_reasons(options.map, tests) if options.map else {}
    records = {}
    for position in chosen:
        outcome, test = outcomes[position], tests[position]
        mine = reasons.setdefault(position, [])
        if position in runner.unknown:
            mine.insert(0, f"the runner cannot run it: {runner.unknown[position]}")
        elif outcome.letters is None:
            mine.insert(0, f"alone on the unpatched code its run {outcome.why}")
        else:
            letter = outcome.letters[position]
            starts = outcome.extra.get("starts", [])
            if starts:
                mine.insert(
                    0,
                    f"starts other processes ({', '.join(starts)}): they load sqlglot from"
                    " disk, where a patch made in memory does not show",
                )
            if letter == "F":
                mine.insert(0, f"fails on the unpatched code here: {outcome.extra['error']}")
            elif letter != "P":
                mine.insert(0, "is skipped here" if letter == "S" else "does not run here")
        records[test["id"]] = {
            "letter": None if outcome.letters is None else outcome.letters[position],
            "cpu": round(outcome.cpu, 6),
            "seconds": round(outcome.seconds, 4),
            "recorded_seconds": test["seconds"],
            **outcome.extra,
        }
    checked = set(chosen)
    never = {tests[p]["id"]: why for p, why in sorted(reasons.items()) if why and p in checked}
    report = {
        "python": sys.version.split()[0],
        "flags": {"jobs": options.jobs, "budget": options.budget, "preload": options.preload},
        "startup": startup,
        "forks": forks,
        "whole_pass": whole,
        "never_verify": never,
        "tests": records,
    }
    json.dump(report, sys.stdout, indent=1, sort_keys=True)
    sys.stdout.write("\n")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    modes = parser.add_subparsers(dest="mode", required=True)
    check = modes.add_parser("selfcheck", help=selfcheck.__doc__)
    check.add_argument("--root", default=PINNED)
    check.add_argument("--tests", default="/data/tests.json")
    check.add_argument("--map", default=os.path.join(os.path.dirname(__file__), "map"))
    check.add_argument("--jobs", type=int, default=2, help="children at once")
    check.add_argument("--budget", type=float, default=300.0, help="seconds for one test")
    check.add_argument("--forks", type=int, default=200, help="empty forks to time")
    check.add_argument("--only", default="", help="comma-separated parts of test IDs")
    check.add_argument("--preload", default="", help="comma-separated modules to import first")
    check.add_argument("--keep-stderr", action="store_true")
    return selfcheck(parser.parse_args())


if __name__ == "__main__":
    sys.exit(main())
