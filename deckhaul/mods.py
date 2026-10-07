"""Scan local and Steam Workshop mods into one list of Mod records."""

from __future__ import annotations

import json
import os
import re
import zlib
from dataclasses import asdict, dataclass, field
from typing import Dict, List, Optional, Tuple

from . import vdf
from .archive import ArchiveError, ZipContainer, open_container
from .cityhash import hash_path
from .sii import parse_sii, first_unit, version_matches

MOD_EXTS = (".scs", ".zip")
JUNK_ARCHIVES = (".rar", ".7z", ".tar", ".gz", ".xz")

# Categories the in-game mod manager knows; anything else shows as "other".
OFFICIAL_CATEGORIES = (
    "truck", "trailer", "interior", "tuning_parts", "ai_traffic", "sound", "paint_job",
    "cargo_pack", "map", "ui", "weather_setup", "physics", "graphics", "models", "movers",
    "walkers", "prefabs", "other",
)

# Top-level folders a real mod usually has. Used to spot wrongly packed archives.
KNOWN_ROOT_DIRS = {
    "def", "vehicle", "model", "model2", "material", "map", "sound", "ui", "font", "automat",
    "prefab", "prefab2", "unit", "effect", "locale", "system", "custom", "video", "matlib",
    "road_template", "particle", "base", "sounds", "dlc",
}

ROOT_META = ("manifest.sii", "versions.sii", "description.txt", "mod_description.txt")

CACHE_VERSION = 3
CRC_CHECK_LIMIT = 64 << 20


@dataclass
class Mod:
    key: str                      # the id the game writes into active_mods
    source: str                   # "local" | "workshop"
    path: str                     # what the game actually loads
    kind: str = "unknown"         # zip | hashfs1 | hashfs2 | folder
    name: str = ""
    version: str = ""
    author: str = ""
    categories: List[str] = field(default_factory=list)
    compatible: List[str] = field(default_factory=list)
    icon: str = ""
    description: str = ""
    has_manifest: bool = False
    size: int = 0
    mtime: float = 0.0
    workshop_id: Optional[int] = None
    workshop_slot: Optional[str] = None
    scan_problems: List[Tuple[str, str]] = field(default_factory=list)
    hashes: List[int] = field(default_factory=list)   # file hashes outside the root
    paths_known: bool = True

    @property
    def category(self) -> str:
        for c in self.categories:
            if c in OFFICIAL_CATEGORIES:
                return c
        return "other"

    def compatible_with(self, game_version: Optional[str], full: Optional[str] = None) -> Optional[bool]:
        if not game_version or not self.compatible:
            return None
        return version_matches(game_version, self.compatible) or (
            bool(full) and version_matches(full, self.compatible)
        )

    def public(self) -> dict:
        d = asdict(self)
        d.pop("hashes", None)
        d["category"] = self.category
        d["file_count"] = len(self.hashes)
        return d


def workshop_key(workshop_id: int) -> str:
    return "mod_workshop_package.%016X" % workshop_id


def parse_workshop_key(key: str) -> Optional[int]:
    m = re.match(r"mod_workshop_package\.([0-9A-Fa-f]+)$", key)
    return int(m.group(1), 16) if m else None


# --------------------------------------------------------------------- cache

class ScanCache:
    def __init__(self, path: str):
        self.path = path
        self.data: Dict[str, dict] = {}
        self.dirty = False
        try:
            with open(path, encoding="utf-8") as fh:
                raw = json.load(fh)
            if raw.get("v") == CACHE_VERSION:
                self.data = raw.get("items", {})
        except (OSError, ValueError):
            pass

    def get(self, path: str, size: int, mtime: float) -> Optional[dict]:
        item = self.data.get(path)
        if item and item.get("size") == size and abs(item.get("mtime", 0) - mtime) < 1e-3:
            return item["mod"]
        return None

    def put(self, path: str, size: int, mtime: float, mod: Mod) -> None:
        self.data[path] = {"size": size, "mtime": mtime, "mod": asdict(mod)}
        self.dirty = True

    def save(self) -> None:
        if not self.dirty:
            return
        tmp = f"{self.path}.{os.getpid()}.{id(self)}.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump({"v": CACHE_VERSION, "items": dict(self.data)}, fh)
        os.replace(tmp, self.path)
        self.dirty = False


def _stat(path: str) -> Tuple[int, float]:
    if os.path.isdir(path):
        size, mtime = 0, os.path.getmtime(path)
        for dp, _ds, fs in os.walk(path):
            for f in fs:
                try:
                    st = os.stat(os.path.join(dp, f))
                except OSError:
                    continue
                size += st.st_size
                mtime = max(mtime, st.st_mtime)
        return size, mtime
    st = os.stat(path)
    return st.st_size, st.st_mtime


# ---------------------------------------------------------------- inspection

def _apply_manifest(mod: Mod, text: str) -> None:
    unit = first_unit(parse_sii(text), "mod_package")
    if unit is None:
        mod.scan_problems.append(("manifest_broken", "manifest.sii есть, но в нём нет блока mod_package"))
        return
    mod.has_manifest = True
    mod.name = unit.get("display_name") or mod.name
    mod.version = unit.get("package_version") or ""
    mod.author = unit.get("author") or ""
    cats = unit.get("category") or []
    mod.categories = [c for c in (cats if isinstance(cats, list) else [cats]) if c]
    comp = unit.get("compatible_versions") or []
    mod.compatible = [c for c in (comp if isinstance(comp, list) else [comp]) if c]
    mod.icon = unit.get("icon") or ""
    mod._desc_file = unit.get("description_file") or ""  # type: ignore[attr-defined]


def inspect(mod: Mod) -> Mod:
    """Open the container and fill in manifest data, file hashes and layout problems."""
    try:
        c = open_container(mod.path)
    except ArchiveError as exc:
        mod.scan_problems.append(("broken_archive", str(exc)))
        return mod
    except OSError as exc:
        mod.scan_problems.append(("broken_archive", f"файл не читается: {exc}"))
        return mod
    with c:
        mod.kind = c.kind
        try:
            raw = c.read("manifest.sii")
        except ArchiveError as exc:
            raw = None
            mod.scan_problems.append(("broken_archive", str(exc)))
        if raw is not None:
            from .sii import load_sii_bytes, SiiError
            try:
                text, _ = load_sii_bytes(raw)
                _apply_manifest(mod, text)
            except SiiError as exc:
                mod.scan_problems.append(("manifest_broken", f"manifest.sii не читается: {exc}"))
            desc_file = getattr(mod, "_desc_file", "")
            if desc_file:
                try:
                    d = c.read(desc_file)
                    if d:
                        mod.description = d.decode("utf-8", "replace")[:3000]
                except ArchiveError:
                    pass

        # A CRC check reads the whole archive; worth it only for small files.
        if isinstance(c, ZipContainer) and os.path.getsize(mod.path) < CRC_CHECK_LIMIT:
            bad = c.test()
            if bad:
                mod.scan_problems.append(("broken_archive", f"повреждён файл внутри архива: {bad}"))

        root = c.root_names()
        _check_layout(mod, c, root)

        meta = list(ROOT_META) + ([mod.icon] if mod.icon else [])
        try:
            hashes, known = c.content_hashes(meta)
        except (ArchiveError, OSError, zlib.error) as exc:
            mod.scan_problems.append(("broken_archive", str(exc)))
            hashes, known = [], True
        mod.hashes = sorted(hashes)
        mod.paths_known = known
    if not mod.name:
        mod.name = mod.key
    return mod


def _check_layout(mod: Mod, c, root: List[str]) -> None:
    lowered = [r.lower() for r in root]
    inner_mods = [r for r in lowered if r.endswith(MOD_EXTS)]
    if inner_mods and not any(r in KNOWN_ROOT_DIRS for r in lowered):
        mod.scan_problems.append((
            "nested_archive",
            "внутри лежат другие моды (" + ", ".join(inner_mods[:3]) +
            "). Распакуйте их и положите в папку mod по отдельности",
        ))
        return
    if mod.has_manifest or not root:
        return
    if any(r in KNOWN_ROOT_DIRS or r.startswith("dlc_") for r in lowered):
        return
    if len(root) == 1:
        try:
            if c.read(root[0] + "/manifest.sii") is not None:
                mod.scan_problems.append((
                    "nested_folder",
                    f"мод упакован с лишней папкой «{root[0]}»: игра его не увидит. "
                    "Файлы из неё нужно перенести на уровень выше",
                ))
                return
        except ArchiveError:
            pass
    mod.scan_problems.append((
        "unknown_layout",
        "не похоже на мод: в корне нет manifest.sii и привычных папок (def, vehicle, map…)",
    ))


# ---------------------------------------------------------------- discovery

def _local_candidates(mod_dir: str):
    if not os.path.isdir(mod_dir):
        return [], []
    mods, junk = [], []
    for name in sorted(os.listdir(mod_dir), key=str.lower):
        full = os.path.join(mod_dir, name)
        low = name.lower()
        if os.path.isdir(full):
            mods.append((name, full))
        elif low.endswith(MOD_EXTS):
            mods.append((os.path.splitext(name)[0], full))
        elif low.endswith(JUNK_ARCHIVES):
            junk.append(full)
    return mods, junk


def _pick_slot(ws_dir: str, game_version: Optional[str], full_version: Optional[str]):
    """Return (slot_name, payload_path) for a workshop item with versions.sii."""
    path = os.path.join(ws_dir, "versions.sii")
    with open(path, "rb") as fh:
        from .sii import load_sii_bytes
        text, _ = load_sii_bytes(fh.read())
    slots = []
    for u in parse_sii(text):
        if u.cls != "package_version_info":
            continue
        name = u.get("package_name") or u.name.lstrip(".")
        comp = u.get("compatible_versions") or []
        slots.append((name, comp if isinstance(comp, list) else [comp]))
    chosen = None
    for name, comp in slots:
        if comp and game_version and (
            version_matches(game_version, comp) or (full_version and version_matches(full_version, comp))
        ):
            chosen = name
            break
    if chosen is None:
        for name, comp in slots:
            if not comp:
                chosen = name
                break
    if chosen is None and slots:
        chosen = slots[0][0]
    if chosen is None:
        return None, None
    for cand in (chosen, chosen + ".scs", chosen + ".zip"):
        p = os.path.join(ws_dir, cand)
        if os.path.exists(p):
            return chosen, p
    return chosen, None


def _workshop_payload(ws_dir: str, game_version, full_version):
    if os.path.isfile(os.path.join(ws_dir, "versions.sii")):
        return _pick_slot(ws_dir, game_version, full_version)
    if os.path.isfile(os.path.join(ws_dir, "manifest.sii")):
        return None, ws_dir
    payload = [f for f in os.listdir(ws_dir) if f.lower().endswith(MOD_EXTS)]
    if len(payload) == 1:
        return None, os.path.join(ws_dir, payload[0])
    if payload:
        return None, os.path.join(ws_dir, sorted(payload)[0])
    return None, None


@dataclass
class ScanResult:
    mods: List[Mod]
    junk_files: List[str]
    workshop_state: Dict[int, dict]


def read_workshop_state(acf_paths: List[str]) -> Dict[int, dict]:
    """Merge appworkshop_<app>.acf: what Steam has installed vs. what it knows is newest."""
    out: Dict[int, dict] = {}
    for acf in acf_paths:
        try:
            data = vdf.load(acf).get("AppWorkshop", {})
        except OSError:
            continue
        for wid, info in (data.get("WorkshopItemsInstalled") or {}).items():
            if wid.isdigit() and isinstance(info, dict):
                out.setdefault(int(wid), {})["installed"] = info
        for wid, info in (data.get("WorkshopItemDetails") or {}).items():
            if wid.isdigit() and isinstance(info, dict):
                out.setdefault(int(wid), {})["details"] = info
    return out


def scan(mod_dir: str, workshop_dirs: List[str], cache: ScanCache,
         game_version: Optional[str], full_version: Optional[str],
         acf_paths: Optional[List[str]] = None, progress=None) -> ScanResult:
    """progress(done, total, name) is called before each mod is read."""
    mods: List[Mod] = []
    cands, junk = _local_candidates(mod_dir)
    ws_items = []
    for ws_root in workshop_dirs:
        try:
            names = sorted(os.listdir(ws_root))
        except OSError:
            continue
        for wid in names:
            if wid.isdigit() and os.path.isdir(os.path.join(ws_root, wid)):
                ws_items.append((ws_root, wid))
    total = len(cands) + len(ws_items)
    done = 0
    inspector = Inspector()

    def tick(name):
        nonlocal done
        if progress:
            progress(done, total, name)
        done += 1

    for key, path in cands:
        tick(os.path.basename(path))
        mods.append(_load(Mod(key=key, source="local", path=path, name=key), cache, inspector))
        if done % 20 == 0:
            cache.save()                 # a long first scan survives being interrupted

    for ws_root, wid in ws_items:
        tick(f"Workshop {wid}")
        ws_dir = os.path.join(ws_root, wid)
        key = workshop_key(int(wid))
        try:
            slot, payload = _workshop_payload(ws_dir, game_version, full_version)
        except OSError:
            slot, payload = None, None
        if payload is None:
            m = Mod(key=key, source="workshop", path=ws_dir, name=f"Workshop {wid}",
                    workshop_id=int(wid), workshop_slot=slot)
            m.scan_problems.append((
                "workshop_empty",
                "папка мода из Workshop пуста или в ней нет версии для этой игры. "
                "Отпишитесь и подпишитесь заново или проверьте файлы игры в Steam",
            ))
            mods.append(m)
            continue
        m = Mod(key=key, source="workshop", path=payload, name=f"Workshop {wid}",
                workshop_id=int(wid), workshop_slot=slot)
        mods.append(_load(m, cache, inspector))
    inspector.close()
    if progress:
        progress(total, total, "")
    cache.save()
    return ScanResult(mods, junk, read_workshop_state(acf_paths or []))


def _load(mod: Mod, cache: ScanCache, inspector: Optional[Inspector] = None) -> Mod:
    try:
        size, mtime = _stat(mod.path)
    except OSError as exc:
        mod.scan_problems.append(("broken_archive", f"файл не читается: {exc}"))
        return mod
    mod.size, mod.mtime = size, mtime
    cached = cache.get(mod.path, size, mtime)
    if cached is not None:
        fresh = Mod(**{k: v for k, v in cached.items() if k in Mod.__dataclass_fields__})
        fresh.key, fresh.source = mod.key, mod.source
        fresh.workshop_id, fresh.workshop_slot = mod.workshop_id, mod.workshop_slot
        fresh.scan_problems = [tuple(p) for p in fresh.scan_problems]  # type: ignore[misc]
        return fresh
    if inspector is not None:
        mod = inspector.inspect(mod)
    else:
        inspect_safely(mod)
    cache.put(mod.path, size, mtime, mod)
    return mod


INSPECT_TIMEOUT = 120          # seconds per mod
WORKER_MEMORY = 3 << 30        # bytes; a zip bomb hits this instead of the Deck's RAM


def _worker(conn) -> None:
    """Runs in a child process: inspect mods one by one, sent over a pipe."""
    try:
        import resource
        resource.setrlimit(resource.RLIMIT_AS, (WORKER_MEMORY, WORKER_MEMORY))
    except (ImportError, ValueError, OSError):
        pass  # not supported here (macOS); the time limit still applies
    while True:
        try:
            data = conn.recv()
        except EOFError:
            return
        if data is None:
            return
        mod = Mod(**data)
        mod.scan_problems = [tuple(p) for p in mod.scan_problems]
        try:
            inspect_safely(mod)
        except MemoryError:
            mod.scan_problems.append(("unreadable", "мод слишком тяжёлый для проверки"))
        conn.send(asdict(mod))


class Inspector:
    """Inspects mods in a separate process so that one strange file cannot hang
    or exhaust the whole program. Falls back to in-process inspection."""

    def __init__(self, timeout: float = INSPECT_TIMEOUT):
        self.timeout = timeout
        self.proc = None
        self.conn = None

    def _start(self) -> bool:
        try:
            import multiprocessing as mp
            ctx = mp.get_context("spawn")
            parent, child = ctx.Pipe()
            proc = ctx.Process(target=_worker, args=(child,), daemon=True)
            proc.start()
            child.close()
            self.proc, self.conn = proc, parent
            return True
        except Exception:  # noqa: BLE001 — no subprocesses here: inspect in place
            self.proc = self.conn = None
            return False

    def inspect(self, mod: Mod) -> Mod:
        if self.proc is None or not self.proc.is_alive():
            if not self._start():
                return inspect_safely(mod)
        try:
            self.conn.send(asdict(mod))
            if self.conn.poll(self.timeout):
                data = self.conn.recv()
                fresh = Mod(**data)
                fresh.scan_problems = [tuple(p) for p in fresh.scan_problems]
                return fresh
        except (EOFError, OSError, BrokenPipeError):
            pass  # the worker died (out of memory, crash): report below
        self.close()
        mod.scan_problems.append((
            "unreadable",
            f"DeckHaul не смог прочитать этот мод за {int(self.timeout)} секунд или ему не хватило памяти, "
            "проверка пропущена. Игра, возможно, читает его нормально",
        ))
        if not mod.name:
            mod.name = mod.key
        return mod

    def close(self) -> None:
        if self.conn is not None:
            try:
                self.conn.send(None)
            except (OSError, BrokenPipeError):
                pass
            self.conn.close()
        if self.proc is not None:
            self.proc.join(timeout=1)
            if self.proc.is_alive():
                self.proc.kill()
        self.proc = self.conn = None


def inspect_safely(mod: Mod) -> Mod:
    """inspect() that can never stop the whole scan: any surprise in one file
    becomes a problem of that mod."""
    try:
        return inspect(mod)
    except Exception as exc:  # noqa: BLE001 — a strange file must not break the scan
        mod.scan_problems.append((
            "unreadable", f"DeckHaul не смог прочитать этот мод ({type(exc).__name__}: {exc}). "
            "Игра, возможно, читает его нормально",
        ))
        if not mod.name:
            mod.name = mod.key
        return mod
