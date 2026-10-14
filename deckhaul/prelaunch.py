"""Check mods right before ETS2 starts.

Set as the game's launch options in Steam:

    "/home/deck/.local/bin/deckhaul" prelaunch %command%

A quick look (about a second, no archive is opened) at the profile played last:
enabled mods that are gone, mods made for another game version, and whether
Steam updated the game since DeckHaul last saw it. When something is wrong a
dialog offers "start the game" or "open DeckHaul". Whatever happens inside
this check, the game starts: a broken check must never block playing.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from typing import List, Optional

from . import paths, vdf
from .gamelog import read_game_log
from .mods import ScanCache, workshop_key
from .profiles import load_profile
from .sii import version_matches


def game_buildid(layout) -> Optional[str]:
    for lib in layout.libraries:
        acf = os.path.join(lib, "steamapps", f"appmanifest_{layout.game.app_id}.acf")
        if os.path.isfile(acf):
            try:
                return vdf.load(acf).get("AppState", {}).get("buildid")
            except OSError:
                return None
    return None


def _last_played_profile(data_dir: str):
    best = None
    for sub, cloud in (("profiles", False), ("steam_profiles", True)):
        base = os.path.join(data_dir, sub)
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for n in names:
            sii = os.path.join(base, n, "profile.sii")
            try:
                t = os.path.getmtime(sii)
            except OSError:
                continue
            if best is None or t > best[0]:
                best = (t, os.path.join(base, n), cloud)
    return load_profile(best[1], best[2]) if best else None


def quick_check(game: str = "ets2", state_dir: Optional[str] = None) -> List[str]:
    """Return human-readable problems; [] when everything looks fine."""
    state_dir = state_dir or paths.state_dir()
    layout = paths.discover(game)
    act = layout.active
    if act is None:
        return []
    prof = _last_played_profile(act.path)
    if prof is None:
        return []

    installed = {}
    try:
        for n in os.listdir(act.mod_dir):
            full = os.path.join(act.mod_dir, n)
            if os.path.isdir(full):
                installed[n] = full
            elif n.lower().endswith((".scs", ".zip")):
                installed[os.path.splitext(n)[0]] = full
    except OSError:
        pass
    for ws in layout.workshop_dirs:
        try:
            for wid in os.listdir(ws):
                d = os.path.join(ws, wid)
                if wid.isdigit() and os.path.isdir(d) and os.listdir(d):
                    installed[workshop_key(int(wid))] = d
        except OSError:
            continue

    problems: List[str] = []
    missing = [e.label or e.key for e in prof.active if e.key not in installed]
    if missing:
        problems.append(f"Нет на диске включённых модов: {len(missing)} — " + ", ".join(missing[:4])
                        + ("…" if len(missing) > 4 else ""))

    log = read_game_log(act.path)
    gv, full = (log.short_version, log.version) if log else (None, None)
    import json
    settings = {}
    try:
        with open(os.path.join(state_dir, "settings.json"), encoding="utf-8") as fh:
            settings = json.load(fh)
    except (OSError, ValueError):
        pass
    gv = settings.get("game_version") or gv
    marks = settings.get("compat_ok", {})

    cache = ScanCache(os.path.join(state_dir, "scan_cache.json"))
    by_path = {p: item["mod"] for p, item in cache.data.items()}
    outdated = []
    for e in prof.active:
        path = installed.get(e.key)
        if not path or not gv:
            continue
        cached = by_path.get(path)
        if cached is None and os.path.isdir(path):     # workshop item: payload lives inside
            cached = next((m for p, m in by_path.items() if p.startswith(path + os.sep)), None)
        comp = (cached or {}).get("compatible") or []
        if not comp or version_matches(gv, comp) or (full and version_matches(full, comp)):
            continue
        mark = marks.get(e.key) or {}
        if mark.get("game") == gv and mark.get("version") == (cached or {}).get("version"):
            continue
        outdated.append(e.label or e.key)
    if outdated:
        problems.append(f"Рассчитаны на другую версию игры ({gv}): {len(outdated)} — " + ", ".join(outdated[:4])
                        + ("…" if len(outdated) > 4 else ""))

    seen = settings.get("seen_buildid")
    now = game_buildid(layout)
    if seen and now and seen != now:
        problems.append("Steam обновил игру с прошлой проверки. После обновления часть модов может перестать "
                        "работать: откройте DeckHaul после первого запуска.")
    return problems


def _ask(problems: List[str]) -> str:
    """'play', 'open' or 'cancel'. Without any dialog tool: 'play'."""
    text = "Перед запуском ETS2 DeckHaul нашёл:\n\n" + "\n\n".join("• " + p for p in problems)
    if shutil.which("kdialog"):
        r = subprocess.run(["kdialog", "--title", "DeckHaul", "--warningyesnocancel", text,
                            "--yes-label", "Запустить игру", "--no-label", "Открыть DeckHaul",
                            "--cancel-label", "Не запускать"])
        return {0: "play", 1: "open"}.get(r.returncode, "cancel")
    if shutil.which("zenity"):
        r = subprocess.run(["zenity", "--question", "--title=DeckHaul", f"--text={text}",
                            "--ok-label=Запустить игру", "--cancel-label=Открыть DeckHaul"])
        return "play" if r.returncode == 0 else "open"
    return "play"


def main(command: List[str]) -> int:
    """Entry point for `deckhaul prelaunch %command%`."""
    if not command:
        print("Использование в параметрах запуска Steam: deckhaul prelaunch %command%", file=sys.stderr)
        return 2
    choice = "play"
    try:
        problems = quick_check()
        if problems:
            choice = _ask(problems)
    except Exception as exc:  # noqa: BLE001 — the game must start no matter what
        print(f"DeckHaul: проверка перед запуском не удалась: {exc}", file=sys.stderr)
        choice = "play"
    if choice == "open":
        launcher = os.path.expanduser("~/.local/bin/deckhaul")
        if os.path.exists(launcher):
            subprocess.call([launcher])          # blocks while DeckHaul is open
        return 0
    if choice == "cancel":
        return 0
    os.execvp(command[0], command)
    return 0  # not reached


def launch_option() -> str:
    return f'"{os.path.expanduser("~/.local/bin/deckhaul")}" prelaunch %command%'


def configured(app_id: int) -> Optional[bool]:
    """Whether Steam's launch options for the game already call DeckHaul.
    None when Steam's config is not found."""
    found = False
    for root in paths._steam_roots():
        for cfg in _glob(os.path.join(root, "userdata", "*", "config", "localconfig.vdf")):
            found = True
            try:
                data = vdf.load(cfg)
            except OSError:
                continue
            apps = _dig(data, ["UserLocalConfigStore", "Software", "Valve", "Steam", "apps"]) or {}
            opts = (_dig(apps, [str(app_id)]) or {}).get("LaunchOptions", "") if isinstance(apps, dict) else ""
            if "deckhaul" in str(opts) and "prelaunch" in str(opts):
                return True
    return False if found else None


def _glob(pattern: str) -> List[str]:
    import glob
    return glob.glob(pattern)


def _dig(d, keys):
    for k in keys:
        if not isinstance(d, dict):
            return None
        match = next((v for kk, v in d.items() if kk.lower() == k.lower()), None)
        d = match
    return d
