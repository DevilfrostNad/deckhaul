"""What DLC the user really has, from local Steam and game files only.

Four sources, none complete on its own:

1. appmanifest_<app>.acf, "InstalledDepots": every installed DLC depot carries
   "dlcappid" (the DLC's Steam app id; it can differ from the depot id).
2. appmanifest, "UserConfig"/"MountedConfig" -> "DisabledDLC": DLC the user
   switched off in the game's Steam properties (Steam then removes its files).
3. The game folder: dlc_*.scs archives present on disk.
4. game.log.txt: "[hashfs] dlc_*.scs: ..." lines for archives the game
   actually mounted at the last start.

A DLC whose content lives inside the base game files has no depot of its own,
so absence from (1) is not proof it is missing. DeckHaul therefore answers
"yes", "switched off" or "not found", and words the last one carefully.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from . import vdf

_MOUNTED = re.compile(r"\[hashfs\]\s+(dlc_[\w-]+)\.scs\b", re.I)


@dataclass
class DlcState:
    manifest: Optional[str] = None          # path of the appmanifest that was read
    installed: Set[int] = field(default_factory=set)
    disabled: Set[int] = field(default_factory=set)
    depots: Dict[int, int] = field(default_factory=dict)   # dlc app id -> depot id
    files: List[str] = field(default_factory=list)          # dlc_*.scs in the game folder
    mounted: List[str] = field(default_factory=list)        # dlc_*.scs from game.log.txt
    install_dir: Optional[str] = None

    def status(self, app_id: int) -> str:
        """'installed' | 'disabled' | 'not_found' | 'unknown'"""
        if app_id in self.disabled:
            return "disabled"
        if app_id in self.installed:
            return "installed"
        if self.manifest and self.installed:
            return "not_found"
        return "unknown"

    def public(self) -> dict:
        return {
            "manifest": bool(self.manifest), "installed": sorted(self.installed),
            "disabled": sorted(self.disabled), "files": self.files, "mounted": self.mounted,
            "depots": {str(k): v for k, v in sorted(self.depots.items())},
        }


def _ids(value) -> Set[int]:
    return {int(x) for x in re.findall(r"\d+", str(value or ""))}


def read(layout, game_log_path: Optional[str] = None) -> DlcState:
    st = DlcState()
    for lib in getattr(layout, "libraries", []):
        acf = os.path.join(lib, "steamapps", f"appmanifest_{layout.game.app_id}.acf")
        if not os.path.isfile(acf):
            continue
        try:
            app = vdf.load(acf).get("AppState", {})
        except OSError:
            break
        st.manifest = acf
        for depot, info in (app.get("InstalledDepots") or {}).items():
            if isinstance(info, dict) and str(info.get("dlcappid", "")).isdigit():
                dlc = int(info["dlcappid"])
                st.installed.add(dlc)
                if depot.isdigit():
                    st.depots[dlc] = int(depot)
        for block in ("UserConfig", "MountedConfig"):
            st.disabled |= _ids((app.get(block) or {}).get("DisabledDLC"))
        st.installed -= st.disabled
        installdir = app.get("installdir")
        if installdir:
            st.install_dir = os.path.join(lib, "steamapps", "common", installdir)
        break
    st.install_dir = st.install_dir or getattr(layout, "install_dir", None)
    if st.install_dir and os.path.isdir(st.install_dir):
        st.files = sorted(os.path.splitext(n)[0] for n in os.listdir(st.install_dir)
                          if n.lower().startswith("dlc_") and n.lower().endswith(".scs"))
    if game_log_path and os.path.isfile(game_log_path):
        try:
            with open(game_log_path, encoding="utf-8", errors="replace") as fh:
                st.mounted = sorted({m.group(1).lower() for m in _MOUNTED.finditer(fh.read())})
        except OSError:
            pass
    return st
