"""Restore points: everything needed to undo a change DeckHaul made.

A point is taken right before a change. It keeps a copy of the profile as it
was and a journal of what happened to files afterwards:

  {"op": "added", "path": ...}            a file DeckHaul put into mod/
  {"op": "moved", "src": ..., "dst": ...}  a file DeckHaul moved (into its trash)

Rolling back to a point undoes the journals of that point and of every later
one, newest first, then puts the profile copy back. A rollback is itself a
change with its own point, so it can be undone too.

Files in the trash are deleted only when no remaining point refers to them.
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from typing import Dict, List, Optional

KEEP_AUTO = 30


class RestoreError(Exception):
    pass


class Point:
    def __init__(self, data: dict, folder: str):
        self.data = data
        self.folder = folder

    @property
    def id(self) -> str:
        return self.data["id"]

    @property
    def manual(self) -> bool:
        return self.data.get("kind") == "manual"

    @property
    def profile_copy(self) -> str:
        return os.path.join(self.folder, "profile.sii")

    def public(self) -> dict:
        d = {k: v for k, v in self.data.items() if k not in ("changes", "log_errors")}
        d["files_changed"] = len(self.data.get("changes", []))
        return d

    def save(self) -> None:
        tmp = os.path.join(self.folder, "point.json.tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, os.path.join(self.folder, "point.json"))


class RestoreStore:
    def __init__(self, state_dir: str):
        self.root = os.path.join(state_dir, "restore")
        self.trash = os.path.join(state_dir, "trash")
        os.makedirs(self.root, exist_ok=True)

    # ------------------------------------------------------------- listing
    def points(self) -> List[Point]:
        out = []
        for name in os.listdir(self.root):
            meta = os.path.join(self.root, name, "point.json")
            try:
                with open(meta, encoding="utf-8") as fh:
                    out.append(Point(json.load(fh), os.path.join(self.root, name)))
            except (OSError, ValueError):
                continue
        out.sort(key=lambda p: (p.data.get("t", 0), p.id), reverse=True)
        return out

    def get(self, pid: str) -> Point:
        for p in self.points():
            if p.id == pid:
                return p
        raise RestoreError("Точка восстановления не найдена")

    # ------------------------------------------------------------ creating
    def create(self, *, reason: str, summary: str, profile_id: Optional[str], profile_name: str,
               profile_path: Optional[str], mod_dir: str, kind: str = "auto", label: str = "",
               log_errors: Optional[List[str]] = None, log_mtime: float = 0.0) -> Point:
        t = time.time()
        pid = time.strftime("%Y%m%d-%H%M%S", time.localtime(t)) + "-" + uuid.uuid4().hex[:6]
        folder = os.path.join(self.root, pid)
        os.makedirs(folder)
        has_profile = bool(profile_path and os.path.isfile(profile_path))
        if has_profile:
            shutil.copy2(profile_path, os.path.join(folder, "profile.sii"))
        p = Point({
            "id": pid, "t": t, "kind": kind, "label": label, "reason": reason, "summary": summary,
            "profile_id": profile_id, "profile_name": profile_name, "has_profile": has_profile,
            "mod_dir": mod_dir, "changes": [], "rolled_back": False,
            "log_errors": sorted(set(log_errors or [])), "log_mtime": log_mtime, "log_ack": False,
        }, folder)
        p.save()
        self.prune()
        return p

    def move_to_trash(self, path: str) -> str:
        d = os.path.join(self.trash, time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
        os.makedirs(d, exist_ok=True)
        dst = os.path.join(d, os.path.basename(path))
        shutil.move(path, dst)
        return dst

    @staticmethod
    def record(point: Optional[Point], changes: List[dict], summary: Optional[str] = None) -> None:
        if point is None:
            return
        point.data["changes"].extend(changes)
        if summary:
            point.data["summary"] = summary
        point.save()

    # ----------------------------------------------------------- rollback
    # Every point is either "in effect" (its changes are on disk) or rolled
    # back. Returning to a point undoes, newest first, every in-effect point
    # from the newest one down to that point. Undoing a rollback brings the
    # points it had undone back into effect, so they are undone in turn when
    # the walk reaches them. That keeps the timeline consistent however often
    # the user jumps back and forth.

    def plan(self, pid: str) -> List[Point]:
        """In-effect points a return to pid would undo, newest first (without cascades)."""
        pts = self.points()
        ids = [p.id for p in pts]
        if pid not in ids:
            raise RestoreError("Точка восстановления не найдена")
        return [p for p in pts[: ids.index(pid) + 1]
                if not p.manual and not p.data.get("rolled_back")]

    def _undo(self, p: Point, undo_point: Point) -> Dict[str, int]:
        removed = returned = 0
        for ch in reversed(p.data.get("changes", [])):
            if ch["op"] == "added":
                if os.path.exists(ch["path"]):
                    dst = self.move_to_trash(ch["path"])
                    self.record(undo_point, [{"op": "moved", "src": ch["path"], "dst": dst}])
                    removed += 1
            elif ch["op"] == "moved":
                if os.path.exists(ch["dst"]) and not os.path.exists(ch["src"]):
                    os.makedirs(os.path.dirname(ch["src"]), exist_ok=True)
                    shutil.move(ch["dst"], ch["src"])
                    self.record(undo_point, [{"op": "moved", "src": ch["dst"], "dst": ch["src"]}])
                    returned += 1
        return {"removed": removed, "returned": returned}

    def rollback(self, pid: str, undo_point: Point) -> Dict[str, int]:
        """Undo file changes back to pid. Every step is journaled into undo_point,
        so the return itself can be undone. The caller restores the profile copy."""
        target = self.get(pid)
        order = [p.id for p in self.points()]
        seq = order[: order.index(pid) + 1]
        total = {"removed": 0, "returned": 0}
        undone: List[str] = []
        for point_id in seq:
            if point_id == undo_point.id:
                continue
            p = self.get(point_id)                 # fresh: flags change as we go
            if p.manual or p.data.get("rolled_back"):
                continue
            for k, v in self._undo(p, undo_point).items():
                total[k] += v
            p.data["rolled_back"] = True
            p.save()
            undone.append(p.id)
            # Undoing a return re-applies what that return had undone.
            for other in p.data.get("undid", []):
                try:
                    q = self.get(other)
                except RestoreError:
                    continue
                q.data["rolled_back"] = False
                q.save()
        undo_point.data["undid"] = undone
        undo_point.data["returned_to"] = pid
        undo_point.save()
        total["profile"] = 1 if target.data.get("has_profile") else 0
        return total

    # -------------------------------------------------------------- pruning
    def referenced(self) -> set:
        refs = set()
        for p in self.points():
            for ch in p.data.get("changes", []):
                for k in ("src", "dst"):
                    if ch.get(k):
                        refs.add(os.path.realpath(ch[k]))
        return refs

    def prune(self, keep: int = KEEP_AUTO) -> int:
        """Drop the oldest automatic points and the trash files only they referred to."""
        autos = [p for p in self.points() if not p.manual]
        dropped = autos[keep:]
        if not dropped:
            return 0
        candidates = set()
        for p in dropped:
            for ch in p.data.get("changes", []):
                for k in ("src", "dst"):
                    if ch.get(k):
                        candidates.add(os.path.realpath(ch[k]))
            shutil.rmtree(p.folder, ignore_errors=True)
        still = self.referenced()
        trash = os.path.realpath(self.trash) + os.sep
        for f in candidates - still:
            if f.startswith(trash) and os.path.exists(f):
                os.remove(f) if os.path.isfile(f) else shutil.rmtree(f, ignore_errors=True)
                parent = os.path.dirname(f)
                if parent != os.path.realpath(self.trash) and not os.listdir(parent):
                    os.rmdir(parent)
        return len(dropped)

    def delete(self, pid: str) -> None:
        p = self.get(pid)
        if not p.manual:
            raise RestoreError("Удалять можно только сохранённые вами точки")
        shutil.rmtree(p.folder, ignore_errors=True)
