"""Extracting import statements from compiled code and source.

Static analysis is done on code objects rather than the AST because the
compiler has already resolved which names are actually imports, including
imports nested in functions, comprehensions and class bodies.  AST parsing is
used only for the handful of dynamic imports whose module name is a literal
string (``importlib.import_module("x")``).
"""

from __future__ import annotations

import ast
import dis
import types
from dataclasses import dataclass


@dataclass(frozen=True)
class ImportStmt:
    """A single import discovered in a code object."""

    name: str
    level: int = 0
    fromlist: tuple[str, ...] | None = None

    def __str__(self) -> str:  # pragma: no cover - debugging aid
        dots = "." * self.level
        if self.fromlist:
            return f"from {dots}{self.name} import {', '.join(self.fromlist)}"
        return f"import {dots}{self.name}"


def _fromlist(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if isinstance(value, str):
        return (value,)
    if isinstance(value, (tuple, list)):
        return tuple(str(item) for item in value if isinstance(item, str))
    return None


def imports_from_code(code: types.CodeType) -> list[ImportStmt]:
    """Return every ``import``/``from ... import`` in *code* recursively."""
    instructions = list(dis.get_instructions(code))
    found: list[ImportStmt] = []
    seen: set[tuple[str, int, tuple[str, ...] | None]] = set()

    for index, ins in enumerate(instructions):
        if ins.opname == "IMPORT_NAME":
            # CPython 3.14 compiles the relative-import level to LOAD_SMALL_INT
            # instead of LOAD_CONST; both carry the integer in ``argval``.
            constants = [
                previous.argval
                for previous in instructions[max(0, index - 4) : index]
                if previous.opname in ("LOAD_CONST", "LOAD_SMALL_INT")
            ]
            level = 0
            fromlist: tuple[str, ...] | None = None
            if len(constants) >= 2:
                if isinstance(constants[-2], int):
                    level = int(constants[-2])
                fromlist = _fromlist(constants[-1])
            elif len(constants) == 1:
                fromlist = _fromlist(constants[0])
            name = str(ins.argval or "")
            if not name and not level:
                # ``from . import x`` can be compiled without a name operand on
                # some versions; the level constant tells us it is relative.
                level = 1
            key = (name, level, fromlist)
            if key not in seen:
                seen.add(key)
                found.append(ImportStmt(name=name, level=level, fromlist=fromlist))

    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            for statement in imports_from_code(constant):
                key = (statement.name, statement.level, statement.fromlist)
                if key not in seen:
                    seen.add(key)
                    found.append(statement)
    return found


_DYNAMIC_CALLS = {"import_module", "__import__", "load_module"}


def _called_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return None


def dynamic_imports(source: str, *, filename: str = "<unknown>") -> list[str]:
    """Best effort discovery of literal ``import_module("...")`` calls.

    Only string literals are considered; anything computed at runtime still
    needs a ``hidden_imports`` entry or a hook.
    """
    try:
        tree = ast.parse(source, filename=filename)
    except SyntaxError:
        return []
    names: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if _called_name(node.func) not in _DYNAMIC_CALLS:
            continue
        if not node.args:
            continue
        argument = node.args[0]
        if isinstance(argument, ast.Constant) and isinstance(argument.value, str):
            names.append(argument.value)
    return names
