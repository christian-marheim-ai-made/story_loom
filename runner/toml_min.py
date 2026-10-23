
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

try:
    import tomllib as _tomllib
except ModuleNotFoundError:
    _tomllib = None

_ESCAPES = {"n": "\n", "t": "\t", "r": "\r", '"': '"', "\\": "\\"}


def _unescape(s: str) -> str:
    out: list[str] = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt in _ESCAPES:
                out.append(_ESCAPES[nxt])
                i += 2
                continue
            if nxt == "u" and i + 6 <= len(s):
                out.append(chr(int(s[i + 2 : i + 6], 16)))
                i += 6
                continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_array(inner: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    quote: str | None = None
    buf: list[str] = []
    i = 0
    while i < len(inner):
        c = inner[i]
        if quote:
            buf.append(c)
            if c == "\\" and quote == '"' and i + 1 < len(inner):
                buf.append(inner[i + 1])
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
            buf.append(c)
        elif c == "[":
            depth += 1
            buf.append(c)
        elif c == "]":
            depth -= 1
            buf.append(c)
        elif c == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(c)
        i += 1
    tail = "".join(buf).strip()
    if tail:
        parts.append(tail)
    return [p.strip() for p in parts if p.strip()]


def _parse_scalar(raw: str) -> Any:
    raw = raw.strip()
    if raw.startswith(('"""', "'''")):
        raise ValueError("multi-line strings are not supported by the fallback TOML parser")
    if len(raw) >= 2 and raw[0] == raw[-1] == '"':
        return _unescape(raw[1:-1])
    if len(raw) >= 2 and raw[0] == raw[-1] == "'":
        return raw[1:-1]
    if raw == "true":
        return True
    if raw == "false":
        return False
    if raw.startswith("["):
        if not raw.endswith("]"):
            raise ValueError("multi-line arrays are not supported by the fallback TOML parser")
        inner = raw[1:-1].strip()
        return [_parse_scalar(p) for p in _split_array(inner)] if inner else []
    if re.fullmatch(r"[+-]?\d[\d_]*", raw):
        return int(raw.replace("_", ""))
    if re.fullmatch(r"[+-]?(\d[\d_]*)?\.\d[\d_]*([eE][+-]?\d+)?", raw) or re.fullmatch(
        r"[+-]?\d[\d_]*[eE][+-]?\d+", raw
    ):
        return float(raw.replace("_", ""))
    raise ValueError(f"cannot parse TOML value: {raw!r}")


def _strip_comment(line: str) -> str:
    quote: str | None = None
    i = 0
    while i < len(line):
        c = line[i]
        if quote:
            if c == "\\" and quote == '"':
                i += 2
                continue
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#":
            return line[:i]
        i += 1
    return line


def parse_subset(text: str) -> dict[str, Any]:
    data: dict[str, Any] = {}
    table = data
    for lineno, raw_line in enumerate(text.splitlines(), 1):
        line = _strip_comment(raw_line).strip()
        if not line:
            continue
        if line.startswith("["):
            if line.startswith("[["):
                raise ValueError(
                    f"line {lineno}: arrays of tables ([[x]]) are not supported; "
                    f"use a named table such as [models.my-model]"
                )
            if not line.endswith("]"):
                raise ValueError(f"line {lineno}: bad table header {raw_line!r}")
            table = data
            for part in line[1:-1].split("."):
                table = table.setdefault(part.strip().strip('"').strip("'"), {})
            continue
        if "=" not in line:
            raise ValueError(f"line {lineno}: expected key = value, got {raw_line!r}")
        key, _, value = line.partition("=")
        try:
            table[key.strip().strip('"').strip("'")] = _parse_scalar(value)
        except ValueError as exc:
            raise ValueError(f"line {lineno}: {exc}") from None
    return data


def load(path: Path, force_fallback: bool = False) -> dict[str, Any]:
    text = Path(path).read_text(encoding="utf-8")
    if _tomllib is not None and not force_fallback:
        return _tomllib.loads(text)
    return parse_subset(text)
