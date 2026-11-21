"""Maps every line of a Python source file to the statement that owns it.

coverage.py records line numbers, and a patch can touch any line of a
statement that spans several, so the selector needs to go from a line to its
statement. For each file this gives:

  owner[n]   the first line of the innermost statement that holds line n, or 0
             when line n holds no code (blank, or only a comment). Lines such
             as `else:` and `except X:` are not statements of their own and
             belong to the compound statement they are part of.
  spans      first line -> last line, for every statement
  import_time  first lines of statements that run when the module is imported
             rather than when a test calls something: everything at module
             level or directly in a class body, and so every `def` and `class`
             header and decorator there. Statements inside a function body
             run when the function is called.

A decorated definition starts at its first decorator. Two statements on one
line share that line; the map keeps the innermost.
"""

from __future__ import annotations

import ast
import io
import tokenize

NOT_CODE = {
    tokenize.COMMENT,
    tokenize.NL,
    tokenize.NEWLINE,
    tokenize.INDENT,
    tokenize.DEDENT,
    tokenize.ENCODING,
    tokenize.ENDMARKER,
}
FUNCTIONS = (ast.FunctionDef, ast.AsyncFunctionDef)


def code_lines(source: str) -> set[int]:
    """Lines that hold at least one token of code. A multi-line string counts on every line."""
    lines: set[int] = set()
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in NOT_CODE:
            lines.update(range(token.start[0], token.end[0] + 1))
    return lines


def first_line(statement: ast.stmt) -> int:
    decorators = getattr(statement, "decorator_list", [])
    return min([statement.lineno, *(decorator.lineno for decorator in decorators)])


def child_statements(statement: ast.AST) -> list[ast.stmt]:
    """The statements directly inside a compound statement, in source order."""
    children: list[ast.stmt] = []
    for field in ("body", "orelse", "finalbody"):
        children.extend(getattr(statement, field, []))
    for handler in getattr(statement, "handlers", []):
        children.extend(handler.body)
    for case in getattr(statement, "cases", []):
        children.extend(case.body)
    return sorted(children, key=first_line)


def statement_table(source: str) -> dict:
    """The owner of every line, every statement's span, and which statements run at import."""
    tree = ast.parse(source)
    line_count = len(source.splitlines())
    owner = [0] * (line_count + 2)
    spans: dict[int, int] = {}
    import_time: set[int] = set()

    def visit(statement: ast.stmt, inside_function: bool) -> None:
        start, end = first_line(statement), statement.end_lineno or statement.lineno
        # A later statement that starts on the same line is nested or follows a
        # semicolon. Keep the widest span and let the innermost own the line.
        spans[start] = max(end, spans.get(start, 0))
        if not inside_function:
            import_time.add(start)
        for line in range(start, end + 1):
            owner[line] = start
        nested_in_function = inside_function or isinstance(statement, FUNCTIONS)
        for child in child_statements(statement):
            visit(child, nested_in_function)

    for statement in tree.body:
        visit(statement, False)

    code = code_lines(source)
    for line in range(len(owner)):
        if line not in code:
            owner[line] = 0
    return {
        "lines": line_count,
        "owner": owner[: line_count + 1],
        "spans": spans,
        "import_time": import_time,
    }
