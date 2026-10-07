"""Tiny reader for Valve KeyValues text (.vdf / .acf)."""

from __future__ import annotations

import re
from typing import Any, Dict

_TOK = re.compile(r'"((?:\\.|[^"\\])*)"|([{}])|//[^\n]*|\s+')


def loads(text: str) -> Dict[str, Any]:
    stack = [{}]
    key = None
    for m in _TOK.finditer(text):
        s, brace = m.group(1), m.group(2)
        if brace == "{":
            new: Dict[str, Any] = {}
            stack[-1][key if key is not None else ""] = new
            stack.append(new)
            key = None
        elif brace == "}":
            if len(stack) > 1:
                stack.pop()
            key = None
        elif s is not None:
            s = s.replace('\\"', '"').replace("\\\\", "\\")
            if key is None:
                key = s
            else:
                stack[-1][key] = s
                key = None
    return stack[0]


def load(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8", errors="replace") as fh:
        return loads(fh.read())
