"""Find Steam libraries, the game's user-data folder and workshop content on a Steam Deck."""

from __future__ import annotations

import glob
import os
import re
from dataclasses import dataclass, field
from typing import List, Optional

HOME = os.path.expanduser("~")


@dataclass(frozen=True)
class Game:
    key: str
    app_id: int
    title: str
    binary: str


GAMES = {
    "ets2": Game("ets2", 227300, "Euro Truck Simulator 2", "eurotrucks2"),
    "ats": Game("ats", 270880, "American Truck Simulator", "amtrucks"),
}


@dataclass
class DataDir:
    path: str
    flavor: str  # "native" | "proton"
    log_mtime: float = 0.0

    @property
    def mod_dir(self) -> str:
        return os.path.join(self.path, "mod")


@dataclass
class Layout:
    game: Game
    steam_roots: List[str] = field(default_factory=list)
    libraries: List[str] = field(default_factory=list)
    install_dir: Optional[str] = None
    workshop_dirs: List[str] = field(default_factory=list)
    workshop_acf: List[str] = field(default_factory=list)
    data_dirs: List[DataDir] = field(default_factory=list)
    active: Optional[DataDir] = None
    proton_forced: Optional[str] = None

    def to_dict(self):
        return {
            "game": self.game.title,
            "app_id": self.game.app_id,
            "install_dir": self.install_dir,
            "libraries": self.libraries,
            "workshop_dirs": self.workshop_dirs,
            "data_dirs": [{"path": d.path, "flavor": d.flavor} for d in self.data_dirs],
            "active": self.active.path if self.active else None,
            "active_flavor": self.active.flavor if self.active else None,
            "proton_forced": self.proton_forced,
        }


def _steam_roots() -> List[str]:
    cands = [
        os.path.join(HOME, ".local/share/Steam"),
        os.path.join(HOME, ".steam/steam"),
        os.path.join(HOME, ".steam/root"),
        os.path.join(HOME, ".var/app/com.valvesoftware.Steam/.local/share/Steam"),
    ]
    extra = os.environ.get("DECKHAUL_STEAM_ROOT")
    if extra:
        cands.insert(0, extra)
    out, seen = [], set()
    for c in cands:
        if os.path.isdir(os.path.join(c, "steamapps")):
            real = os.path.realpath(c)
            if real not in seen:
                seen.add(real)
                out.append(c)
    return out


def _library_folders(root: str) -> List[str]:
    libs = [root]
    vdf = os.path.join(root, "steamapps", "libraryfolders.vdf")
    try:
        with open(vdf, encoding="utf-8", errors="replace") as fh:
            for m in re.finditer(r'"path"\s+"([^"]+)"', fh.read()):
                libs.append(m.group(1).replace("\\\\", "\\"))
    except OSError:
        pass
    return libs


def _proton_mapping(root: str, app_id: int) -> Optional[str]:
    cfg = os.path.join(root, "config", "config.vdf")
    try:
        with open(cfg, encoding="utf-8", errors="replace") as fh:
            text = fh.read()
    except OSError:
        return None
    m = re.search(r'"CompatToolMapping"\s*\{(.*?)\n\t\t\t\}', text, re.S)
    block = m.group(1) if m else text
    m = re.search(r'"%d"\s*\{[^}]*?"name"\s+"([^"]*)"' % app_id, block, re.S)
    if m and m.group(1):
        return m.group(1)
    return None


def _log_mtime(path: str) -> float:
    try:
        return os.path.getmtime(os.path.join(path, "game.log.txt"))
    except OSError:
        return 0.0


def discover(game_key: str = "ets2", data_dir_override: Optional[str] = None) -> Layout:
    game = GAMES[game_key]
    lay = Layout(game=game)
    lay.steam_roots = _steam_roots()

    libs, seen = [], set()
    for root in lay.steam_roots:
        for lib in _library_folders(root):
            real = os.path.realpath(lib)
            if real not in seen and os.path.isdir(os.path.join(lib, "steamapps")):
                seen.add(real)
                libs.append(lib)
    lay.libraries = libs

    for lib in libs:
        inst = os.path.join(lib, "steamapps", "common", game.title)
        if os.path.isdir(inst) and lay.install_dir is None:
            lay.install_dir = inst
        ws = os.path.join(lib, "steamapps", "workshop", "content", str(game.app_id))
        if os.path.isdir(ws):
            lay.workshop_dirs.append(ws)
        acf = os.path.join(lib, "steamapps", "workshop", f"appworkshop_{game.app_id}.acf")
        if os.path.isfile(acf):
            lay.workshop_acf.append(acf)

    for root in lay.steam_roots:
        lay.proton_forced = lay.proton_forced or _proton_mapping(root, game.app_id)

    cands: List[DataDir] = []
    if data_dir_override:
        cands.append(DataDir(data_dir_override, "manual"))
    else:
        native = os.path.join(HOME, ".local/share", game.title)
        cands.append(DataDir(native, "native"))
        flat = os.path.join(HOME, ".var/app/com.valvesoftware.Steam/.local/share", game.title)
        cands.append(DataDir(flat, "native"))
        for lib in libs:
            pfx = os.path.join(
                lib, "steamapps", "compatdata", str(game.app_id), "pfx", "drive_c", "users",
                "steamuser", "Documents", game.title,
            )
            cands.append(DataDir(pfx, "proton"))
    for d in cands:
        if os.path.isdir(d.path):
            d.log_mtime = _log_mtime(d.path)
            lay.data_dirs.append(d)

    if lay.data_dirs:
        # The folder the game wrote its log to most recently is the one in use.
        lay.active = max(lay.data_dirs, key=lambda d: d.log_mtime)
    return lay


def game_running(game: Game) -> bool:
    for cmd in glob.glob("/proc/[0-9]*/cmdline"):
        try:
            with open(cmd, "rb") as fh:
                data = fh.read().lower()
        except OSError:
            continue
        if game.binary.encode() in data and b"deckhaul" not in data:
            return True
    return False


def state_dir() -> str:
    base = os.environ.get("XDG_DATA_HOME") or os.path.join(HOME, ".local/share")
    d = os.environ.get("DECKHAUL_STATE_DIR") or os.path.join(base, "deckhaul")
    os.makedirs(d, exist_ok=True)
    return d
