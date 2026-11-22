#!/usr/bin/env python3
"""Makes the mutant validation set: small edits of the pinned sqlglot.

Runs inside the labeling image (eval/mutants/Dockerfile), where the pinned
checkout is at /sqlglot, with the repository mounted read-only at /repo:

    python /repo/eval/mutants/generate.py --out <directory>

It writes one git-style unified diff per mutant to <out>/commits/<id>.patch,
in the format of the kit's sample commits, and <out>/manifest.json, which says
what each mutant is. The same seeds, pinned commit and map give the same files.

The mutants are drawn at random from the whole code base. The sample commits
are never read. The point is to check the selector on commits whose failing
tests we can find out for ourselves, beyond the 30 in the kit. The selector
never reads this directory and nothing in it is copied into the image.

There are two batches. Each draws from its own seeded stream, so adding the
second left the first exactly as it was labeled.

What one edit does to a line (kind):
  compare    one comparison operator swapped: == with != or is, is with ==,
             < with <=, > with >=
  boolean    one `and` swapped with `or`
  negate     an `if` condition negated
  constant   True swapped with False, or an integer raised by one
  string     one character added to a string literal
  delete     the line removed: one element of a dict, set, list or tuple
             that has the line to itself
  comment    a comment line added that leaves the syntax tree alone
  in_string  a comment-looking line added inside a string that spans lines
  fixture    one line of tests/fixtures/optimizer/*.sql damaged
A mutant with several edits has the kind multi (one file) or two_files.

Batch 1: one line changed or one line added, like the sample commits. Where
the selector's map places the edited line (where). The last three are the
rules most likely to hide a miss, so each gets its own share:
  executed    in a statement inside a function that some test ran
  never       in a statement that no test ran and that does not run at import:
              the selector selects nothing for it
  class_body  in a statement directly in a class body: runs at import
  order       in a statement only reached after other tests have run
A comment, in_string or fixture mutant records its own kind of place instead:
comment, string (docstring when the string is one) or fixture.

Batch 2: what batch 1 left out. Where the edit is (where):
  class_attribute  a one-line assignment directly in a class body
  class_entry      one line of a longer assignment in a class body: an entry
                   of a dict or set, a string in it
  class_lambda     inside a lambda that a class-body statement holds
  module_constant  an assignment at module level, on one line or several
  test_method      in tests/**/*.py, inside a test method
  test_helper      in tests/**/*.py, inside any other function
  test_class       in tests/**/*.py, directly in a class body
  multi_adjacent   two neighbouring lines of one sqlglot file
  multi_near       three lines of one sqlglot file, close together
  multi_spread     four lines of one sqlglot file, far apart
  a+b              one edit in each of two files, placed at a and at b
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import random
import re
import sys
import tokenize
import warnings
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

SEED = 20261121
SEED_2 = 20261122  # the second batch's own stream
PINNED = Path("/sqlglot")
REPO = Path("/repo")
CONTEXT = 3  # unchanged lines git shows around a change

# Batch 1: (where, kinds, how many). 60 with the comments, strings and fixtures.
PLAN = [
    ("executed", ("compare",), 8),
    ("executed", ("boolean",), 7),
    ("executed", ("negate",), 5),
    ("executed", ("constant",), 8),
    ("executed", ("string",), 2),
    ("class_body", ("constant",), 5),
    ("class_body", ("string",), 2),
    ("class_body", ("compare", "boolean", "negate"), 1),
    ("never", ("compare", "boolean", "negate", "constant"), 6),
    ("order", ("compare", "boolean", "negate", "constant"), 1),
]
COMMENTS, IN_STRINGS, FIXTURES = 5, 2, 8

# Batch 2: single edits, then edits on several lines of a file, then pairs of files. 41 in all.
CODE = ("compare", "boolean", "negate", "constant", "string")
PLAN_2 = [
    ("class_attribute", ("constant",), 3),
    ("class_attribute", ("string",), 2),
    ("class_entry", ("string",), 3),
    ("class_entry", ("constant",), 2),
    ("class_entry", ("delete",), 4),
    ("class_lambda", CODE, 4),
    ("module_constant", ("constant", "string"), 4),
    ("module_constant", ("delete",), 2),
    ("test_method", ("string",), 2),
    ("test_method", ("constant", "compare", "boolean"), 1),
    ("test_helper", CODE, 2),
    ("test_class", ("constant", "string"), 1),
]
SEVERAL = ["adjacent", "adjacent", "near", "near", "spread", "spread"]
PAIRS = [
    ("executed", "executed"),
    ("executed", "executed"),
    ("executed", "fixture"),
    ("executed", "test_method"),
    ("class_body", "executed"),
]

REMARKS = [
    "Order matters here",
    "See the dialect docs",
    "Handled by the caller",
    "Guard against empty input",
    "Fast path",
    "Assumes a normalized tree",
    "Keep this list sorted",
    "Covered by the transpile tests",
]
SWAPS = {
    ("==",): ("!=", "is"),
    ("!=",): ("==",),
    ("is",): ("==",),
    ("is", "not"): ("!=",),
    ("<",): ("<=",),
    ("<=",): ("<",),
    (">",): (">=",),
    (">=",): (">",),
}
PLAIN_STRING = re.compile(r"""[rRuU]?(?P<quote>['"])(?!(?P=quote)(?P=quote)).*(?P=quote)""", re.S)


@dataclass
class Edit:
    """One line of a file changed, removed, or a new line put in front of it."""

    path: str  # repo-relative
    line: int  # 1-based line of the pinned file; an added line goes in front of it
    new: str | None  # the line's new text or the added line; None removes the line
    kind: str
    added: bool = False
    where: str = ""
    tests: int | None = None
    scope: str = ""  # the classes, defs and lambdas around it, outermost first


@dataclass
class Mutant:
    edits: list[Edit]
    kind: str
    where: str
    batch: int
    key: str  # what its id is derived from
    tests: int | None = None


# ----------------------------------------------------------------- edit sites


class Sites(ast.NodeVisitor):
    """Every place in one file where a single-line edit of a known kind can be made."""

    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.lines = text.split("\n")
        self.tokens = [
            token
            for token in tokenize.generate_tokens(io.StringIO(text).readline)
            if token.type in (tokenize.OP, tokenize.NAME, tokenize.NUMBER, tokenize.STRING)
        ]
        self.at = {token.start: token for token in self.tokens}
        self.edits: list[Edit] = []
        self.class_lines: set[int] = set()
        self.multiline_strings: list[tuple[int, int, bool]] = []
        self.docstrings: set[int] = set()
        self.in_annotation = 0
        self.scope: list[str] = []
        # Lines of an assignment directly in the module or a class body: "one" line or "many".
        self.assigned: dict[int, str] = {}

    def plain(self, line: int) -> bool:
        """ASCII only: there a column is the same to ast (bytes) and tokenize (characters)."""
        return self.lines[line - 1].isascii()

    def add(self, line: int, new: str | None, kind: str) -> None:
        self.edits.append(Edit(self.path, line, new, kind, scope=">".join(self.scope)))

    def replace(self, line: int, start: int, end: int, text: str, kind: str) -> None:
        old = self.lines[line - 1]
        self.add(line, old[:start] + text + old[end:], kind)

    def between(self, left: ast.expr, right: ast.expr) -> list[tokenize.TokenInfo]:
        """The operator tokens between two operands, brackets aside."""
        start, end = (left.end_lineno, left.end_col_offset), (right.lineno, right.col_offset)
        found = [t for t in self.tokens if start <= t.start and t.end <= end]
        return [t for t in found if t.string not in ("(", ")")]

    def note_assignments(self, body: list[ast.stmt]) -> None:
        for statement in body:
            if isinstance(statement, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                last = statement.end_lineno or statement.lineno
                for line in range(statement.lineno, last + 1):
                    self.assigned[line] = "one" if last == statement.lineno else "many"

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        self.note_docstring(node)
        self.note_assignments(node.body)
        self.scope.append(f"class {node.name}")
        self.generic_visit(node)
        self.scope.pop()

    def visit_Module(self, node: ast.Module) -> None:
        self.note_docstring(node)
        self.note_assignments(node.body)
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self.note_docstring(node)
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.in_annotation += 1  # type hints are not behaviour: leave them alone
        self.visit(node.args)
        if node.returns:
            self.visit(node.returns)
        self.in_annotation -= 1
        for default in [*node.args.defaults, *(d for d in node.args.kw_defaults if d)]:
            self.visit(default)
        self.scope.append(f"def {node.name}")
        for statement in node.body:
            self.visit(statement)
        self.scope.pop()

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.scope.append("lambda")
        self.generic_visit(node)
        self.scope.pop()

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        self.visit(node.target)
        if node.value:
            self.visit(node.value)

    def note_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", [])
        first = body[0].value if body and isinstance(body[0], ast.Expr) else None
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            self.docstrings.add(id(first))

    def visit_Compare(self, node: ast.Compare) -> None:
        if len(node.ops) == 1 and not self.in_annotation:
            tokens = self.between(node.left, node.comparators[0])
            swaps = SWAPS.get(tuple(token.string for token in tokens))
            line = tokens[0].start[0] if tokens else 0
            if swaps and all(token.start[0] == line for token in tokens) and self.plain(line):
                for swap in swaps:
                    self.replace(line, tokens[0].start[1], tokens[-1].end[1], swap, "compare")
        self.generic_visit(node)

    def visit_BoolOp(self, node: ast.BoolOp) -> None:
        word, other = ("and", "or") if isinstance(node.op, ast.And) else ("or", "and")
        for left, right in zip(node.values, node.values[1:], strict=False):
            tokens = [token for token in self.between(left, right) if token.string == word]
            if len(tokens) == 1 and self.plain(tokens[0].start[0]) and not self.in_annotation:
                token = tokens[0]
                self.replace(token.start[0], token.start[1], token.end[1], other, "boolean")
        self.generic_visit(node)

    def visit_If(self, node: ast.If) -> None:
        test = node.test
        if test.lineno == test.end_lineno and self.plain(test.lineno):
            start, end = test.col_offset, test.end_col_offset
            text = self.lines[test.lineno - 1][start:end]
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                word = self.at.get((test.lineno, start))
                if word is not None and word.string == "not":
                    rest = self.lines[test.lineno - 1][word.end[1] :].lstrip()
                    self.add(test.lineno, self.lines[test.lineno - 1][:start] + rest, "negate")
            elif isinstance(test, (ast.BoolOp, ast.IfExp, ast.NamedExpr, ast.Lambda)):
                self.replace(test.lineno, start, end, f"not ({text})", "negate")
            else:
                self.replace(test.lineno, start, end, f"not {text}", "negate")
        self.generic_visit(node)

    def visit_JoinedStr(self, node: ast.JoinedStr) -> None:
        for value in node.values:  # the text of an f-string is not a separate token
            if isinstance(value, ast.FormattedValue):
                self.visit(value.value)

    def visit_Constant(self, node: ast.Constant) -> None:
        if node.end_lineno != node.lineno:
            if isinstance(node.value, str):
                is_docstring = id(node) in self.docstrings
                self.multiline_strings.append((node.lineno, node.end_lineno, is_docstring))
            return
        token = self.at.get((node.lineno, node.col_offset))
        if token is None or self.in_annotation or not self.plain(node.lineno):
            return
        if token.end != (node.end_lineno, node.end_col_offset):
            return  # several string tokens side by side
        start, end = token.start[1], token.end[1]
        if isinstance(node.value, bool):
            self.replace(node.lineno, start, end, str(not node.value), "constant")
        elif isinstance(node.value, int) and token.string == str(node.value):
            self.replace(node.lineno, start, end, str(node.value + 1), "constant")
        elif (
            isinstance(node.value, str)
            and id(node) not in self.docstrings
            and PLAIN_STRING.fullmatch(token.string)
        ):
            changed = token.string[:-1] + "_" + token.string[-1]
            self.replace(node.lineno, start, end, changed, "string")

    def alone(self, node: ast.expr, elements: list[tuple[ast.expr, ast.expr]]) -> None:
        """A remove-the-line edit for each element of a literal that has a line to itself.

        elements holds (first node, last node) of each: a dict entry's key and value.
        """
        if self.in_annotation or node.end_lineno == node.lineno:
            return
        used = Counter(
            line
            for first, last in elements
            for line in range(first.lineno, (last.end_lineno or first.lineno) + 1)
        )
        for first, last in elements:
            line = first.lineno
            text = self.lines[line - 1]
            if (
                last.end_lineno == line
                and used[line] == 1
                and line not in (node.lineno, node.end_lineno)
                and self.plain(line)
                and not text[: first.col_offset].strip()
                and text[last.end_col_offset :].strip() == ","
            ):
                self.add(line, None, "delete")

    def visit_Dict(self, node: ast.Dict) -> None:
        pairs = zip(node.keys, node.values, strict=True)
        self.alone(node, [(key, value) for key, value in pairs if key is not None])
        self.generic_visit(node)

    def visit_sequence(self, node: ast.Set | ast.List | ast.Tuple) -> None:
        self.alone(node, [(element, element) for element in node.elts])
        self.generic_visit(node)

    visit_Set = visit_List = visit_Tuple = visit_sequence


# ------------------------------------------------------------------- patches


def blob(data: bytes) -> str:
    """The id git gives a file's content, shortened as in an index line."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:7]


def span(first: int, count: int) -> bytes:
    return b"%d" % first if count == 1 else b"%d,%d" % (first, count)


def file_diff(path: str, old: bytes, edits: list[Edit]) -> tuple[bytes, bytes]:
    """(the diff git would print for these edits to one file, the file after them)."""
    if not old.endswith(b"\n"):
        raise ValueError(f"{path} does not end with a newline")
    lines = old.split(b"\n")
    count = len(lines) - 1  # the file ends with a newline, so the last piece is empty

    # Runs of changed lines with no unchanged line between them, as git shows
    # them: [first old line, how many old lines go, the new lines].
    runs: list[list] = []
    for edit in sorted(edits, key=lambda edit: (edit.line, not edit.added)):
        gone = 0 if edit.added else 1
        new = [] if edit.new is None else [edit.new.encode("utf-8")]
        if runs and runs[-1][0] + runs[-1][1] == edit.line:
            runs[-1][1] += gone
            runs[-1][2] += new
        else:
            runs.append([edit.line, gone, new])
    # git joins two runs into one hunk when their context lines would touch.
    hunks: list[list[list]] = []
    for run in runs:
        if hunks and run[0] - (hunks[-1][-1][0] + hunks[-1][-1][1]) <= 2 * CONTEXT:
            hunks[-1].append(run)
        else:
            hunks.append([run])

    name = path.encode("utf-8")
    out = [b"diff --git a/%s b/%s" % (name, name), b"", b"--- a/%s" % name, b"+++ b/%s" % name]
    shift = 0
    for hunk in hunks:
        first = max(1, hunk[0][0] - CONTEXT)
        last = min(count, hunk[-1][0] + hunk[-1][1] - 1 + CONTEXT)
        body = []
        number = first
        for start, gone, new in hunk:
            body += [b" " + lines[n - 1] for n in range(number, start)]
            body += [b"-" + lines[n - 1] for n in range(start, start + gone)]
            body += [b"+" + text for text in new]
            number = start + gone
        body += [b" " + lines[n - 1] for n in range(number, last + 1)]
        # git names the block a hunk sits in: the last line above it that starts
        # with a letter, an underscore or a dollar sign, cut to 80 bytes.
        function = b""
        for number in range(first - 1, 0, -1):
            if re.match(rb"[A-Za-z_$]", lines[number - 1]):
                function = b" " + lines[number - 1].rstrip()[:80].rstrip()
                break
        old_count = last - first + 1
        new_count = old_count + sum(len(new) - gone for _, gone, new in hunk)
        out.append(
            b"@@ -%s +%s @@%s" % (span(first, old_count), span(first + shift, new_count), function)
        )
        out += body
        shift += new_count - old_count

    after = list(lines)
    for start, gone, new in reversed(runs):
        after[start - 1 : start - 1 + gone] = new
    result = b"\n".join(after)
    out[1] = b"index %s..%s 100644" % (blob(old).encode(), blob(result).encode())
    return b"\n".join(out) + b"\n", result


def patch_of(edits: list[Edit], sources: dict[str, bytes]) -> tuple[bytes, dict[str, bytes]]:
    """(the whole diff of a mutant, each edited file after it). git lists files by path."""
    parts, after = [], {}
    for path in sorted({edit.path for edit in edits}):
        mine = [edit for edit in edits if edit.path == path]
        diff, after[path] = file_diff(path, sources[path], mine)
        parts.append(diff)
    return b"".join(parts), after


# ------------------------------------------------------------------ choosing


def placed(chooser, sites: Sites, edit: Edit) -> None:
    """Fill in where the selector's map puts the edited line."""
    source = chooser.source(edit.path)
    owner = source.owner[edit.line] if source and edit.line <= source.lines else 0
    if not owner:
        edit.where = "unmapped"
        return
    _, _, flag, set_id = source.statements[owner]
    if flag == 2:
        edit.where = "order"
    elif flag == 1:
        edit.where = "class_body" if edit.line in sites.class_lines else "module"
    elif set_id < 0:
        edit.where = "never"
    else:
        edit.where, edit.tests = "executed", chooser.tests(set_id).bit_count()


def closer(sites: Sites, edit: Edit) -> str:
    """Where a line that runs at import sits, more closely than `placed` says."""
    if edit.where not in ("class_body", "module"):
        return edit.where
    in_class = edit.where == "class_body"
    if "lambda" in edit.scope.split(">"):
        return "class_lambda" if in_class else "module_lambda"
    size = sites.assigned.get(edit.line)
    if size is None:
        return edit.where + "_other"  # a def header, a decorator, a base class
    if not in_class:
        return "module_constant"
    return "class_attribute" if size == "one" else "class_entry"


def in_tests(edit: Edit) -> str:
    """Where an edit of a file under tests/ sits."""
    around = edit.scope.split(">") if edit.scope else []
    if any(name.startswith("def test") for name in around):
        return "test_method"
    if any(name.startswith("def ") or name == "lambda" for name in around):
        return "test_helper"
    return "test_class" if around else "test_module"


def changes_the_code(old: bytes, new: bytes, path: str) -> bool:
    """The edited file still compiles and its syntax tree is a different one."""
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")  # `x is 2` compiles, with a warning we expect
            compile(new, path, "exec", dont_inherit=True)
            return ast.dump(ast.parse(new)) != ast.dump(ast.parse(old))
    except (SyntaxError, ValueError):
        return False


def spread(rng: random.Random, pool: list[Edit], count: int, taken: set, accept) -> list[Edit]:
    """count edits from the pool, from as many different files as it allows."""
    by_file: dict[str, list[Edit]] = {}
    for edit in pool:
        by_file.setdefault(edit.path, []).append(edit)
    files = sorted(by_file)
    rng.shuffle(files)
    chosen: list[Edit] = []
    while len(chosen) < count and files:
        for file in list(files):
            candidates = [e for e in by_file[file] if (e.path, e.line) not in taken]
            rng.shuffle(candidates)
            pick = next((e for e in candidates if accept(e)), None)
            if pick is None:
                files.remove(file)
                continue
            taken.add((pick.path, pick.line))
            chosen.append(pick)
            if len(chosen) == count:
                break
    return chosen


def several(rng: random.Random, pool: list[Edit], how: str, taken: set, accept) -> list[Edit]:
    """Edits on 2 to 4 lines of one file: two neighbours, three close together, four far apart."""
    by_file: dict[str, dict[int, list[Edit]]] = {}
    for edit in pool:
        if (edit.path, edit.line) not in taken:
            by_file.setdefault(edit.path, {}).setdefault(edit.line, []).append(edit)
    files = sorted(by_file)
    rng.shuffle(files)
    for file in files:
        on = by_file[file]
        lines = sorted(on)
        rng.shuffle(lines)
        for anchor in lines:
            if how == "adjacent":
                group = [anchor, anchor + 1] if anchor + 1 in on else []
            elif how == "near":
                close = [line for line in sorted(on) if 0 < line - anchor <= 8]
                group = [anchor, *rng.sample(close, 2)] if len(close) >= 2 else []
            else:
                group = [anchor]
                for line in lines:
                    if len(group) < 4 and all(abs(line - other) >= 20 for other in group):
                        group.append(line)
                group = group if len(group) == 4 else []
            if group:
                edits = [rng.choice(on[line]) for line in sorted(group)]
                if accept(*edits):
                    taken.update((edit.path, edit.line) for edit in edits)
                    return edits
    return []


def damage(rng: random.Random, line: str) -> str:
    """A fixture line with a stray token put in, a word dropped or a mark removed."""
    words = line.split(" ")
    for _ in range(20):
        how = rng.choice(("insert", "insert", "drop", "mark"))
        if how == "insert" and len(words) > 1:
            at = rng.randrange(1, len(words))
            changed = " ".join([*words[:at], rng.choice(("42", "x", "0", "NULL")), *words[at:]])
        elif how == "drop" and len(words) > 2:
            at = rng.randrange(len(words))
            changed = " ".join(words[:at] + words[at + 1 :])
        else:
            marks = [i for i, char in enumerate(line) if char in ",()<>=."]
            if not marks:
                continue
            at = rng.choice(marks)
            changed = line[:at] + line[at + 1 :]
        if changed != line and changed.strip():
            return changed
    return line + " 42"


def fixture_edit(rng: random.Random, file: Path, sources: dict[str, bytes]) -> Edit:
    """One damaged line of a fixture file."""
    path = str(file.relative_to(PINNED))
    sources[path] = file.read_bytes()
    lines = sources[path].decode("utf-8").split("\n")
    usable = [n for n, text in enumerate(lines, 1) if text.strip() and text[0] not in "#-"]
    line = rng.choice(usable)
    return Edit(path, line, damage(rng, lines[line - 1]), "fixture", where="fixture")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--out", required=True, type=Path)
    options = parser.parse_args()

    sys.path.insert(0, str(REPO))
    import selector  # the map reader, to see where the selector places a line

    tests = json.loads((REPO / "sample" / "tests.json").read_text("utf-8"))["tests"]
    chooser = selector.Selector(str(REPO / "map"), tests)
    rng = random.Random(SEED)

    sources: dict[str, bytes] = {}
    sites: dict[str, Sites] = {}
    pool: list[Edit] = []
    for file in sorted((PINNED / "sqlglot").rglob("*.py")):
        path = str(file.relative_to(PINNED))
        if path == "sqlglot/__main__.py":
            continue  # no test imports it
        sources[path] = file.read_bytes()
        text = sources[path].decode("utf-8")
        sites[path] = Sites(path, text)
        sites[path].visit(ast.parse(sources[path]))
        for edit in sites[path].edits:
            placed(chooser, sites[path], edit)
            pool.append(edit)

    def accept(*edits: Edit) -> bool:
        """These edits to one file leave it compiling, with a different syntax tree."""
        path = edits[0].path
        return changes_the_code(sources[path], file_diff(path, sources[path], list(edits))[1], path)

    # ---------------------------------------------------------------- batch 1
    taken: set[tuple[str, int]] = set()
    chosen: list[Edit] = []
    for where, kinds, count in PLAN:
        matching = [e for e in pool if e.where == where and e.kind in kinds]
        picked = spread(rng, matching, count, taken, accept)
        if len(picked) < count:
            print(f"only {len(picked)} of {count} for {where} {kinds}", file=sys.stderr)
        chosen += picked

    # Comment lines that leave the syntax tree as it was.
    files = sorted(path for path in sources if len(sites[path].lines) > 30)
    rng.shuffle(files)
    for path in files:
        if sum(1 for e in chosen if e.kind == "comment") == COMMENTS:
            break
        lines = sites[path].lines
        line = rng.randrange(2, len(lines))
        below = next((text for text in lines[line - 1 :] if text.strip()), "")
        indent = below[: len(below) - len(below.lstrip())]
        edit = Edit(path, line, f"{indent}# {rng.choice(REMARKS)}", "comment", added=True)
        new = file_diff(path, sources[path], [edit])[1]
        try:
            same = ast.dump(ast.parse(new)) == ast.dump(ast.parse(sources[path]))
        except SyntaxError:
            same = False
        if same and (path, line) not in taken:
            edit.where = "comment"
            taken.add((path, line))
            chosen.append(edit)

    # Comment-looking lines inside a string that spans lines: not comments at all.
    strings = [
        (path, first, last, docstring)
        for path in sorted(sources)
        for first, last, docstring in sites[path].multiline_strings
    ]
    rng.shuffle(strings)
    strings.sort(key=lambda found: found[3])  # strings that are not docstrings first
    for path, first, last, docstring in strings:
        if sum(1 for e in chosen if e.kind == "in_string") == IN_STRINGS:
            break
        line = rng.randrange(first + 1, last + 1)
        edit = Edit(path, line, f"# {rng.choice(REMARKS)}", "in_string", added=True)
        if (path, line) not in taken and accept(edit):
            edit.where = "docstring" if docstring else "string"
            taken.add((path, line))
            chosen.append(edit)

    # One damaged line in a fixture.
    fixtures = sorted((PINNED / "tests" / "fixtures" / "optimizer").glob("*.sql"))
    fixtures = [file for file in fixtures if file.read_bytes().endswith(b"\n")]
    order = list(fixtures)
    rng.shuffle(order)
    for file in order[:FIXTURES]:
        chosen.append(fixture_edit(rng, file, sources))
        taken.add((chosen[-1].path, chosen[-1].line))

    mutants = [
        Mutant([e], e.kind, e.where, 1, f"{SEED}:{e.path}:{e.line}:{e.new}", e.tests)
        for e in chosen
    ]

    # ---------------------------------------------------------------- batch 2
    rng = random.Random(SEED_2)
    for edit in pool:
        edit.where = closer(sites[edit.path], edit)
    for file in sorted((PINNED / "tests").rglob("*.py")):
        path = str(file.relative_to(PINNED))
        data = file.read_bytes()
        # Only files the map knows some test to import, so the selector has an answer.
        if not data.endswith(b"\n") or (path not in chooser.opens and path not in chooser.own_file):
            continue
        sources[path] = data
        sites[path] = Sites(path, data.decode("utf-8"))
        sites[path].visit(ast.parse(data))
        for edit in sites[path].edits:
            edit.where = in_tests(edit)
            pool.append(edit)

    def note(edits: list[Edit], kind: str, where: str, tests: int | None = None) -> None:
        key = ";".join(f"{e.path}:{e.line}:{int(e.added)}:{e.new}" for e in edits)
        mutants.append(Mutant(edits, kind, where, 2, f"{SEED_2}:{key}", tests))

    for where, kinds, count in PLAN_2:
        matching = [e for e in pool if e.where == where and e.kind in kinds]
        picked = spread(rng, matching, count, taken, accept)
        if len(picked) < count:
            print(f"only {len(picked)} of {count} for {where} {kinds}", file=sys.stderr)
        for edit in picked:
            note([edit], edit.kind, edit.where)

    # Several lines of one sqlglot file, in code that runs: at import or in some test.
    running = ("executed", "class_attribute", "class_entry", "class_lambda", "module_constant")
    live = [e for e in pool if e.where in running and e.kind in CODE]
    for how in SEVERAL:
        edits = several(rng, live, how, taken, accept)
        if edits:
            note(edits, "multi", f"multi_{how}")
        else:
            print(f"no {how} lines found", file=sys.stderr)

    # One edit in each of two files.
    def one(where: str, other: str) -> Edit | None:
        if where == "fixture":
            free = [f for f in fixtures if str(f.relative_to(PINNED)) != other]
            edit = fixture_edit(rng, rng.choice(free), sources)
            taken.add((edit.path, edit.line))
            return edit
        wanted = ("class_attribute", "class_entry") if where == "class_body" else (where,)
        matching = [e for e in pool if e.where in wanted and e.kind in CODE and e.path != other]
        picked = spread(rng, matching, 1, taken, accept)
        return picked[0] if picked else None

    for first_where, second_where in PAIRS:
        first = one(first_where, "")
        second = one(second_where, first.path) if first else None
        if first and second:
            note([first, second], "two_files", f"{first_where}+{second_where}")
        else:
            print(f"no pair for {first_where}+{second_where}", file=sys.stderr)

    # ------------------------------------------------------------------ write
    commits = options.out / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    for stale in commits.glob("*.patch"):
        stale.unlink()
    entries = []
    used: set[str] = set()
    for mutant in mutants:
        patch, after = patch_of(mutant.edits, sources)
        digest = int(hashlib.sha1(mutant.key.encode()).hexdigest(), 16)
        name = ""
        while not name or name in used:
            digest, name = (
                digest // 7,
                "".join(
                    "0123456789abcdefghijklmnopqrstuvwxyz"[digest // 36**i % 36] for i in range(4)
                ),
            )
        used.add(name)
        (commits / f"{name}.patch").write_bytes(patch)
        files = []
        for path in sorted(after):
            old_lines = sources[path].decode("utf-8").split("\n")
            mine = sorted((e for e in mutant.edits if e.path == path), key=lambda e: e.line)
            files.append(
                {
                    "path": path,
                    "sha256": hashlib.sha256(after[path]).hexdigest(),
                    "edits": [
                        {
                            "line": e.line,
                            "old": [] if e.added else [old_lines[e.line - 1]],
                            "new": [] if e.new is None else [e.new],
                        }
                        for e in mine
                    ],
                }
            )
        entries.append(
            {
                "id": name,
                "batch": mutant.batch,
                "kind": mutant.kind,
                "where": mutant.where,
                "tests_that_ran_the_statement": mutant.tests,
                "files": files,
            }
        )
    entries.sort(key=lambda entry: entry["id"])
    manifest = {
        "seeds": [SEED, SEED_2],
        "sqlglot_commit": chooser.map.index["sqlglot_commit"],
        "sources_sha256": chooser.map.index["sources_sha256"],
        "mutants": entries,
    }
    (options.out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", "utf-8")
    for batch in (1, 2):
        counts: dict[str, int] = {}
        for entry in entries:
            if entry["batch"] == batch:
                key = f"{entry['where']}/{entry['kind']}"
                counts[key] = counts.get(key, 0) + 1
        print(f"batch {batch}: {sum(counts.values())} mutants: {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
