"""Check GitHub for a newer DeckHaul and install it.

Versions are git tags like v1.1.0. Release notes come from CHANGELOG.md of
that tag, so publishing a version needs nothing but `git tag` and `git push`.
Only public, unauthenticated GitHub endpoints are used.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tarfile
import tempfile
import time
import urllib.request
from typing import List, Optional, Tuple

from . import __version__

REPO = "DevilfrostNad/deckhaul"
API = f"https://api.github.com/repos/{REPO}"
ARCHIVE = f"https://github.com/{REPO}/archive/refs/tags/{{tag}}.tar.gz"
RAW = f"https://raw.githubusercontent.com/{REPO}/{{tag}}/CHANGELOG.md"
CHECK_EVERY = 24 * 3600
APP_DIR = os.path.expanduser("~/.local/share/deckhaul-app")
UA = {"User-Agent": f"DeckHaul/{__version__}", "Accept": "application/vnd.github+json"}


class UpdateError(Exception):
    pass


def parse_version(tag: str) -> Optional[Tuple[int, ...]]:
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", (tag or "").strip())
    return tuple(int(x) for x in m.groups()) if m else None


def _get(url: str, timeout: float = 15.0) -> bytes:
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def latest_tag(fetch=_get) -> Optional[str]:
    tags = json.loads(fetch(f"{API}/tags?per_page=100"))
    best, best_v = None, None
    for t in tags:
        v = parse_version(t.get("name", ""))
        if v and (best_v is None or v > best_v):
            best, best_v = t["name"], v
    return best


def changelog_section(text: str, version: str) -> str:
    """Return the CHANGELOG.md block of one version ('## 1.1.0 …' up to the next '## ')."""
    out: List[str] = []
    inside = False
    for line in text.splitlines():
        if line.startswith("## "):
            if inside:
                break
            inside = version in line
            continue
        if inside:
            out.append(line)
    return "\n".join(out).strip()


def installed_copy() -> bool:
    """True when running from the folder install.sh created (not from a git checkout)."""
    here = os.path.realpath(os.path.dirname(os.path.dirname(__file__)))
    return here == os.path.realpath(APP_DIR)


class Updater:
    def __init__(self, state_dir: str, fetch=_get):
        self.path = os.path.join(state_dir, "update.json")
        self.fetch = fetch
        self.data = {"checked": 0, "latest": None, "notes": "", "error": ""}
        try:
            with open(self.path, encoding="utf-8") as fh:
                self.data.update(json.load(fh))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        with open(self.path, "w", encoding="utf-8") as fh:
            json.dump(self.data, fh, ensure_ascii=False)

    def status(self) -> dict:
        latest = self.data.get("latest")
        newer = bool(latest and parse_version(latest) and parse_version(latest) > parse_version(__version__))
        return {
            "current": __version__, "latest": latest, "available": newer,
            "notes": self.data.get("notes", "") if newer else "",
            "checked": self.data.get("checked", 0), "error": self.data.get("error", ""),
            "can_apply": installed_copy(),
            "repo": f"https://github.com/{REPO}",
        }

    def check(self, force: bool = False) -> dict:
        if not force and time.time() - self.data.get("checked", 0) < CHECK_EVERY:
            return self.status()
        try:
            tag = latest_tag(self.fetch)
            notes = ""
            if tag and parse_version(tag) > parse_version(__version__):
                try:
                    notes = changelog_section(self.fetch(RAW.format(tag=tag)).decode("utf-8", "replace"),
                                              tag.lstrip("v"))
                except OSError:
                    notes = ""
            self.data.update({"checked": time.time(), "latest": tag, "notes": notes, "error": ""})
        except (OSError, ValueError) as exc:
            self.data.update({"checked": time.time(), "error": f"нет связи с GitHub: {exc}"})
        self._save()
        return self.status()

    def apply(self, tag: Optional[str] = None, archive_url: Optional[str] = None) -> str:
        """Download the tagged version and run its install.sh. Returns the installed tag."""
        tag = tag or self.data.get("latest")
        if not tag or not parse_version(tag):
            raise UpdateError("Неизвестно, какую версию ставить. Сначала проверьте обновления.")
        if not archive_url and not installed_copy():
            raise UpdateError("DeckHaul запущен не из установленной копии. "
                              "Обновите папку с исходниками командой git pull.")
        url = archive_url or ARCHIVE.format(tag=tag)
        work = tempfile.mkdtemp(prefix="deckhaul-update-")
        try:
            blob = os.path.join(work, "src.tar.gz")
            try:
                with open(blob, "wb") as fh:
                    fh.write(self.fetch(url, timeout=120))
            except OSError as exc:
                raise UpdateError(f"Не удалось скачать обновление: {exc}")
            root = _safe_extract(blob, os.path.join(work, "src"))
            for need in ("install.sh", os.path.join("deckhaul", "__init__.py")):
                if not os.path.isfile(os.path.join(root, need)):
                    raise UpdateError("Скачанный архив не похож на DeckHaul, обновление отменено.")
            env = dict(os.environ, DECKHAUL_SELF_UPDATE="1")
            res = subprocess.run(["bash", os.path.join(root, "install.sh")], cwd=root, env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=300)
            if res.returncode != 0:
                tail = res.stdout.decode("utf-8", "replace").strip().splitlines()[-3:]
                raise UpdateError("Установщик новой версии завершился с ошибкой: " + " ".join(tail))
        finally:
            shutil.rmtree(work, ignore_errors=True)
        return tag


def _safe_extract(archive: str, dest: str) -> str:
    """Extract a GitHub source tarball; refuse links and paths leaving dest. Returns the top folder."""
    os.makedirs(dest)
    root = os.path.realpath(dest)
    with tarfile.open(archive, "r:gz") as tf:
        members = []
        for m in tf.getmembers():
            target = os.path.realpath(os.path.join(dest, m.name))
            if not (target == root or target.startswith(root + os.sep)):
                raise UpdateError("В архиве обновления опасный путь, обновление отменено.")
            if m.issym() or m.islnk() or m.isdev():
                continue
            members.append(m)
        if hasattr(tarfile, "data_filter"):
            tf.extractall(dest, members=members, filter="data")
        else:
            tf.extractall(dest, members=members)
    tops = [n for n in os.listdir(dest) if os.path.isdir(os.path.join(dest, n))]
    if len(tops) != 1:
        raise UpdateError("Скачанный архив не похож на DeckHaul, обновление отменено.")
    return os.path.join(dest, tops[0])
