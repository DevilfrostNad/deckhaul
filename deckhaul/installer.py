"""Install mods the user downloaded with a browser.

A download is turned into one or more ready-to-copy payloads:
  * a plain .scs or a .zip that is itself a mod  -> copied as is (.zip renamed to .scs)
  * an archive with .scs files inside             -> every .scs becomes its own mod
  * an archive with a mod folder inside           -> the folder is packed into a new .scs
  * .rar / .7z                                    -> unpacked with bsdtar / 7z first

Nothing touches the game folder until install() is called.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
import time
import zipfile
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .mods import KNOWN_ROOT_DIRS, Mod, inspect

ARCHIVE_EXTS = (".scs", ".zip", ".rar", ".7z")
PARTIAL_EXTS = (".part", ".crdownload", ".download", ".tmp", ".opdownload")
SETTLE_SECONDS = 3          # a file younger than this may still be written
SKIP_DIRS = {"__MACOSX", ".git"}


class InstallError(Exception):
    pass


def downloads_dir(override: Optional[str] = None) -> str:
    if override:
        return os.path.expanduser(override)
    home = os.path.expanduser("~")
    cfg = os.path.join(os.environ.get("XDG_CONFIG_HOME") or os.path.join(home, ".config"), "user-dirs.dirs")
    try:
        with open(cfg, encoding="utf-8") as fh:
            m = re.search(r'^XDG_DOWNLOAD_DIR="([^"]+)"', fh.read(), re.M)
        if m:
            return m.group(1).replace("$HOME", home)
    except OSError:
        pass
    for name in ("Downloads", "Загрузки"):
        p = os.path.join(home, name)
        if os.path.isdir(p):
            return p
    return os.path.join(home, "Downloads")


def extract_tool() -> Optional[List[str]]:
    """Command prefix that unpacks rar/7z into a folder given as the last argument."""
    for name in ("bsdtar",):
        if shutil.which(name):
            return [name, "-x", "-f"]
    for name in ("7zz", "7z", "7za"):
        if shutil.which(name):
            return [name, "x", "-y", "-bd"]
    return None


# --------------------------------------------------------------- data model

@dataclass
class Payload:
    id: int
    src: str                       # file ready to copy into mod/
    target: str                    # file name inside mod/
    notes: List[str] = field(default_factory=list)
    mod: Optional[dict] = None
    problems: List[str] = field(default_factory=list)
    replaces: List[dict] = field(default_factory=list)   # older copies already installed
    overwrite: bool = False        # a file with this name is already in mod/
    already: bool = False          # exactly this mod is already installed
    workshop_twin: Optional[str] = None
    installable: bool = True

    def public(self) -> dict:
        d = self.__dict__.copy()
        d.pop("src")
        return d


@dataclass
class Candidate:
    id: str                        # fingerprint of path + size + mtime
    path: str
    name: str
    size: int
    mtime: float
    status: str = "ready"          # ready | error | needs_tool | incomplete
    error: str = ""
    payloads: List[Payload] = field(default_factory=list)

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "size": self.size, "mtime": self.mtime,
            "status": self.status, "error": self.error,
            "payloads": [p.public() for p in self.payloads],
        }


def fingerprint(path: str, size: int, mtime: float) -> str:
    return hashlib.sha1(f"{path}|{size}|{int(mtime)}".encode()).hexdigest()[:16]


# ----------------------------------------------------------------- scanning

def list_downloads(folder: str) -> List[dict]:
    """Files in the downloads folder that may be mods. Not recursive."""
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    now = time.time()
    partial_stems = {os.path.splitext(n)[0] for n in names if n.lower().endswith(PARTIAL_EXTS)}
    for n in names:
        low = n.lower()
        if not low.endswith(ARCHIVE_EXTS) or n.startswith("."):
            continue
        p = os.path.join(folder, n)
        try:
            st = os.stat(p)
        except OSError:
            continue
        if not os.path.isfile(p):
            continue
        busy = n in partial_stems or now - st.st_mtime < SETTLE_SECONDS
        out.append({"path": p, "name": n, "size": st.st_size, "mtime": st.st_mtime, "busy": busy})
    out.sort(key=lambda d: -d["mtime"])
    return out


def _peek_zip(path: str) -> Optional[List[str]]:
    try:
        with zipfile.ZipFile(path) as z:
            return [n for n in z.namelist() if not n.endswith("/")]
    except (zipfile.BadZipFile, OSError):
        return None


def _looks_like_mod_root(names: List[str]) -> bool:
    tops = {n.split("/", 1)[0].lower() for n in names}
    return "manifest.sii" in tops or any(t in KNOWN_ROOT_DIRS or t.startswith("dlc_") for t in tops)


def _safe_name(stem: str) -> str:
    stem = re.sub(r"[^\w.\-+]+", "_", stem, flags=re.U).strip("._")
    return stem or "mod"


def _unique(target: str, used: set) -> str:
    stem, ext = os.path.splitext(target)
    n, out = 2, target
    while out.lower() in used:
        out = f"{stem}_{n}{ext}"
        n += 1
    used.add(out.lower())
    return out


def _extract_zip(path: str, dest: str) -> None:
    root = os.path.realpath(dest)
    with zipfile.ZipFile(path) as z:
        for info in z.infolist():
            name = info.filename.replace("\\", "/")
            if name.endswith("/") or any(part in SKIP_DIRS for part in name.split("/")):
                continue
            full = os.path.realpath(os.path.join(dest, name))
            if not full.startswith(root + os.sep):
                raise InstallError(f"в архиве опасный путь: {name}")
            os.makedirs(os.path.dirname(full), exist_ok=True)
            with z.open(info) as src, open(full, "wb") as dst:
                shutil.copyfileobj(src, dst, 1 << 20)


def _extract_external(path: str, dest: str) -> None:
    tool = extract_tool()
    if tool is None:
        raise InstallError("needs_tool")
    if tool[0].startswith("7z"):
        cmd = tool + [f"-o{dest}", path]
    else:
        cmd = tool + [path, "-C", dest]
    res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=1800)
    if res.returncode != 0:
        msg = res.stderr.decode("utf-8", "replace").strip().splitlines()
        raise InstallError("архив не распаковался: " + (msg[-1] if msg else f"код {res.returncode}"))
    # Never trust links or paths that escaped the folder.
    root = os.path.realpath(dest)
    for dp, ds, fs in os.walk(dest):
        for n in ds + fs:
            p = os.path.join(dp, n)
            if os.path.islink(p):
                os.unlink(p)
            elif not os.path.realpath(p).startswith(root + os.sep):
                raise InstallError("в архиве опасный путь")


def _pack_folder(folder: str, out_path: str) -> None:
    tmp = out_path + ".part"
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
        for dp, ds, fs in os.walk(folder):
            ds[:] = [d for d in ds if d not in SKIP_DIRS]
            for n in fs:
                full = os.path.join(dp, n)
                rel = os.path.relpath(full, folder).replace(os.sep, "/")
                z.write(full, rel)
    os.replace(tmp, out_path)


def _payloads_from_tree(root: str, archive_stem: str, staging: str) -> List[Payload]:
    """Find mods inside an unpacked archive."""
    payloads: List[Payload] = []
    used: set = set()

    # 1. Ready .scs / mod .zip files anywhere inside.
    for dp, ds, fs in os.walk(root):
        ds[:] = sorted(d for d in ds if d not in SKIP_DIRS)
        for n in sorted(fs):
            low = n.lower()
            full = os.path.join(dp, n)
            if low.endswith(".scs"):
                payloads.append(Payload(len(payloads), full, _unique(n, used), ["взят из архива"]))
            elif low.endswith(".zip"):
                names = _peek_zip(full)
                if names and _looks_like_mod_root(names):
                    t = _unique(os.path.splitext(n)[0] + ".scs", used)
                    payloads.append(Payload(len(payloads), full, t, ["взят из архива", "переименован в .scs"]))
    if payloads:
        return payloads

    # 2. Unpacked mod folders: the topmost folders that look like a mod root.
    roots = []
    for dp, ds, fs in os.walk(root):
        ds[:] = sorted(d for d in ds if d not in SKIP_DIRS)
        names = [n.lower() for n in fs] + [d.lower() for d in ds]
        if "manifest.sii" in names or any(n in KNOWN_ROOT_DIRS for n in ds):
            roots.append(dp)
            ds[:] = []          # do not descend into a found mod
    for r in roots:
        stem = archive_stem if os.path.samefile(r, root) else os.path.basename(r)
        target = _unique(_safe_name(stem) + ".scs", used)
        out = os.path.join(staging, "packed", target)
        os.makedirs(os.path.dirname(out), exist_ok=True)
        _pack_folder(r, out)
        notes = ["упакован из папки в .scs"]
        if not os.path.samefile(r, root):
            notes.append(f"убрана лишняя папка «{os.path.relpath(r, root)}»")
        payloads.append(Payload(len(payloads), out, target, notes))
    return payloads


def analyze(path: str, staging_root: str) -> Candidate:
    """Work out what is inside a downloaded file. May unpack into staging_root."""
    st = os.stat(path)
    name = os.path.basename(path)
    cand = Candidate(fingerprint(path, st.st_size, st.st_mtime), path, name, st.st_size, st.st_mtime)
    stem, ext = os.path.splitext(name)
    ext = ext.lower()
    staging = os.path.join(staging_root, cand.id)

    try:
        if ext == ".scs":
            cand.payloads = [Payload(0, path, name)]
        elif ext == ".zip":
            names = _peek_zip(path)
            if names is None:
                raise InstallError("ZIP-архив повреждён или скачан не до конца")
            if _looks_like_mod_root(names) and not any(n.lower().endswith(".scs") for n in names):
                cand.payloads = [Payload(0, path, _safe_name(stem) + ".scs", ["переименован в .scs"])]
            else:
                tree = os.path.join(staging, "tree")
                if not os.path.isdir(tree):
                    shutil.rmtree(staging, ignore_errors=True)
                    os.makedirs(tree)
                    _extract_zip(path, tree)
                cand.payloads = _payloads_from_tree(tree, stem, staging)
        else:
            tree = os.path.join(staging, "tree")
            if not os.path.isdir(tree):
                shutil.rmtree(staging, ignore_errors=True)
                os.makedirs(tree)
                try:
                    _extract_external(path, tree)
                except InstallError:
                    shutil.rmtree(staging, ignore_errors=True)
                    raise
            cand.payloads = _payloads_from_tree(tree, stem, staging)
    except InstallError as exc:
        if str(exc) == "needs_tool":
            cand.status = "needs_tool"
            cand.error = (f"Чтобы открыть {ext}, нужен распаковщик bsdtar или 7-Zip. "
                          "Распакуйте архив в «Загрузки» вручную, DeckHaul увидит файлы .scs.")
        else:
            cand.status, cand.error = "error", str(exc)
        return cand
    except (OSError, zipfile.BadZipFile, subprocess.TimeoutExpired) as exc:
        cand.status, cand.error = "error", f"не удалось разобрать файл: {exc}"
        return cand

    if not cand.payloads:
        cand.status = "error"
        cand.error = ("В архиве нет мода: ни файлов .scs, ни папки с manifest.sii или def/. "
                      "Возможно, это инструкция или мод для другой игры.")
        return cand

    for p in cand.payloads:
        m = inspect(Mod(key=os.path.splitext(p.target)[0], source="local", path=p.src))
        p.mod = m.public()
        bad = {"broken_archive", "nested_archive", "nested_folder"}
        p.problems = [text for code, text in m.scan_problems]
        p.installable = not any(code in bad for code, _ in m.scan_problems)
    return cand


def compare_with_installed(cand: Candidate, mod_dir: str, installed: List[Mod]) -> None:
    """Mark payloads that update or duplicate mods already on disk."""
    existing = {n.lower() for n in os.listdir(mod_dir)} if os.path.isdir(mod_dir) else set()
    for p in cand.payloads:
        p.overwrite = p.target.lower() in existing
        p.replaces, p.workshop_twin, p.already = [], None, False
        if p.overwrite:
            try:
                p.already = os.path.getsize(os.path.join(mod_dir, p.target)) == os.path.getsize(p.src)
            except OSError:
                pass
        if not p.mod:
            continue
        name = (p.mod.get("name") or "").strip().lower()
        author = (p.mod.get("author") or "").strip().lower()
        stem = os.path.splitext(p.target)[0].lower()
        for m in installed:
            same_name = m.has_manifest and m.name.strip().lower() == name and (m.author or "").strip().lower() == author
            if not (same_name or m.key.lower() == stem):
                continue
            if m.source == "workshop":
                p.workshop_twin = m.name
                continue
            if m.version and m.version == p.mod.get("version") and same_name:
                p.already = True
                continue
            p.replaces.append({"key": m.key, "name": m.name, "version": m.version,
                               "file": os.path.basename(m.path)})


# ------------------------------------------------------------------ install

def install_payload(p: Payload, mod_dir: str) -> str:
    os.makedirs(mod_dir, exist_ok=True)
    need = os.path.getsize(p.src)
    if shutil.disk_usage(mod_dir).free < need + (64 << 20):
        raise InstallError("Не хватает места на диске для мода")
    dst = os.path.join(mod_dir, p.target)
    tmp = dst + ".deckhaul-part"
    shutil.copyfile(p.src, tmp)
    os.replace(tmp, dst)
    return dst


def move_to_trash(path: str, trash_root: str) -> str:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    d = os.path.join(trash_root, stamp)
    os.makedirs(d, exist_ok=True)
    dst = os.path.join(d, os.path.basename(path))
    shutil.move(path, dst)
    return dst


def cleanup_staging(staging_root: str, keep_ids: List[str]) -> None:
    if not os.path.isdir(staging_root):
        return
    for n in os.listdir(staging_root):
        if n not in keep_ids:
            shutil.rmtree(os.path.join(staging_root, n), ignore_errors=True)


def install(cand: Candidate, payload_ids: List[int], mod_dir: str, trash,
            remove_old: bool, delete_download: bool) -> Dict[str, list]:
    """Copy the chosen payloads into mod/.

    trash(path) moves a file away and returns where it went. Returns installed
    keys, replaced keys and a journal of file operations for restore points."""
    chosen = [p for p in cand.payloads if p.id in payload_ids]
    if not chosen:
        raise InstallError("Не выбрано ни одного мода")
    for p in chosen:
        if not p.installable:
            raise InstallError(f"«{p.target}» установить нельзя: " + "; ".join(p.problems))
    installed, replaced, journal = [], [], []

    def away(path):
        journal.append({"op": "moved", "src": path, "dst": trash(path)})

    for p in chosen:
        if remove_old:
            for old in p.replaces:
                old_path = os.path.join(mod_dir, old["file"])
                if os.path.exists(old_path) and old["file"].lower() != p.target.lower():
                    away(old_path)
                replaced.append({"old": old["key"], "new": os.path.splitext(p.target)[0]})
        if p.overwrite:
            cur = os.path.join(mod_dir, p.target)
            if os.path.exists(cur):
                away(cur)
        journal.append({"op": "added", "path": install_payload(p, mod_dir)})
        installed.append(os.path.splitext(p.target)[0])
    if delete_download and os.path.exists(cand.path):
        away(cand.path)
    return {"installed": installed, "replaced": replaced, "journal": journal,
            "trashed": [j for j in journal if j["op"] == "moved"]}
