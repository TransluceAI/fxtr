"""Path expression parser for indexer definitions.

Parses expressions like::

    "!.name: str"
    "!(as '!my.type').turns[*: int].metrics[*: str].score: float"
    "!(as? '!t1' | '!t2').items[$i: int].value: int"

Grammar (see stage1_namespace_index_1bcd.md for full spec)::

    expression := initial_deref "." body
               |  initial_deref

    initial_deref := "!" typed_deref_suffix?
    typed_deref_suffix := "(as " id_list ")"     -- strict
                       |  "(as? " id_list ")"    -- lax

    body       := segment ("." segment)* (":" col_type)?
    segment    := name bracket? deref?
    name       := [a-zA-Z_][a-zA-Z0-9_]*
    bracket    := "[" bracket_inner "]"
    deref      := "!" typed_deref_suffix?

    bracket_inner := "*:" ws col_type            -- unnamed var
                  |  "$" varname ":" ws col_type  -- named var
                  |  integer

    id_list    := py_string (ws "|" ws py_string)*
    col_type   := "int" | "str" | "bool" | "bytes" | "float" | "FxtrID"
"""

from __future__ import annotations

import ast
import dataclasses
import io
import tokenize
from typing import Never

from fxtr2.data.entity_core import FxtrID

from .indexers import DerefMarker, PathElement, PathIndex, PathKey, TypedDerefMarker, VarMarker
from .indexing_types import IndexerColumnType

_COL_TYPE_MAP: dict[str, IndexerColumnType] = {
    "int": IndexerColumnType.INT,
    "str": IndexerColumnType.STR,
    "bool": IndexerColumnType.BOOL,
    "bytes": IndexerColumnType.BYTES,
    "float": IndexerColumnType.FLOAT,
    "FxtrID": IndexerColumnType.FXTR_ID,
}


@dataclasses.dataclass(frozen=True)
class ParsedPathExpression:
    """Result of parsing a path expression."""

    initial_lookup: DerefMarker | TypedDerefMarker
    path: tuple[PathElement, ...]
    var_types: tuple[IndexerColumnType, ...]
    var_names: tuple[str, ...] | None  # None when named_vars=False
    value_type: IndexerColumnType | None  # None when require_value_type=False


class _Parser:
    """Character-by-character parser for path expressions."""

    def __init__(self, expr: str, *, named_vars: bool) -> None:
        self.expr = expr
        self.pos = 0
        self.named_vars = named_vars
        self.path: list[PathElement] = []
        self.var_types: list[IndexerColumnType] = []
        self.var_names: list[str] = []

    # -- helpers --

    def _at_end(self) -> bool:
        return self.pos >= len(self.expr)

    def _peek(self) -> str:
        if self._at_end():
            return ""
        return self.expr[self.pos]

    def _advance(self) -> str:
        ch = self.expr[self.pos]
        self.pos += 1
        return ch

    def _expect(self, ch: str) -> None:
        if self._at_end() or self.expr[self.pos] != ch:
            self._error(f"expected '{ch}'")
        self.pos += 1

    def _skip_ws(self) -> None:
        while not self._at_end() and self.expr[self.pos] in " \t":
            self.pos += 1

    def _error(self, msg: str) -> Never:
        caret = " " * self.pos + "^"
        raise ValueError(f"Parse error at position {self.pos}: {msg}\n  {self.expr}\n  {caret}")

    def _starts_with(self, s: str) -> bool:
        return self.expr[self.pos : self.pos + len(s)] == s

    # -- col_type --

    def _parse_col_type(self) -> IndexerColumnType:
        self._skip_ws()
        for name, ct in _COL_TYPE_MAP.items():
            if self._starts_with(name):
                # Make sure it's not a prefix of something longer (e.g., "integer")
                end = self.pos + len(name)
                if end >= len(self.expr) or not self.expr[end].isalnum():
                    self.pos = end
                    return ct
        self._error(f"expected column type (one of {', '.join(_COL_TYPE_MAP)})")
        raise AssertionError("unreachable")

    # -- id_list for typed deref --

    def _parse_py_string(self) -> str:
        """Parse a Python string literal at the current position using tokenize."""
        self._skip_ws()
        remaining = self.expr[self.pos :]
        if not remaining or remaining[0] not in ("'", '"'):
            self._error("expected a quoted string literal")

        # Use tokenize to parse the string token
        try:
            tokens = list(tokenize.generate_tokens(io.StringIO(remaining).readline))
        except tokenize.TokenError as e:
            self._error(f"invalid string literal: {e}")

        # Find the first STRING token
        for tok in tokens:
            if tok.type == tokenize.STRING:
                value = ast.literal_eval(tok.string)
                # Advance past the token
                self.pos += tok.end[1]
                return value
        self._error("expected a string literal")

    def _parse_id_list(self) -> frozenset[FxtrID]:
        """Parse a pipe-separated list of quoted FxtrID strings."""
        ids: list[FxtrID] = []
        while True:
            s = self._parse_py_string()
            try:
                ids.append(FxtrID.from_id_string(s))
            except Exception as e:
                self._error(f"invalid FxtrID string {s!r}: {e}")
            self._skip_ws()
            if not self._at_end() and self._peek() == "|":
                self._advance()  # consume '|'
                self._skip_ws()
            else:
                break
        if not ids:
            self._error("expected at least one type ID")
        return frozenset(ids)

    # -- deref --

    def _parse_typed_deref_suffix(self) -> DerefMarker | TypedDerefMarker:
        """Parse the optional suffix after '!': nothing, '(as ...)', or '(as? ...)'."""
        if self._at_end() or self._peek() != "(":
            return DerefMarker()

        self._advance()  # consume '('
        # Expect 'as' or 'as?'
        if not self._starts_with("as"):
            self._error("expected 'as' or 'as?' after '!('")
        self.pos += 2  # consume 'as'

        if not self._at_end() and self._peek() == "?":
            self._advance()  # consume '?'
            strict = False
        else:
            strict = True

        self._skip_ws()
        supported_types = self._parse_id_list()
        self._skip_ws()
        self._expect(")")

        return TypedDerefMarker(supported_types=supported_types, strict=strict)

    # -- bracket --

    def _parse_bracket(self) -> None:
        """Parse bracket contents: [*: type], [$var: type], or [integer]."""
        self._advance()  # consume '['

        if self._peek() == "*":
            # Unnamed variable: *: col_type
            if self.named_vars:
                self._error("expected '$varname' in query mode, got '*'")
            self._advance()  # consume '*'
            self._expect(":")
            ct = self._parse_col_type()
            self.path.append(VarMarker())
            self.var_types.append(ct)
        elif self._peek() == "$":
            # Named variable: $varname: col_type
            if not self.named_vars:
                self._error("expected '*' in parse mode, got '$'")
            self._advance()  # consume '$'
            # Read variable name
            start = self.pos
            while not self._at_end() and (
                self.expr[self.pos].isalnum() or self.expr[self.pos] == "_"
            ):
                self.pos += 1
            if self.pos == start:
                self._error("expected variable name after '$'")
            var_name = self.expr[start : self.pos]
            if var_name in self.var_names:
                self._error(f"duplicate variable name '${var_name}'")
            self._expect(":")
            ct = self._parse_col_type()
            self.path.append(VarMarker())
            self.var_types.append(ct)
            self.var_names.append(var_name)
        elif self._peek().isdigit():
            # Fixed integer index
            start = self.pos
            while not self._at_end() and self.expr[self.pos].isdigit():
                self.pos += 1
            index = int(self.expr[start : self.pos])
            self.path.append(PathIndex(index=index))
        else:
            self._error("expected '*', '$', or integer inside brackets")

        self._expect("]")

    # -- segment --

    def _parse_name(self) -> str:
        """Parse an identifier [a-zA-Z_][a-zA-Z0-9_]*."""
        start = self.pos
        if self._at_end() or not (self.expr[self.pos].isalpha() or self.expr[self.pos] == "_"):
            self._error("expected identifier")
        self.pos += 1
        while not self._at_end() and (self.expr[self.pos].isalnum() or self.expr[self.pos] == "_"):
            self.pos += 1
        return self.expr[start : self.pos]

    def _parse_segment(self) -> None:
        """Parse one segment: name, optional bracket, optional deref."""
        name = self._parse_name()
        self.path.append(PathKey(key=name))

        # Optional bracket
        if not self._at_end() and self._peek() == "[":
            self._parse_bracket()

        # Optional deref
        if not self._at_end() and self._peek() == "!":
            self._advance()  # consume '!'
            deref = self._parse_typed_deref_suffix()
            self.path.append(deref)

    # -- top-level --

    def parse(self, *, require_value_type: bool) -> ParsedPathExpression:
        """Parse the full expression."""
        # Step 0: parse initial deref
        if self._at_end() or self._peek() != "!":
            self._error("expression must start with '!'")
        self._advance()  # consume '!'

        initial_lookup = self._parse_typed_deref_suffix()

        # After initial deref, expect '.' + body, or end of string
        value_type: IndexerColumnType | None = None

        if not self._at_end():
            if self._peek() == ".":
                self._advance()  # consume '.'
            else:
                self._error("expected '.' or end of expression after initial deref")

            # Parse body segments
            self._parse_segment()
            while not self._at_end() and self._peek() == ".":
                self._advance()  # consume '.'
                self._parse_segment()

            # Optional value type
            if not self._at_end() and self._peek() == ":":
                self._advance()  # consume ':'
                value_type = self._parse_col_type()

        if require_value_type and value_type is None:
            self._error("expected ': <type>' for value type")

        if not self._at_end():
            self._error("unexpected trailing characters")

        return ParsedPathExpression(
            initial_lookup=initial_lookup,
            path=tuple(self.path),
            var_types=tuple(self.var_types),
            var_names=tuple(self.var_names) if self.named_vars else None,
            value_type=value_type,
        )


def parse_path_expression(
    expr: str,
    *,
    named_vars: bool,
    require_value_type: bool,
) -> ParsedPathExpression:
    """Parse a path expression string into a structured result.

    Args:
        expr: The expression to parse.
        named_vars: If True, variables must be named (``$var``). If False, unnamed (``*``).
        require_value_type: If True, the expression must end with ``: <type>``.

    Returns:
        A ``ParsedPathExpression`` with the parsed components.

    Raises:
        ValueError: If the expression is malformed.
    """
    return _Parser(expr, named_vars=named_vars).parse(require_value_type=require_value_type)
