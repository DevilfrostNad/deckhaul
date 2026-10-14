"""«Отчёт о проблеме»: one zip with everything needed to debug DeckHaul remotely.

Included: versions, detected paths (home replaced by ~), scan state, the mod
list with problems, a structural description of archives DeckHaul could not
read (headers and member flags, never file contents), launcher/server logs and
the error lines of game.log.txt.

Never included: profile names, profile.sii, saves, settings with the Steam
key, mod source links.
"""

from __future__ import annotations

import io
import json
import os
import platform
import struct
import sys
import time
import zipfile
from typing import List

from . import __version__

LOG_TAIL = 200_000          # bytes kept from the end of each log
PROBLEM_CODES = {"unreadable", "broken_archive", "manifest_broken", "unknown_layout"}


def _scrub(text: str) -> str:
    home = os.path.expanduser("~")
    return text.replace(home, "~") if home and home != "/" else text


def _tail(path: str) -> str:
    try:
        with open(path, "rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - LOG_TAIL))
            return _scrub(fh.read().decode("utf-8", "replace"))
    except OSError:
        return ""


def describe_archive(path: str, limit: int = 60) -> dict:
    """What the container looks like inside, without any of its contents."""
    out: dict = {"file": os.path.basename(path)}
    try:
        if os.path.isdir(path):
            n = sum(len(fs) for _d, _s, fs in os.walk(path))
            return {**out, "kind": "folder", "files": n}
        out["size"] = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(64)
        out["head_hex"] = head.hex()
        if head[:4] == b"SCS#":
            version, salt = struct.unpack_from("<HH", head, 4)
            out.update(kind="hashfs", version=version, salt=salt, hash=head[8:12].decode("latin-1"),
                       entries=struct.unpack_from("<I", head, 12)[0])
        elif head[:2] == b"PK":
            out["kind"] = "zip"
            try:
                with zipfile.ZipFile(path) as z:
                    infos = z.infolist()
                    out["members"] = len(infos)
                    out["sample"] = [{
                        "name": i.filename[:120], "flags": i.flag_bits, "method": i.compress_type,
                        "size": i.file_size, "csize": i.compress_size,
                    } for i in infos[:limit]]
                    out["encrypted_flag"] = sum(1 for i in infos if i.flag_bits & 1)
                    out["methods"] = sorted({i.compress_type for i in infos})
            except (zipfile.BadZipFile, OSError, ValueError) as exc:
                out["zip_error"] = str(exc)
        else:
            out["kind"] = "unknown"
    except OSError as exc:
        out["error"] = str(exc)
    return out


def build(app) -> bytes:
    """Return the report as zip bytes."""
    snap = app.snapshot()
    lay = snap.get("layout") or {}
    mods = snap.get("mods", [])
    problem_mods = [m for m in mods if any(p[0] in PROBLEM_CODES for p in m.get("scan_problems") or [])]

    info = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "deckhaul": __version__,
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "steamos": _read_os_release(),
        "game": lay.get("game"),
        "game_version": snap.get("game_version_full") or snap.get("game_version"),
        "layout": json.loads(_scrub(json.dumps(lay, ensure_ascii=False))),
        "scan": snap.get("scan"),
        "summary": snap.get("summary"),
        "profiles": [{"storage": p["storage"], "writable": p["writable"], "cloud": p["cloud"],
                      "mods": p["active_count"]} for p in snap.get("profiles", [])],
        "active_mods": snap.get("active", []),
        "dlc": app.dlc_report(),
        "issues": [{"code": i["code"], "severity": i["severity"], "title": i["title"], "mod": i.get("mod")}
                   for i in snap.get("issues", [])],
        "mods": [{
            "key": m.get("key"), "name": m.get("name"), "version": m.get("version"),
            "source": m.get("source"), "kind": m.get("kind"), "size": m.get("size"),
            "category": m.get("category"), "has_manifest": m.get("has_manifest"),
            "files": m.get("file_count"), "paths_known": m.get("paths_known"),
            "compatible": m.get("compatible"), "problems": m.get("scan_problems"),
            "missing": m.get("missing", False),
        } for m in mods],
    }
    archives = []
    for m in problem_mods:
        mod = app.by_key.get(m["key"])
        if mod is not None:
            archives.append(describe_archive(mod.path))

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("report.json", _scrub(json.dumps(info, ensure_ascii=False, indent=1)))
        z.writestr("unreadable_archives.json", _scrub(json.dumps(archives, ensure_ascii=False, indent=1)))
        for name in ("launcher.log", "server.log"):
            text = _tail(os.path.join(app.state_dir, name))
            if text:
                z.writestr(name, text)
        if app.log is not None:
            lines = [f"{l.level}: {l.text}" for l in app.log.lines]
            z.writestr("game_log_errors.txt", _scrub("\n".join(lines)))
        z.writestr("README.txt", (
            "Отчёт DeckHaul о проблеме.\n"
            "Внутри нет профилей, сохранений, названий профилей и ключа Steam.\n"
            "Пути показаны относительно домашней папки (~).\n"
        ))
    return buf.getvalue()


def _read_os_release() -> str:
    try:
        with open("/etc/os-release", encoding="utf-8") as fh:
            vals = dict(line.rstrip().split("=", 1) for line in fh if "=" in line)
        return " ".join(v.strip('"') for v in (vals.get("PRETTY_NAME", ""), vals.get("VERSION_ID", "")) if v)
    except OSError:
        return ""


def save(app, folder: str) -> str:
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, time.strftime("deckhaul-report-%Y%m%d-%H%M%S.zip"))
    with open(path, "wb") as fh:
        fh.write(build(app))
    return path
