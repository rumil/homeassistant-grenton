"""Parser for the Lua table literal a CLU sends in a clientReport.

A report carries one value per subscribed key, written as a Lua table
constructor, for example ``{27,nil,1,true,"2023-12-05",0.50}``. Values are
numbers, ``nil``, booleans and quoted strings with Lua escapes. A User
Feature can also hold a table, which arrives as a nested ``{...}``.

The parser keeps exactly one value per top-level entry, so a string that
contains a comma or a brace, or a nested table, never shifts the values of
the keys after it.
"""

from __future__ import annotations

from ....state import GrentonValue, cast_string_to_grenton_value

_WHITESPACE = " \t\r\n"
_SEPARATORS = ",;"
_SIMPLE_ESCAPES = {
    "a": "\a",
    "b": "\b",
    "f": "\f",
    "n": "\n",
    "r": "\r",
    "t": "\t",
    "v": "\v",
    "\\": "\\",
    '"': '"',
    "'": "'",
    "\n": "\n",
}


class LuaLiteralError(ValueError):
    """The text is not a Lua table literal this parser understands."""


def parse_lua_table(text: str) -> list[GrentonValue]:
    """Parse ``{v1,v2,...}`` into one value per top-level entry.

    Strings keep the typing the integration has always used, so ``"1"``
    becomes ``1`` (see ``_typed_string``). A nested table is returned as its
    source text (a string), so it occupies exactly one position.

    Raises:
        LuaLiteralError: when the text is not a complete table literal.
    """
    return _Parser(text).parse_top_level()


def _typed_string(content: str) -> GrentonValue:
    """Type the content of a quoted string the way the integration always has.

    The CLU stores a value written with ``setVar("x","1")`` as a string and
    reports it quoted; entities rely on reading it back as a number or a
    boolean. Any other text is returned unchanged, quotes and spaces included.
    """
    stripped = content.strip()
    lowered = stripped.lower()
    if lowered == "nil":
        return None
    if lowered in ("true", "false"):
        return lowered == "true"
    try:
        return int(stripped)
    except ValueError:
        pass
    try:
        return float(stripped)
    except ValueError:
        return content


class _Parser:
    def __init__(self, text: str) -> None:
        self.text = text
        self.pos = 0

    def error(self, message: str) -> LuaLiteralError:
        return LuaLiteralError(f"{message} at position {self.pos} in {self.text!r}")

    def skip_whitespace(self) -> None:
        while self.pos < len(self.text) and self.text[self.pos] in _WHITESPACE:
            self.pos += 1

    def peek(self) -> str:
        return self.text[self.pos] if self.pos < len(self.text) else ""

    def parse_top_level(self) -> list[GrentonValue]:
        self.skip_whitespace()
        values = self.parse_table()
        self.skip_whitespace()
        if self.pos != len(self.text):
            raise self.error("Unexpected text after the table")
        return values

    def parse_table(self) -> list[GrentonValue]:
        if self.peek() != "{":
            raise self.error("Expected '{'")
        self.pos += 1
        values: list[GrentonValue] = []
        while True:
            self.skip_whitespace()
            char = self.peek()
            if char == "":
                raise self.error("Unterminated table")
            if char == "}":
                self.pos += 1
                return values
            values.append(self.parse_value())
            self.skip_whitespace()
            char = self.peek()
            if char in _SEPARATORS and char != "":
                self.pos += 1
            elif char != "}":
                raise self.error("Expected ',' or '}'")

    def parse_value(self) -> GrentonValue:
        char = self.peek()
        if char in ('"', "'"):
            return _typed_string(self.parse_string())
        if char == "{":
            start = self.pos
            self.parse_table()
            return self.text[start:self.pos]
        return self.parse_bare()

    def parse_bare(self) -> GrentonValue:
        start = self.pos
        while self.pos < len(self.text) and self.text[self.pos] not in _SEPARATORS + "}{\"'" + _WHITESPACE:
            self.pos += 1
        token = self.text[start:self.pos]
        if not token:
            raise self.error("Expected a value")
        if "=" in token or "[" in token:
            raise self.error("Keyed table fields are not supported")
        return cast_string_to_grenton_value(token)

    def parse_string(self) -> str:
        quote = self.text[self.pos]
        self.pos += 1
        out = bytearray()
        while True:
            if self.pos >= len(self.text):
                raise self.error("Unterminated string")
            char = self.text[self.pos]
            if char == quote:
                self.pos += 1
                return out.decode("utf-8", errors="replace")
            if char != "\\":
                out += char.encode("utf-8")
                self.pos += 1
                continue
            # Escape sequence
            self.pos += 1
            if self.pos >= len(self.text):
                raise self.error("Unterminated escape")
            esc = self.text[self.pos]
            if esc.isdigit():
                digits = esc
                self.pos += 1
                while len(digits) < 3 and self.pos < len(self.text) and self.text[self.pos].isdigit():
                    digits += self.text[self.pos]
                    self.pos += 1
                code = int(digits)
                if code > 255:
                    raise self.error("Escape out of range")
                out.append(code)
                continue
            out += _SIMPLE_ESCAPES.get(esc, esc).encode("utf-8")
            self.pos += 1
