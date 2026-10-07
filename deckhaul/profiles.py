"""Game profiles: list them, read the active mod list, write a new one with a backup."""

from __future__ import annotations

import os
import shutil
import time
from dataclasses import dataclass, field
from typing import List, Optional

from .sii import (
    BinarySiiError, SiiError, bsii_guess_active_mods, first_unit, load_sii_bytes, parse_sii,
    read_active_mods, write_active_mods,
)


@dataclass
class ActiveEntry:
    key: str
    label: str

    @staticmethod
    def parse(raw: str) -> "ActiveEntry":
        key, _, label = raw.partition("|")
        return ActiveEntry(key.strip(), label.strip())

    def raw(self) -> str:
        return f"{self.key}|{self.label}"


@dataclass
class Profile:
    id: str
    folder: str
    cloud: bool
    name: str = ""
    storage: str = "text"
    active: List[ActiveEntry] = field(default_factory=list)
    writable: bool = True
    note: str = ""
    mtime: float = 0.0

    @property
    def sii_path(self) -> str:
        return os.path.join(self.folder, "profile.sii")

    def public(self) -> dict:
        return {
            "id": self.id, "name": self.name, "cloud": self.cloud, "storage": self.storage,
            "writable": self.writable, "note": self.note, "mtime": self.mtime,
            "active_count": len(self.active),
        }


def _decode_dir_name(name: str) -> str:
    try:
        return bytes.fromhex(name).decode("utf-8")
    except ValueError:
        return name


def load_profile(folder: str, cloud: bool) -> Optional[Profile]:
    sii = os.path.join(folder, "profile.sii")
    if not os.path.isfile(sii):
        return None
    pid = ("cloud:" if cloud else "") + os.path.basename(folder)
    p = Profile(id=pid, folder=folder, cloud=cloud, name=_decode_dir_name(os.path.basename(folder)))
    p.mtime = os.path.getmtime(sii)
    with open(sii, "rb") as fh:
        raw = fh.read()
    try:
        text, p.storage = load_sii_bytes(raw)
        unit = first_unit(parse_sii(text), "user_profile")
        if unit is not None and unit.get("profile_name"):
            p.name = unit.get("profile_name")
        p.active = [ActiveEntry.parse(x) for x in read_active_mods(text)]
    except BinarySiiError as exc:
        p.storage = "binary"
        p.writable = False
        p.active = [ActiveEntry.parse(x) for x in bsii_guess_active_mods(exc.raw)]
        p.note = (
            "Профиль сохранён в двоичном формате. Список модов прочитан приблизительно, "
            "записать новый порядок нельзя. Откройте профиль в игре и сохраните его "
            "после включения текстового формата (см. README)."
        )
    except SiiError as exc:
        p.writable = False
        p.note = f"profile.sii не читается: {exc}"
    return p


def list_profiles(data_dir: str) -> List[Profile]:
    out: List[Profile] = []
    for sub, cloud in (("profiles", False), ("steam_profiles", True)):
        base = os.path.join(data_dir, sub)
        if not os.path.isdir(base):
            continue
        for name in sorted(os.listdir(base)):
            folder = os.path.join(base, name)
            if os.path.isdir(folder):
                prof = load_profile(folder, cloud)
                if prof:
                    out.append(prof)
    out.sort(key=lambda p: -p.mtime)
    return out


# ------------------------------------------------------------------- backups

def backup_dir(state_dir: str, profile: Profile) -> str:
    d = os.path.join(state_dir, "backups", profile.id.replace(":", "_"))
    os.makedirs(d, exist_ok=True)
    return d


def make_backup(state_dir: str, profile: Profile, reason: str = "manual") -> str:
    d = backup_dir(state_dir, profile)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    dst = os.path.join(d, f"{stamp}-{reason}.sii")
    n = 1
    while os.path.exists(dst):
        dst = os.path.join(d, f"{stamp}-{reason}-{n}.sii")
        n += 1
    shutil.copy2(profile.sii_path, dst)
    return dst


def list_backups(state_dir: str, profile: Profile) -> List[dict]:
    d = backup_dir(state_dir, profile)
    out = []
    for name in sorted(os.listdir(d), reverse=True):
        full = os.path.join(d, name)
        if name.endswith(".sii"):
            out.append({"name": name, "size": os.path.getsize(full), "mtime": os.path.getmtime(full)})
    return out


def restore_backup(state_dir: str, profile: Profile, name: str) -> str:
    d = backup_dir(state_dir, profile)
    src = os.path.join(d, os.path.basename(name))
    if not os.path.isfile(src):
        raise FileNotFoundError(name)
    safety = make_backup(state_dir, profile, "before-restore")
    shutil.copy2(src, profile.sii_path)
    return safety


def write_order(state_dir: str, profile: Profile, entries: List[ActiveEntry], backup: bool = True) -> str:
    """Write the new active_mods list, as plain text SII (the game reads it fine).
    The caller may skip the backup when it keeps its own copy (restore points)."""
    if not profile.writable:
        raise SiiError(profile.note or "профиль нельзя изменить")
    with open(profile.sii_path, "rb") as fh:
        raw = fh.read()
    text, _ = load_sii_bytes(raw)
    new_text = write_active_mods(text, [e.raw() for e in entries])
    # Make sure we can read back what we are about to write.
    check = [ActiveEntry.parse(x).key for x in read_active_mods(new_text)]
    if check != [e.key for e in entries]:
        raise SiiError("проверка записи не прошла, профиль не изменён")
    backup = make_backup(state_dir, profile, "before-apply") if backup else ""
    tmp = profile.sii_path + ".deckhaul.tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as fh:
        fh.write(new_text)
    os.replace(tmp, profile.sii_path)
    return backup
