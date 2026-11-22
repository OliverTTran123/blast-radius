"""pytest plugin: the sqlglot classes one test uses.

Loaded with `-p class_plugin` while each test runs in its own process. After a
sqlglot module is imported it wraps that module's classes, so a later call
records the class. Three things count:

  - an instance is constructed (__init__ or __new__): the instance's class and
    every sqlglot class in its MRO, plus the classes it is nested inside
  - Dialect.get / __getitem__ / get_or_raise, or a module __getattr__, returns
    a class
  - a method runs: that is not seen here. The map already knows it from
    coverage, and class_assemble.py adds it

The process writes one JSON list of qualified names to $CLASS_OUT.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
import types

import pytest

USED: set[str] = set()
_WRAPPED: set[int] = set()


def _qual(cls: type) -> str:
    return f"{cls.__module__}.{cls.__qualname__}"


def _sqlglot(cls: type) -> bool:
    module = getattr(cls, "__module__", "") or ""
    return module == "sqlglot" or module.startswith("sqlglot.")


def note(cls: object) -> None:
    """Record cls, its sqlglot bases, and the classes it is nested in."""
    if not isinstance(cls, type) or not _sqlglot(cls):
        return
    for item in cls.__mro__:
        if item is object or not _sqlglot(item):
            continue
        USED.add(_qual(item))
    module = cls.__module__
    parts = cls.__qualname__.split(".")
    for length in range(1, len(parts)):
        USED.add(f"{module}.{'.'.join(parts[:length])}")


def _mark(func: types.FunctionType) -> types.FunctionType:
    def wrapped(*args, **kwargs):
        result = func(*args, **kwargs)
        if isinstance(result, type):
            note(result)
        return result

    wrapped.__wrapped__ = func
    return wrapped


def _defined_here(func: object) -> bool:
    """True for a function written in sqlglot. Enum installs its own __new__."""
    module = getattr(func, "__module__", "") or ""
    return module == "sqlglot" or module.startswith("sqlglot.")


def _wrap_class(cls: type) -> None:
    if id(cls) in _WRAPPED or not _sqlglot(cls):
        return
    _WRAPPED.add(id(cls))
    init = cls.__dict__.get("__init__")
    if _defined_here(init):

        def __init__(self, *args, _init=init, **kwargs):
            note(type(self))
            return _init(self, *args, **kwargs)

        __init__.__wrapped__ = init
        cls.__init__ = __init__
    new = cls.__dict__.get("__new__")
    if _defined_here(new):

        def __new__(cls_, *args, _new=new, **kwargs):
            obj = _new(cls_, *args, **kwargs)
            if isinstance(obj, type):
                note(obj)
            elif isinstance(obj, cls_):
                note(type(obj))
            return obj

        __new__.__wrapped__ = new
        cls.__new__ = __new__
    # Only classmethods. A plain function named get is an instance method, and
    # replacing it with a classmethod changes what the tests run.
    for name in ("get", "__getitem__", "get_or_raise"):
        func = cls.__dict__.get(name)
        if isinstance(func, classmethod) and _defined_here(func.__func__):
            setattr(cls, name, classmethod(_mark(func.__func__)))
    for value in list(vars(cls).values()):
        if isinstance(value, type):
            _wrap_class(value)


def _wrap_module(module: types.ModuleType) -> None:
    for value in list(vars(module).values()):
        if isinstance(value, type):
            _wrap_class(value)
    getattr_ = module.__dict__.get("__getattr__")
    if isinstance(getattr_, types.FunctionType) and not getattr(getattr_, "_br_class", False):
        marked = _mark(getattr_)
        marked._br_class = True  # type: ignore[attr-defined]
        module.__getattr__ = marked


class _Loader:
    """Runs the real loader, then wraps whatever classes the module defined."""

    def __init__(self, inner: object) -> None:
        self.inner = inner

    def create_module(self, spec: object):
        create = getattr(self.inner, "create_module", None)
        return create(spec) if create is not None else None

    def exec_module(self, module: types.ModuleType) -> None:
        self.inner.exec_module(module)  # type: ignore[attr-defined]
        _wrap_module(module)


class _Finder:
    def find_spec(self, fullname: str, path: object, target: object = None):
        if not (fullname == "sqlglot" or fullname.startswith("sqlglot.")):
            return None
        sys.meta_path.remove(self)
        try:
            spec = importlib.util.find_spec(fullname)
        finally:
            sys.meta_path.insert(0, self)
        if spec is None or spec.loader is None or isinstance(spec.loader, _Loader):
            return spec
        spec.loader = _Loader(spec.loader)
        return spec


sys.meta_path.insert(0, _Finder())


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    out = os.environ.get("CLASS_OUT")
    if not out:
        return
    payload = json.dumps(sorted(USED), separators=(",", ":")).encode("utf-8")
    descriptor = os.open(out, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
