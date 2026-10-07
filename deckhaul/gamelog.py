"""Read game.log.txt: the running game version and mod-related errors from the last launch."""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

_VERSION = re.compile(r"init ver\.\s*([0-9]+\.[0-9]+(?:\.[0-9]+)*\w*)")
_LEVEL = re.compile(r"<(ERROR|WARNING)>\s*(.*)")
_MOD_REF = re.compile(r"(?:/mod/|\bmod/|mod_workshop_package\.[0-9A-Fa-f]+|workshop/content/\d+/(\d+))([^\s'\"]*)")


@dataclass
class LogLine:
    level: str
    text: str
    mod_hint: Optional[str] = None


@dataclass
class GameLog:
    path: str
    mtime: float = 0.0
    version: Optional[str] = None
    lines: List[LogLine] = field(default_factory=list)

    @property
    def short_version(self) -> Optional[str]:
        """'1.53.3.14s' -> '1.53.3' (what compatible_versions globs target)."""
        if not self.version:
            return None
        m = re.match(r"(\d+\.\d+(?:\.\d+)?)", self.version)
        return m.group(1) if m else self.version


def read_game_log(data_dir: str, limit: int = 200) -> Optional[GameLog]:
    path = os.path.join(data_dir, "game.log.txt")
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    log = GameLog(path=path, mtime=os.path.getmtime(path))
    m = _VERSION.search(text)
    if m:
        log.version = m.group(1)
    seen = set()
    for raw in text.splitlines():
        lm = _LEVEL.search(raw)
        if not lm:
            continue
        level, msg = lm.group(1), lm.group(2).strip()
        if msg in seen:
            continue
        seen.add(msg)
        hint = None
        mm = _MOD_REF.search(msg)
        if mm:
            hint = mm.group(0)
        log.lines.append(LogLine(level.lower(), msg, hint))
        if len(log.lines) >= limit:
            break
    return log
