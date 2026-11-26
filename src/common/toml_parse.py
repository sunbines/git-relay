"""TOML loading for git-relay configuration files.

Python 3.11+ ships ``tomllib`` and ``tomli`` provides the same API on older
versions. The small parser below covers the syntax used by git-relay configs
when neither module is available (for example Python 3.9 on openEuler
without python3-tomli in the repositories).
"""

from __future__ import annotations

import re

try:
    import tomllib
except ModuleNotFoundError:  # Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[no-redef]
    except ModuleNotFoundError:
        tomllib = None

_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_INT_RE = re.compile(r"[+-]?\d(?:_?\d)*")
_FLOAT_RE = re.compile(
    r"[+-]?\d(?:_?\d)*(?:\.\d(?:_?\d)*(?:[eE][+-]?\d(?:_?\d)*)?"
    r"|[eE][+-]?\d(?:_?\d)*)")
_HEX_DIGITS = "0123456789abcdefABCDEF"
_ESCAPES = {
    "b": "\b", "t": "\t", "n": "\n", "f": "\f", "r": "\r",
    '"': '"', "\\": "\\",
}


def toml_backend() -> str:
    """Return the name of the TOML backend that will actually be used."""
    if tomllib is None:
        return "built-in fallback"
    return "tomllib" if tomllib.__name__ == "tomllib" else "tomli"


def parse_toml(text: str) -> dict:
    """Parse TOML text, preferring the standard library or tomli backend."""
    if tomllib is not None:
        # A real backend handles the full spec, so the subset parser is unused.
        return tomllib.loads(text)
    return _parse_toml_subset(text)


def _find_unquoted(text: str, needle: str) -> int:
    """Return the first index of ``needle`` outside quotes, or -1 if absent."""
    quote = ""
    escaped = False
    for index, char in enumerate(text):
        if quote == '"':
            # Inside a basic string only a backslash escape can hide a quote.
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quote = ""
        elif quote == "'":
            # Literal strings have no escapes, so the next quote always closes.
            if char == "'":
                quote = ""
        elif char in "\"'":
            quote = char
        elif char == needle:
            return index
    return -1


def _strip_comment(line: str) -> str:
    """Drop a trailing TOML comment while leaving '#' inside strings intact."""
    index = _find_unquoted(line, "#")
    return line[:index] if index >= 0 else line


def _skip_whitespace(text: str, index: int) -> int:
    """Advance past spaces and tabs and return the new index."""
    while index < len(text) and text[index] in " \t":
        index += 1
    return index


def _parse_basic_string(text: str, index: int, lineno: int) -> tuple[str, int]:
    """Decode a double-quoted string starting after the opening quote."""
    out: list[str] = []
    while index < len(text):
        char = text[index]
        if char == '"':
            return "".join(out), index + 1
        if char != "\\":
            out.append(char)
            index += 1
            continue
        index += 1
        if index >= len(text):
            break
        escape = text[index]
        if escape in _ESCAPES:
            out.append(_ESCAPES[escape])
            index += 1
        elif escape in ("u", "U"):
            # Fixed-width hex payload: 4 digits for \u, 8 for \U.
            width = 4 if escape == "u" else 8
            digits = text[index + 1:index + 1 + width]
            if (len(digits) != width
                    or any(char not in _HEX_DIGITS for char in digits)):
                raise ValueError(f"line {lineno}: invalid unicode escape")
            out.append(chr(int(digits, 16)))
            index += 1 + width
        else:
            raise ValueError(f"line {lineno}: unsupported escape \\{escape}")
    raise ValueError(f"line {lineno}: unterminated string")


def _parse_literal_string(
        text: str, index: int, lineno: int) -> tuple[str, int]:
    """Read a single-quoted literal string, which performs no escape handling."""
    end = text.find("'", index)
    if end < 0:
        raise ValueError(f"line {lineno}: unterminated string")
    return text[index:end], end + 1


def _parse_array(text: str, index: int, lineno: int) -> tuple[list, int]:
    """Parse a TOML array starting at the opening bracket."""
    items: list = []
    index += 1
    while True:
        index = _skip_whitespace(text, index)
        if index >= len(text):
            raise ValueError(f"line {lineno}: unterminated array")
        if text[index] == "]":
            return items, index + 1
        value, index = _parse_value_at(text, index, lineno)
        items.append(value)
        index = _skip_whitespace(text, index)
        if index >= len(text):
            raise ValueError(f"line {lineno}: unterminated array")
        # A trailing comma before ']' is legal, so only ',' advances the loop.
        if text[index] == ",":
            index += 1
        elif text[index] != "]":
            raise ValueError(f"line {lineno}: expected ',' or ']' in array")


def _parse_value_at(text: str, index: int, lineno: int) -> tuple[object, int]:
    """Parse one value at ``index`` and return it with the next index."""
    index = _skip_whitespace(text, index)
    if index >= len(text):
        raise ValueError(f"line {lineno}: missing value")
    char = text[index]
    if char == '"':
        if text.startswith('"""', index):
            raise ValueError(
                f"line {lineno}: multi-line strings need tomli "
                f"or Python 3.11+")
        return _parse_basic_string(text, index + 1, lineno)
    if char == "'":
        if text.startswith("'''", index):
            raise ValueError(
                f"line {lineno}: multi-line strings need tomli "
                f"or Python 3.11+")
        return _parse_literal_string(text, index + 1, lineno)
    if char == "[":
        return _parse_array(text, index, lineno)
    if char == "{":
        raise ValueError(
            f"line {lineno}: inline tables need tomli or Python 3.11+")
    # Bare scalars end at whitespace or at the array/line delimiters.
    end = index
    while end < len(text) and text[end] not in " \t,]":
        end += 1
    token = text[index:end]
    if token == "true":
        return True, end
    if token == "false":
        return False, end
    if _INT_RE.fullmatch(token):
        return int(token.replace("_", "")), end
    if _FLOAT_RE.fullmatch(token):
        return float(token.replace("_", "")), end
    raise ValueError(f"line {lineno}: unsupported value {token!r}")


def _parse_value(text: str, lineno: int) -> object:
    """Parse a complete value line and reject any trailing text."""
    value, index = _parse_value_at(text, 0, lineno)
    index = _skip_whitespace(text, index)
    if index != len(text):
        raise ValueError(f"line {lineno}: unexpected text after value")
    return value


def _parse_key(text: str, lineno: int) -> list[str]:
    """Split a possibly dotted key into its parts, honouring quoted segments."""
    parts: list[str] = []
    index = 0
    while True:
        index = _skip_whitespace(text, index)
        if index >= len(text):
            raise ValueError(f"line {lineno}: empty key")
        char = text[index]
        if char == '"':
            part, index = _parse_basic_string(text, index + 1, lineno)
        elif char == "'":
            part, index = _parse_literal_string(text, index + 1, lineno)
        else:
            match = _BARE_KEY_RE.match(text, index)
            if match is None:
                raise ValueError(f"line {lineno}: invalid key")
            part = match.group(0)
            index = match.end()
        parts.append(part)
        index = _skip_whitespace(text, index)
        if index >= len(text):
            return parts
        # Anything other than a dot after a part means the key is malformed.
        if text[index] != ".":
            raise ValueError(f"line {lineno}: invalid key")
        index += 1


def _parse_toml_subset(text: str) -> dict:
    """Parse the git-relay config subset of TOML without external modules."""
    data: dict = {}
    table = data
    for lineno, raw in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw).strip()
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[["):
                raise ValueError(
                    f"line {lineno}: arrays of tables need tomli or "
                    f"Python 3.11+")
            if not line.endswith("]"):
                raise ValueError(f"line {lineno}: malformed table header")
            path = _parse_key(line[1:-1], lineno)
            table = data
            # Walk or create the nested dicts for a dotted table header.
            for part in path:
                node = table.setdefault(part, {})
                if not isinstance(node, dict):
                    raise ValueError(
                        f"line {lineno}: cannot redefine {part!r} as a table")
                table = node
            continue
        # '=' may appear inside quoted keys, so split only on an unquoted one.
        split = _find_unquoted(line, "=")
        if split < 0:
            raise ValueError(f"line {lineno}: expected 'key = value'")
        path = _parse_key(line[:split], lineno)
        value = _parse_value(line[split + 1:], lineno)
        node = table
        for part in path[:-1]:
            child = node.setdefault(part, {})
            if not isinstance(child, dict):
                raise ValueError(
                    f"line {lineno}: cannot redefine {part!r} as a table")
            node = child
        node[path[-1]] = value
    return data
