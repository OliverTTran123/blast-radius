"""Checks a fetched sqlglot checkout against the map and keeps what the runner needs.

Usage: check_pinned.py <checkout> <destination> <map directory>

Runs while the scored image is built, after tools/build_map/fetch_sqlglot.py.
The map was made from one exact set of source files, and map/sources.bin holds
every one of them. A patch is only placed correctly, and a test run only means
what the map says, if the image holds those same files. So every
sqlglot/**/*.py of the checkout must be byte for byte the file in the map, with
none missing and none extra, or the build stops. The archive itself is not
hashed: GitHub does not promise the same archive bytes twice, and the files
are what matters.

Then sqlglot/ and tests/ move to the destination, are compiled, and are made
readable by any user. The image runs as nobody on a read-only disk, so
bytecode has to exist already. It is written as unchecked hash-based .pyc
files, which Python loads without comparing timestamps.
"""

import compileall
import hashlib
import json
import os
import py_compile
import shutil
import sys
import zlib
from pathlib import Path

KEPT = ("sqlglot", "tests")


def main(checkout: Path, destination: Path, map_directory: Path) -> None:
    index = json.loads((map_directory / "index.json").read_text("utf-8"))
    packed = (map_directory / "sources.bin").read_bytes()
    expected = {
        name: zlib.decompress(packed[offset : offset + length])
        for name, (offset, length, _) in index["sources"].items()
    }
    found = {
        path.relative_to(checkout).as_posix(): path.read_bytes()
        for path in (checkout / "sqlglot").rglob("*.py")
        if path.is_file()
    }
    problems = [f"missing: {name}" for name in sorted(set(expected) - set(found))]
    problems += [f"not in the map: {name}" for name in sorted(set(found) - set(expected))]
    problems += [
        f"differs from the map: {name}"
        for name in sorted(set(found) & set(expected))
        if found[name] != expected[name]
    ]
    # The map's own fingerprint of all its sources, computed the way assemble.py does.
    digest = hashlib.sha256()
    for name in sorted(found):
        digest.update(name.encode("utf-8") + b"\0" + found[name] + b"\0")
    if not problems and digest.hexdigest() != index["sources_sha256"]:
        problems.append("the sources' fingerprint is not the one in map/index.json")
    if problems:
        shown = "\n  ".join(problems[:20])
        sys.exit(f"the fetched sqlglot is not the one the map was built from:\n  {shown}")

    destination.mkdir(parents=True)
    for name in KEPT:
        shutil.move(checkout / name, destination / name)
    compiled = compileall.compile_dir(
        destination,
        quiet=1,
        invalidation_mode=py_compile.PycInvalidationMode.UNCHECKED_HASH,
    )
    if not compiled:
        sys.exit("a file of the pinned checkout did not compile")
    files = 0
    for directory, _, names in os.walk(destination):
        os.chmod(directory, 0o755)
        for name in names:
            os.chmod(os.path.join(directory, name), 0o644)
            files += 1
    print(
        f"{len(found)} sqlglot source files match the map ({index['sqlglot_commit']});"
        f" kept {', '.join(KEPT)}: {files} files, compiled"
    )


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]), Path(sys.argv[3]))
