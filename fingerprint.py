"""Fingerprint of every sqlglot module global and class attribute.

The selector runs this in a fresh interpreter, once on the pristine tree and
once with the patched modules served from memory, and keeps a narrower class
selection only when nothing outside that class and its subclasses changed.

  stdin   {"files": {"sqlglot/dialects/postgres.py": "<source>"}}
          an empty files object is the pristine tree
  stdout  {"values": {"<key>": "<sha256>"}, "unknown": ["<key>", ...]}
          or {"error": "<why>"}

A function is its bytecode, constants and defaults. A class is its qualified
name and MRO. Containers are walked. Anything else is its type and repr with
the memory address removed; a value whose repr still contains an address, or
whose repr raises, is unknown.
"""

from __future__ import annotations

import enum
import hashlib
import importlib
import importlib.util
import json
import pkgutil
import re
import sys
import types

ADDRESS = re.compile(r" at 0x[0-9a-fA-F]+")
SKIP_MODULE = {
    "__builtins__",
    "__cached__",
    "__file__",
    "__loader__",
    "__name__",
    "__package__",
    "__path__",
    "__spec__",
}
SKIP_CLASS = {"__dict__", "__module__", "__weakref__"}


def qual(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def sqlglot_class(value: object) -> bool:
    if not isinstance(value, type):
        return False
    module = getattr(value, "__module__", "") or ""
    return module == "sqlglot" or module.startswith("sqlglot.")


def dump(value: object, seen: set[int], bad: list[str], key: str, depth: int = 0) -> object:
    """A JSON-ready description of value. Appends key to bad when it gives up."""
    if depth > 40:
        bad.append(key)
        return ["unknown", "depth"]
    if value is None or isinstance(value, (bool, int, str)):
        return ["v", type(value).__name__, value]
    if isinstance(value, float):
        return ["v", "float", repr(value)]
    if isinstance(value, bytes):
        return ["v", "bytes", value.hex()]
    if isinstance(value, enum.Enum):
        return ["e", qual(type(value)), value.name, dump(value.value, seen, bad, key, depth + 1)]
    if isinstance(value, types.CodeType):
        return [
            "code",
            value.co_code.hex(),
            list(value.co_names),
            dump(value.co_consts, seen, bad, key, depth + 1),
        ]
    if isinstance(value, types.FunctionType):
        return [
            "f",
            dump(value.__code__, seen, bad, key, depth + 1),
            dump(value.__defaults__, seen, bad, key, depth + 1),
            dump(value.__kwdefaults__, seen, bad, key, depth + 1),
        ]
    if isinstance(value, (staticmethod, classmethod)):
        return ["w", dump(value.__func__, seen, bad, key, depth + 1)]
    if isinstance(value, property):
        return [
            "p",
            dump(value.fget, seen, bad, key, depth + 1),
            dump(value.fset, seen, bad, key, depth + 1),
            dump(value.fdel, seen, bad, key, depth + 1),
        ]
    if isinstance(value, type):
        return ["c", qual(value), [qual(base) for base in value.__mro__]]
    ident = id(value)
    if ident in seen:
        return ["cycle"]
    if isinstance(value, (list, tuple)):
        # A tuple of classes is often built from a set, so its order follows the
        # addresses of those classes. Describe it by the sorted names.
        if value and all(isinstance(item, type) for item in value):
            return ["types", sorted(qual(item) for item in value)]
        seen.add(ident)
        tag = "l" if isinstance(value, list) else "t"
        return [tag, [dump(item, seen, bad, key, depth + 1) for item in value]]
    if isinstance(value, (set, frozenset)):
        # A set of objects iterates by identity. Dump in description order so two
        # processes agree about which shared object is the cycle.
        seen.add(ident)
        ranked = sorted(
            value,
            key=lambda item: json.dumps(
                dump(item, set(seen), bad, key, depth + 1),
                separators=(",", ":"),
                ensure_ascii=True,
            ),
        )
        items = [dump(item, seen, bad, key, depth + 1) for item in ranked]
        return ["s" if isinstance(value, set) else "z", items]
    if isinstance(value, dict):
        seen.add(ident)
        ranked = []
        for item_key, item_value in value.items():
            dumped_key = dump(item_key, set(seen), bad, key, depth + 1)
            ranked.append((dumped_key, item_key, item_value))
        ranked.sort(key=lambda item: json.dumps(item[0], separators=(",", ":"), ensure_ascii=True))
        pairs = []
        for dumped_key, item_key, item_value in ranked:
            pairs.append(
                [
                    dump(item_key, seen, bad, key, depth + 1),
                    dump(item_value, seen, bad, key, depth + 1),
                ]
            )
        return ["d", pairs]
    try:
        text = ADDRESS.sub("", repr(value))
    except Exception:
        bad.append(key)
        return ["unknown", type(value).__name__]
    return ["r", f"{type(value).__module__}.{type(value).__qualname__}", text]


def digest(node: object) -> str:
    text = json.dumps(node, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class _Loader:
    def __init__(self, source: str, origin: str, search: list[str] | None) -> None:
        self.source = source
        self.origin = origin
        self.search = search

    def create_module(self, spec: object) -> None:
        return None

    def exec_module(self, module: types.ModuleType) -> None:
        if self.search is not None:
            module.__path__ = list(self.search)
        module.__file__ = self.origin
        # Bytes, not text: that is how Python compiles a file it imports, and a
        # string does not always compile to the same code object.
        code = compile(self.source.encode("latin-1"), self.origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)


def install(files: dict[str, str]) -> None:
    """Serve the patched modules from memory. Everything else stays on disk."""

    class Finder:
        def find_spec(self, fullname: str, path: object, target: object = None):
            relative = fullname.replace(".", "/")
            chosen = relative + ".py"
            if chosen not in files:
                chosen = relative + "/__init__.py"
            if chosen not in files:
                return None
            sys.meta_path.remove(self)
            try:
                spec = importlib.util.find_spec(fullname)
            finally:
                sys.meta_path.insert(0, self)
            if spec is None:
                return None
            search = list(spec.submodule_search_locations or []) or None
            spec.loader = _Loader(files[chosen], spec.origin or chosen, search)
            spec.cached = None
            return spec

    sys.meta_path.insert(0, Finder())


def capture(files: dict[str, str], apply: bool = True) -> dict:
    # The patched sources are allocated before import in both processes. A set
    # of classes is ordered by address, so the two processes must allocate the
    # same things before those classes are created. The finder is installed
    # either way, for the same reason; it only serves source when apply is set.
    held = tuple(files.values())
    install(files if apply else {})
    del held
    sys.path.insert(0, "/app/pinned")
    import sqlglot

    names = []
    for info in pkgutil.walk_packages(sqlglot.__path__, prefix="sqlglot."):
        if not info.name.endswith(".__main__"):
            names.append(info.name)
    modules: list[types.ModuleType] = [sqlglot]
    for name in sorted(names):
        modules.append(importlib.import_module(name))
    values: dict[str, str] = {}
    unknown: list[str] = []
    seen_classes: set[int] = set()

    def emit_class(cls: type) -> None:
        if id(cls) in seen_classes or not sqlglot_class(cls):
            return
        seen_classes.add(id(cls))
        prefix = f"c:{qual(cls)}"
        for name, item in sorted(vars(cls).items()):
            if name in SKIP_CLASS:
                continue
            key = f"{prefix}:{name}"
            bad: list[str] = []
            values[key] = digest(dump(item, set(), bad, key))
            if bad:
                unknown.append(key)
            if isinstance(item, type):
                emit_class(item)

    for module in modules:
        for name, item in sorted(vars(module).items()):
            if name in SKIP_MODULE:
                continue
            key = f"m:{module.__name__}:{name}"
            bad = []
            values[key] = digest(dump(item, set(), bad, key))
            if bad:
                unknown.append(key)
            if isinstance(item, type):
                emit_class(item)
    return {"values": values, "unknown": unknown}


def main() -> None:
    raw = sys.stdin.read()
    request = json.loads(raw) if raw.strip() else {}
    try:
        result = capture(request.get("files") or {}, apply=bool(request.get("apply", True)))
    except Exception as error:
        result = {"error": f"{type(error).__name__}: {error}"}
    json.dump(result, sys.stdout, separators=(",", ":"))


if __name__ == "__main__":
    main()
