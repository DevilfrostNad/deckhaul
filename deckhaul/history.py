"""Remember what was installed last time and record what changed: new mods,
removed mods, version bumps and game updates."""

from __future__ import annotations

import json
import os
import time
from typing import List, Optional

MAX_EVENTS = 300


class History:
    def __init__(self, path: str):
        self.path = path
        self.data = {"game_version": None, "mods": {}, "events": []}
        try:
            with open(path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

    @property
    def events(self) -> List[dict]:
        return self.data["events"]

    def _add(self, kind: str, text: str, key: Optional[str] = None, **extra) -> dict:
        ev = {"t": time.time(), "kind": kind, "text": text, "mod": key}
        ev.update(extra)
        self.data["events"].insert(0, ev)
        del self.data["events"][MAX_EVENTS:]
        return ev

    def update(self, mods, game_version: Optional[str]) -> List[dict]:
        """Compare the fresh scan with the stored snapshot. Returns the new events."""
        fresh: List[dict] = []
        first_run = not self.data["mods"] and self.data["game_version"] is None
        old_gv = self.data.get("game_version")
        if game_version and old_gv and game_version != old_gv:
            fresh.append(self._add(
                "game", f"Игра обновилась: {old_gv} → {game_version}. Проверьте совместимость модов.",
                old=old_gv, new=game_version,
            ))
        if game_version:
            self.data["game_version"] = game_version

        prev = self.data["mods"]
        now = {}
        for m in mods:
            snap = {"name": m.name, "version": m.version, "size": m.size, "mtime": m.mtime,
                    "source": m.source}
            now[m.key] = snap
            old = prev.get(m.key)
            if first_run:
                continue
            if old is None:
                fresh.append(self._add("added", f"Новый мод: «{m.name}» {m.version}".rstrip(), m.key))
            elif old.get("version") != m.version and (old.get("version") or m.version):
                fresh.append(self._add(
                    "updated", f"«{m.name}» обновлён: {old.get('version') or '?'} → {m.version or '?'}",
                    m.key, old=old.get("version"), new=m.version,
                ))
            elif old.get("size") != m.size or abs((old.get("mtime") or 0) - m.mtime) > 1:
                fresh.append(self._add(
                    "changed", f"Файлы «{m.name}» изменились, номер версии тот же ({m.version or '?'})",
                    m.key,
                ))
        if not first_run:
            for key, old in prev.items():
                if key not in now:
                    fresh.append(self._add("removed", f"Мод удалён: «{old.get('name') or key}»", key))
        self.data["mods"] = now
        self.save()
        return fresh

    def note(self, kind: str, text: str, **extra) -> None:
        self._add(kind, text, **extra)
        self.save()

    def save(self) -> None:
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)
