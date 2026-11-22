#!/usr/bin/env python3
"""Makes the mutant validation set: one-line edits of the pinned sqlglot.

Runs inside the labeling image (eval/mutants/Dockerfile), where the pinned
checkout is at /sqlglot, with the repository mounted read-only at /repo:

    python /repo/eval/mutants/generate.py --out <directory>

It writes one git-style unified diff per mutant to <out>/commits/<id>.patch,
in the format of the kit's sample commits, and <out>/manifest.json, which says
what each mutant is. The same seed, pinned commit and map give the same files.

The mutants have the shape of the sample commits (one line changed or one line
added) but are drawn at random from the whole code base. The sample commits are
never read. The point is to check the selector on commits whose failing tests
we can find out for ourselves, beyond the 30 in the kit. The selector never
reads this directory and nothing in it is copied into the image.

What is edited (kind):
  compare    one comparison operator swapped: == with != or is, is with ==,
             < with <=, > with >=
  boolean    one `and` swapped with `or`
  negate     an `if` condition negated
  constant   True swapped with False, or an integer raised by one
  string     one character added to a string literal
  comment    a comment line added that leaves the syntax tree alone
  in_string  a comment-looking line added inside a string that spans lines
  fixture    one line of tests/fixtures/optimizer/*.sql damaged

Where the selector's map places the edited line (where). The last three are
the rules most likely to hide a miss, so each gets its own share:
  executed    in a statement inside a function that some test ran
  never       in a statement that no test ran and that does not run at import:
              the selector selects nothing for it
  class_body  in a statement directly in a class body: runs at import
  order       in a statement only reached after other tests have run
A comment, in_string or fixture mutant records its own kind of place instead:
comment, string (docstring when the string is one) or fixture.
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
from dataclasses import dataclass
from pathlib import Path

SEED = 20261121
PINNED = Path("/sqlglot")
REPO = Path("/repo")

# (where, kinds, how many). 60 in all.
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
    path: str  # repo-relative
    line: int  # 1-based line of the pinned file; an added line goes in front of it
    new: str  # the line's new text, or the added line
    kind: str
    added: bool = False
    where: str = ""
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

    def plain(self, line: int) -> bool:
        """ASCII only: there a column is the same to ast (bytes) and tokenize (characters)."""
        return self.lines[line - 1].isascii()

    def replace(self, line: int, start: int, end: int, text: str, kind: str) -> None:
        old = self.lines[line - 1]
        self.edits.append(Edit(self.path, line, old[:start] + text + old[end:], kind))

    def between(self, left: ast.expr, right: ast.expr) -> list[tokenize.TokenInfo]:
        """The operator tokens between two operands, brackets aside."""
        start, end = (left.end_lineno, left.end_col_offset), (right.lineno, right.col_offset)
        found = [t for t in self.tokens if start <= t.start and t.end <= end]
        return [t for t in found if t.string not in ("(", ")")]

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self.class_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
        self.note_docstring(node)
        self.generic_visit(node)

    def visit_Module(self, node: ast.Module) -> None:
        self.note_docstring(node)
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
        for statement in node.body:
            self.visit(statement)

    visit_AsyncFunctionDef = visit_FunctionDef

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
                    self.edits.append(
                        Edit(
                            self.path,
                            test.lineno,
                            self.lines[test.lineno - 1][:start] + rest,
                            "negate",
                        )
                    )
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


# ------------------------------------------------------------------- patches


def blob(data: bytes) -> str:
    """The id git gives a file's content, shortened as in an index line."""
    return hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()[:7]


def span(first: int, count: int) -> bytes:
    return b"%d" % first if count == 1 else b"%d,%d" % (first, count)


def make_patch(path: str, old: bytes, edit: Edit) -> tuple[bytes, bytes]:
    """(the diff git would print for this edit, the file after it)."""
    if not old.endswith(b"\n"):
        raise ValueError(f"{path} does not end with a newline")
    lines = old.split(b"\n")
    count = len(lines) - 1  # the file ends with a newline, so the last piece is empty
    new_line = edit.new.encode("utf-8")
    first = max(1, edit.line - 3)
    last = min(count, edit.line + (2 if edit.added else 3))
    body = []
    for number in range(first, last + 1):
        if number != edit.line:
            body.append(b" " + lines[number - 1])
        elif edit.added:
            body += [b"+" + new_line, b" " + lines[number - 1]]
        else:
            body += [b"-" + lines[number - 1], b"+" + new_line]
    after = list(lines)
    if edit.added:
        after.insert(edit.line - 1, new_line)
    else:
        after[edit.line - 1] = new_line
    new = b"\n".join(after)

    # git names the block a hunk sits in: the last line above it that starts
    # with a letter, an underscore or a dollar sign, cut to 80 bytes.
    function = b""
    for number in range(first - 1, 0, -1):
        if re.match(rb"[A-Za-z_$]", lines[number - 1]):
            function = b" " + lines[number - 1].rstrip()[:80].rstrip()
            break
    name = path.encode("utf-8")
    old_count = last - first + 1
    head = [
        b"diff --git a/%s b/%s" % (name, name),
        b"index %s..%s 100644" % (blob(old).encode(), blob(new).encode()),
        b"--- a/%s" % name,
        b"+++ b/%s" % name,
        b"@@ -%s +%s @@%s"
        % (span(first, old_count), span(first, old_count + edit.added), function),
    ]
    return b"\n".join([*head, *body]) + b"\n", new


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

    def accept(edit: Edit) -> bool:
        return changes_the_code(
            sources[edit.path], make_patch(edit.path, sources[edit.path], edit)[1], edit.path
        )

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
        new = make_patch(path, sources[path], edit)[1]
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
    rng.shuffle(fixtures)
    for file in fixtures[:FIXTURES]:
        path = str(file.relative_to(PINNED))
        sources[path] = file.read_bytes()
        lines = sources[path].decode("utf-8").split("\n")
        usable = [n for n, text in enumerate(lines, 1) if text.strip() and text[0] not in "#-"]
        line = rng.choice(usable)
        chosen.append(Edit(path, line, damage(rng, lines[line - 1]), "fixture", where="fixture"))

    commits = options.out / "commits"
    commits.mkdir(parents=True, exist_ok=True)
    for stale in commits.glob("*.patch"):
        stale.unlink()
    mutants = []
    used: set[str] = set()
    for edit in chosen:
        patch, new = make_patch(edit.path, sources[edit.path], edit)
        digest = int(
            hashlib.sha1(f"{SEED}:{edit.path}:{edit.line}:{edit.new}".encode()).hexdigest(), 16
        )
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
        old_lines = sources[edit.path].decode("utf-8").split("\n")
        mutants.append(
            {
                "id": name,
                "kind": edit.kind,
                "where": edit.where,
                "tests_that_ran_the_statement": edit.tests,
                "path": edit.path,
                "line": edit.line,
                "added": edit.added,
                "old": None if edit.added else old_lines[edit.line - 1],
                "new": edit.new,
                "sha256": hashlib.sha256(new).hexdigest(),
            }
        )
    mutants.sort(key=lambda entry: entry["id"])
    manifest = {
        "seed": SEED,
        "sqlglot_commit": chooser.map.index["sqlglot_commit"],
        "sources_sha256": chooser.map.index["sources_sha256"],
        "mutants": mutants,
    }
    (options.out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", "utf-8")
    counts: dict[str, int] = {}
    for entry in mutants:
        key = f"{entry['where']}/{entry['kind']}"
        counts[key] = counts.get(key, 0) + 1
    print(f"{len(mutants)} mutants: {dict(sorted(counts.items()))}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
