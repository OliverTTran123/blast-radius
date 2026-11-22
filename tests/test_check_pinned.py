"""Behavior tests for tools/image/check_pinned.py: the image build stops on any source mismatch.

python3 -B -m unittest discover -s tests
"""

import hashlib
import io
import json
import shutil
import sys
import tempfile
import unittest
import zlib
from contextlib import redirect_stdout
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools" / "image"))

import check_pinned  # noqa: E402

SOURCES = {
    "sqlglot/__init__.py": b"from sqlglot.core import VALUE\n",
    "sqlglot/core.py": b"VALUE = 1\n",
    "sqlglot/dialects/one.py": b"NAME = 'one'\n",
}


class CheckPinnedTest(unittest.TestCase):
    def setUp(self):
        self.work = Path(tempfile.mkdtemp(prefix="check-pinned-"))
        self.addCleanup(shutil.rmtree, self.work, ignore_errors=True)
        # A map as assemble.py writes it: each source compressed on its own, end to end.
        index = {"sources": {}, "sqlglot_commit": "0" * 40}
        packed = b""
        digest = hashlib.sha256()
        for name in sorted(SOURCES):
            piece = zlib.compress(SOURCES[name], 9)
            index["sources"][name] = [len(packed), len(piece), "tree fingerprint"]
            packed += piece
            digest.update(name.encode() + b"\0" + SOURCES[name] + b"\0")
        index["sources_sha256"] = digest.hexdigest()
        self.map = self.work / "map"
        self.map.mkdir()
        (self.map / "index.json").write_text(json.dumps(index))
        (self.map / "sources.bin").write_bytes(packed)
        self.checkout = self.work / "checkout"
        for name, data in SOURCES.items():
            self.write(name, data)
        self.write("tests/test_one.py", b"import unittest\n")
        self.write("tests/fixtures/one.sql", b"SELECT 1;\n")
        self.write("docs/readme.txt", b"not kept\n")
        self.destination = self.work / "pinned"

    def write(self, name: str, data: bytes) -> None:
        path = self.checkout / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)

    def check(self) -> str:
        said = io.StringIO()
        with redirect_stdout(said):
            check_pinned.main(self.checkout, self.destination, self.map)
        return said.getvalue()

    def refused(self) -> str:
        with self.assertRaises(SystemExit) as stopped:
            self.check()
        self.assertFalse(self.destination.exists())  # nothing is kept from a wrong checkout
        return str(stopped.exception)

    def test_matching_checkout_is_kept_and_compiled(self):
        self.assertIn("3 sqlglot source files match the map", self.check())
        self.assertEqual(
            sorted(path.name for path in self.destination.iterdir()), ["sqlglot", "tests"]
        )
        self.assertEqual(
            (self.destination / "sqlglot/core.py").read_bytes(), SOURCES["sqlglot/core.py"]
        )
        self.assertTrue((self.destination / "tests/fixtures/one.sql").exists())
        compiled = list((self.destination / "sqlglot/__pycache__").glob("core.*.pyc"))
        self.assertEqual(len(compiled), 1)
        # Bytes 4 to 8 of a .pyc are its flags: 1 is hash-based, and without 2 it is unchecked.
        self.assertEqual(int.from_bytes(compiled[0].read_bytes()[4:8], "little"), 1)
        for path in self.destination.rglob("*"):
            mode = path.stat().st_mode & 0o777
            self.assertEqual(mode, 0o755 if path.is_dir() else 0o644, path)

    def test_a_changed_source_stops_the_build(self):
        self.write("sqlglot/core.py", b"VALUE = 2\n")
        self.assertIn("differs from the map: sqlglot/core.py", self.refused())

    def test_a_missing_source_stops_the_build(self):
        (self.checkout / "sqlglot/dialects/one.py").unlink()
        self.assertIn("missing: sqlglot/dialects/one.py", self.refused())

    def test_an_extra_source_stops_the_build(self):
        self.write("sqlglot/dialects/two.py", b"NAME = 'two'\n")
        self.assertIn("not in the map: sqlglot/dialects/two.py", self.refused())


if __name__ == "__main__":
    unittest.main()
