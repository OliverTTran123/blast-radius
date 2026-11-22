#!/usr/bin/env python3
"""Fold class use into map/ as one extra chunk, #classes.

The statement rows already in the map stay as they are. This adds, per class,
the tests that used it and, per source file, which import-time statements sit
directly in which class body. Run it with the same Python that built the map.

  class_assemble.py --map map --used /work/classes --tests /data/tests.json
"""

from __future__ import annotations

import argparse
import ast
import json
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import statements  # noqa: E402


def module_of(path: str) -> str:
    relative = path[:-3] if path.endswith(".py") else path
    if relative.endswith("/__init__"):
        relative = relative[: -len("/__init__")]
    return relative.replace("/", ".")


def class_info(source: str, module: str) -> tuple[dict[int, str], list[tuple[int, int, str]]]:
    """Statement line -> enclosing class, and each class's (start, end, name)."""
    tree = ast.parse(source)
    where: dict[int, str] = {}
    spans: list[tuple[int, int, str]] = []

    def walk(body: list[ast.stmt], stack: list[str]) -> None:
        for stmt in body:
            if not isinstance(stmt, ast.ClassDef):
                continue
            qual = module + "." + ".".join([*stack, stmt.name])
            spans.append((statements.first_line(stmt), stmt.end_lineno or stmt.lineno, qual))
            for sub in stmt.body:
                where[statements.first_line(sub)] = qual
            walk(stmt.body, [*stack, stmt.name])

    walk(tree.body, [])
    return where, spans


def masks_of(index: dict, directory: Path) -> list[int]:
    raw = zlib.decompress((directory / "sets.bin").read_bytes())
    width = index["set_bytes"]
    return [int.from_bytes(raw[i * width : (i + 1) * width], "little") for i in range(index["sets"])]


def read_source(blob: bytes, where: list[int]) -> str:
    return zlib.decompress(blob[where[0] : where[0] + where[1]]).decode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--map", required=True)
    parser.add_argument("--used", required=True)
    parser.add_argument("--tests", required=True)
    options = parser.parse_args()
    directory = Path(options.map)
    index = json.loads((directory / "index.json").read_text(encoding="utf-8"))
    if index["format"] != 2:
        print(f"map format {index['format']} is not supported", file=sys.stderr)
        return 1
    if "#classes" in index["chunks"]:
        print("map already has #classes", file=sys.stderr)
        return 1
    tests = json.loads(Path(options.tests).read_text(encoding="utf-8"))["tests"]
    if len(tests) != index["tests"]:
        print(f"tests.json has {len(tests)} tests, the map has {index['tests']}", file=sys.stderr)
        return 1
    sets = masks_of(index, directory)
    files = (directory / "files.bin").read_bytes()
    sources = (directory / "sources.bin").read_bytes()
    used_root = Path(options.used)
    hierarchy = json.loads((used_root / "hierarchy.json").read_text(encoding="utf-8"))

    used_by: dict[str, int] = {}
    missing: list[int] = []
    for test in tests:
        path = used_root / f"{test['number']:04d}" / "used.json"
        if not path.exists():
            missing.append(test["number"])
            continue
        for name in json.loads(path.read_text(encoding="utf-8")):
            if name in hierarchy:
                used_by[name] = used_by.get(name, 0) | (1 << test["number"])

    where_all: dict[str, dict[str, str]] = {}
    for path, where in index["sources"].items():
        if not path.startswith("sqlglot/") or not path.endswith(".py"):
            continue
        source = read_source(sources, where)
        try:
            direct, spans = class_info(source, module_of(path))
        except SyntaxError:
            continue
        if direct:
            where_all[path] = {str(line): name for line, name in sorted(direct.items())}
        chunk_at = index["chunks"].get(path)
        if chunk_at is None:
            continue
        chunk = json.loads(zlib.decompress(files[chunk_at[0] : chunk_at[0] + chunk_at[1]]))
        for start, _end, _parent, flag, set_id in chunk["statements"]:
            if flag == 1 or set_id < 0:
                continue
            holders = [qual for begin, end, qual in spans if begin <= start <= end]
            for qual in holders:
                if qual in hierarchy:
                    used_by[qual] = used_by.get(qual, 0) | sets[set_id]

    if missing:
        # A test with no record could have used anything, so it stays selected.
        every = sum(1 << number for number in missing)
        for name in hierarchy:
            used_by[name] = used_by.get(name, 0) | every

    names = sorted(hierarchy)
    index_of = {name: position for position, name in enumerate(names)}
    children: list[list[int]] = [[] for _ in names]
    for name, info in hierarchy.items():
        for base in info["bases"]:
            if base in index_of and name in index_of:
                children[index_of[base]].append(index_of[name])
    for kids in children:
        kids.sort()

    fresh: dict[int, int] = {}
    added: list[int] = []

    def add(mask: int) -> int:
        if mask == 0:
            return -1
        if mask in fresh:
            return fresh[mask]
        fresh[mask] = len(sets) + len(added)
        added.append(mask)
        return fresh[mask]

    where_ids = {
        path: {line: index_of[name] for line, name in lines.items() if name in index_of}
        for path, lines in sorted(where_all.items())
    }
    chunk = {
        "names": names,
        "enum": [1 if hierarchy[name]["enum"] else 0 for name in names],
        "meta": [1 if hierarchy[name]["meta"] else 0 for name in names],
        "children": children,
        "used": [add(used_by.get(name, 0)) for name in names],
        "where": where_ids,
    }
    packed = zlib.compress(json.dumps(chunk, separators=(",", ":"), sort_keys=True).encode(), 9)
    index["chunks"]["#classes"] = [len(files), len(packed)]
    width = index["set_bytes"]
    extra = b"".join(mask.to_bytes(width, "little") for mask in added)
    raw = zlib.decompress((directory / "sets.bin").read_bytes()) + extra
    (directory / "sets.bin").write_bytes(zlib.compress(raw, 9))
    (directory / "files.bin").write_bytes(files + packed)
    index["sets"] = len(sets) + len(added)
    (directory / "index.json").write_text(json.dumps(index, indent=1, sort_keys=True) + "\n", "utf-8")
    nonempty = sum(mask != 0 for mask in (used_by.get(name, 0) for name in names))
    print(
        f"classes: {len(names)} named, {nonempty} used, {len(added)} new sets, "
        f"chunk {len(packed)} bytes, missing tests {len(missing)}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
