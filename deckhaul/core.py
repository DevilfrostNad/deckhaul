"""The application service: one object the CLI and the web UI both talk to."""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
import time
from typing import Dict, List, Optional

from . import __version__, checks, installer, order, paths, profiles, restore, truckymods, updater
from .archive import ArchiveError, open_container
from .gamelog import read_game_log
from .history import History
from .mods import Mod, ScanCache, scan
from .profiles import ActiveEntry
from .sii import version_matches as sii_version_matches


class UserError(Exception):
    """Something the user can fix; the message is shown as is."""


class App:
    def __init__(self, game: str = "ets2", data_dir: Optional[str] = None, state_dir: Optional[str] = None):
        self.lock = threading.RLock()
        self.scan_lock = threading.Lock()
        self.scan_state = {"running": False, "done": 0, "total": 0, "current": "", "error": ""}
        self.state_dir = state_dir or paths.state_dir()
        os.makedirs(self.state_dir, exist_ok=True)
        self.settings_path = os.path.join(self.state_dir, "settings.json")
        self.settings = self._load_settings()
        self.game = game or self.settings.get("game", "ets2")
        self.data_dir_override = data_dir or self.settings.get("data_dir") or os.environ.get("DECKHAUL_DATA_DIR")
        self.user_rules_path = os.path.join(self.state_dir, "rules_user.json")
        self.cache = ScanCache(os.path.join(self.state_dir, "scan_cache.json"))
        self.history = History(os.path.join(self.state_dir, "history.json"))
        self.layout = None
        self.mods: List[Mod] = []
        self.by_key: Dict[str, Mod] = {}
        self.junk: List[str] = []
        self.workshop_state: Dict[int, dict] = {}
        self.profiles: List[profiles.Profile] = []
        self.profile: Optional[profiles.Profile] = None
        self.log = None
        self.issues: List[checks.Issue] = []
        self.new_events: List[dict] = []
        self.online: Dict[int, dict] = {}
        self.scanned_at = 0.0
        self.staging_root = os.path.join(self.state_dir, "staging")
        self.trash_root = os.path.join(self.state_dir, "trash")
        self.download_cache: Dict[str, installer.Candidate] = {}
        self.updater = updater.Updater(self.state_dir)
        self.points = restore.RestoreStore(self.state_dir)
        self.tm = truckymods.Client(os.path.join(self.state_dir, "truckymods_cache.json"), self.game)
        self.tm_updates: Dict[str, dict] = {}
        self.tm_checking = False
        self.online_path = os.path.join(self.state_dir, "workshop_online.json")
        self.online_checked = 0.0
        try:
            with open(self.online_path, encoding="utf-8") as fh:
                cached = json.load(fh)
            self.online = {int(k): v for k, v in cached.get("details", {}).items()}
            self.online_checked = cached.get("checked", 0.0)
        except (OSError, ValueError):
            pass

    # ---------------------------------------------------------------- settings
    def _load_settings(self) -> dict:
        try:
            with open(self.settings_path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def _save_settings(self) -> None:
        with open(self.settings_path, "w", encoding="utf-8") as fh:
            json.dump(self.settings, fh, ensure_ascii=False, indent=2)

    @property
    def overrides(self) -> Dict[str, str]:
        return self.settings.setdefault("overrides", {})

    @property
    def rules(self):
        return order.load_rules(self.user_rules_path)

    @property
    def game_version(self) -> Optional[str]:
        if self.settings.get("game_version"):
            return self.settings["game_version"]
        return self.log.short_version if self.log else None

    @property
    def full_version(self) -> Optional[str]:
        return self.log.version if self.log else None

    # ------------------------------------------------------------------- scan
    def refresh(self) -> None:
        """Rescan everything now (used by the CLI and after changes)."""
        with self.lock:
            self._assign(self._collect(None))

    def refresh_in_background(self) -> bool:
        """Start a scan in a thread; the UI polls scan_state. False if one is running."""
        if self.scan_state["running"]:
            return False
        self.scan_state.update(running=True, done=0, total=0, current="", error="")
        threading.Thread(target=self._background_refresh, daemon=True).start()
        return True

    def _background_refresh(self) -> None:
        try:
            with self.scan_lock:
                data = self._collect(self._progress)
            with self.lock:
                self._assign(data)
        except Exception as exc:  # shown in the UI instead of a silent dead page
            import traceback
            traceback.print_exc()
            self.scan_state["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            self.scan_state["running"] = False

    def _progress(self, done: int, total: int, name: str) -> None:
        self.scan_state.update(done=done, total=total, current=name)

    def _collect(self, progress) -> dict:
        """The slow part of a refresh. Touches no shared state, so it can run unlocked."""
        layout = paths.discover(self.game, self.data_dir_override)
        act = layout.active
        log = read_game_log(act.path) if act else None
        gv = self.settings.get("game_version") or (log.short_version if log else None)
        full = log.version if log else None
        res = scan(act.mod_dir if act else "", layout.workshop_dirs, self.cache,
                   gv, full, layout.workshop_acf, progress=progress)
        profs = profiles.list_profiles(act.path) if act else []
        return {"layout": layout, "log": log, "res": res, "profiles": profs}

    def _assign(self, data: dict) -> None:
        self.layout, self.log = data["layout"], data["log"]
        res = data["res"]
        self.mods, self.junk, self.workshop_state = res.mods, res.junk_files, res.workshop_state
        self.by_key = {}
        for m in self.mods:
            self.by_key.setdefault(m.key, m)
        self.profiles = data["profiles"]
        want = self.settings.get("profile")
        self.profile = next((p for p in self.profiles if p.id == want), None) or (
            self.profiles[0] if self.profiles else None
        )
        self._apply_online_titles()
        self._remember_buildid()
        self.new_events = self.history.update(self.mods, self.full_version or self.game_version)
        self._recheck()
        self.scanned_at = time.time()

    def _recheck(self, active: Optional[List[ActiveEntry]] = None) -> List[checks.Issue]:
        current = active is None
        if current:
            active = self.profile.active if self.profile else []
        issues = checks.check_all(
            self.layout, self.mods, self.junk, active, self.game_version, self.full_version,
            self.log, self.rules, self.overrides, self.workshop_state, self.compat_status,
        )
        issues += self._online_issues(active)
        issues += self._tm_issues({e.key for e in active})
        for i in issues:
            if i.code == "incompatible" and i.mod in self.by_key:
                m = self.by_key[i.mod]
                i.extra["source"] = self.source_of(m) if m.source == "local" else ""
                i.extra["workshop_id"] = m.workshop_id
        if current:
            issues += self._after_change_issue()
        if current:
            self.issues = issues
        return issues

    def _online_issues(self, active: List[ActiveEntry]) -> List[checks.Issue]:
        out = []
        keys = {e.key for e in active}
        for m in self.mods:
            if m.workshop_id is None or m.workshop_id not in self.online:
                continue
            info = self.online[m.workshop_id]
            if info.get("result") != 1 or info.get("banned"):
                out.append(checks.Issue(
                    "workshop_gone", checks.WARNING if m.key in keys else checks.INFO,
                    f"«{m.name}» удалён из Steam Workshop",
                    "Автор убрал мод или его заблокировали. Обновлений больше не будет, "
                    "а Steam может удалить файлы при следующей проверке.",
                    "Найдите замену или сохраните копию мода в папку mod.", mod=m.key,
                ))
                continue
            remote = int(info.get("time_updated") or 0)
            inst = (self.workshop_state.get(m.workshop_id) or {}).get("installed") or {}
            local = int(inst.get("timeupdated") or 0) or int(m.mtime)
            if remote and local and remote > local + 60:
                out.append(checks.Issue(
                    "workshop_outdated", checks.WARNING if m.key in keys else checks.INFO,
                    f"Для «{m.name}» в Workshop есть обновление",
                    "Steam ещё не скачал новую версию.",
                    "Откройте Steam в режиме рабочего стола и дождитесь загрузки.", mod=m.key,
                ))
        return out

    # --------------------------------------------------------------- profiles
    def select_profile(self, pid: str) -> None:
        with self.lock:
            p = next((x for x in self.profiles if x.id == pid), None)
            if p is None:
                raise UserError("Профиль не найден")
            self.profile = p
            self.settings["profile"] = pid
            self._save_settings()
            self._recheck()

    def _require_profile(self) -> profiles.Profile:
        if self.profile is None:
            raise UserError("Профиль не выбран. Создайте профиль в игре и нажмите «Обновить».")
        return self.profile

    def _entries(self, keys: List[str]) -> List[ActiveEntry]:
        known = {e.key: e for e in (self.profile.active if self.profile else [])}
        out = []
        for k in keys:
            m = self.by_key.get(k)
            if m is not None:
                out.append(ActiveEntry(k, m.name))
            elif k in known:
                out.append(known[k])
            else:
                raise UserError(f"Неизвестный мод: {k}")
        return out

    def preview(self, keys: List[str]) -> List[checks.Issue]:
        with self.lock:
            return self._recheck(self._entries(keys))

    def auto_sort(self, keys: List[str]):
        with self.lock:
            entries = self._entries(keys)
            present = [self.by_key[e.key] for e in entries if e.key in self.by_key]
            missing = [e.key for e in entries if e.key not in self.by_key]
            sorted_mods, cycles = order.auto_sort(present, self.rules, self.overrides)
            # Missing mods stay where the game will complain loudest: at the top.
            return missing + [m.key for m in sorted_mods], cycles

    def apply(self, keys: List[str]) -> str:
        with self.lock:
            prof = self._require_profile()
            if paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её: при выходе она перезапишет профиль.")
            entries = self._entries(keys)
            if len({e.key for e in entries}) != len(entries):
                raise UserError("В списке есть повторы")
            point = self._point("apply", self._order_summary(prof, keys))
            profiles.write_order(self.state_dir, prof, entries, backup=False)
            self.history.note("apply", f"Новый порядок модов записан в профиль «{prof.name}» "
                              f"({len(entries)} шт.)", point=point.id)
            self.refresh()
            return point.id

    def _order_summary(self, prof, keys: List[str]) -> str:
        before = [e.key for e in prof.active]
        on = [k for k in keys if k not in before]
        off = [k for k in before if k not in keys]
        name = lambda k: self.by_key[k].name if k in self.by_key else k
        parts = []
        if on:
            parts.append("включены " + ", ".join(name(k) for k in on[:3]) + ("…" if len(on) > 3 else ""))
        if off:
            parts.append("выключены " + ", ".join(name(k) for k in off[:3]) + ("…" if len(off) > 3 else ""))
        if not parts:
            parts.append("изменён порядок")
        return "Порядок модов: " + "; ".join(parts)

    # --------------------------------------------------------- restore points
    def _log_snapshot(self):
        if not self.log:
            return [], 0.0
        return [l.text for l in self.log.lines if l.level == "error"], self.log.mtime

    def _point(self, reason: str, summary: str, kind: str = "auto", label: str = ""):
        prof = self.profile
        errors, mtime = self._log_snapshot()
        return self.points.create(
            reason=reason, summary=summary, kind=kind, label=label,
            profile_id=prof.id if prof else None, profile_name=prof.name if prof else "",
            profile_path=prof.sii_path if prof else None,
            mod_dir=self.layout.active.mod_dir if self.layout and self.layout.active else "",
            log_errors=errors, log_mtime=mtime,
        )

    def list_points(self) -> List[dict]:
        out = []
        for p in self.points.points():
            d = p.public()
            d["undo_count"] = len(self.points.plan(p.id))
            out.append(d)
        return out

    def save_point(self, label: str) -> str:
        with self.lock:
            label = (label or "").strip()[:120] or "Рабочее состояние"
            self._require_profile()
            p = self._point("manual", "Сохранено вами", kind="manual", label=label)
            self.history.note("point", f"Сохранена точка восстановления «{label}»", point=p.id)
            return p.id

    def delete_point(self, pid: str) -> None:
        try:
            self.points.delete(pid)
        except restore.RestoreError as exc:
            raise UserError(str(exc))

    def rollback(self, pid: str) -> dict:
        with self.lock:
            if self.layout and paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её перед возвратом.")
            try:
                target = self.points.get(pid)
            except restore.RestoreError as exc:
                raise UserError(str(exc))
            title = target.data.get("label") or target.data.get("summary") or pid
            what = f"Возврат к состоянию «{title}»" if target.manual else f"Возврат к состоянию до «{title}»"
            undo = self._point("rollback", what)
            res = self.points.rollback(pid, undo)
            if target.data.get("has_profile"):
                prof = next((p for p in profiles.list_profiles(self.layout.active.path)
                             if p.id == target.data.get("profile_id")), None) if self.layout.active else None
                if prof is None:
                    raise UserError("Профиль из этой точки больше не существует. Файлы модов возвращены.")
                shutil.copy2(target.profile_copy, prof.sii_path)
            self.history.note("rollback", f"{what}: возвращено файлов "
                              f"{res['returned']}, убрано {res['removed']}", point=undo.id)
            self.refresh()
            return res

    def ack_point_errors(self, pid: str) -> None:
        with self.lock:
            p = self.points.get(pid)
            p.data["log_ack"] = True
            p.save()
            self._recheck()

    def _after_change_issue(self) -> List[checks.Issue]:
        """New errors in game.log since the last change DeckHaul made."""
        if not self.log:
            return []
        for p in self.points.points():
            d = p.data
            if d.get("rolled_back") or d.get("kind") == "manual":
                continue
            if self.log.mtime <= d.get("t", 0):
                return []          # the game has not run since this change
            if d.get("log_ack"):
                return []
            before = set(d.get("log_errors", []))
            new = [l.text for l in self.log.lines if l.level == "error" and l.text not in before]
            if not new:
                return []
            title = d.get("summary") or "изменения"
            return [checks.Issue(
                "after_change", checks.WARNING,
                f"После изменения «{title}» в игре появились новые ошибки: {checks.plural(len(new), 'ошибка', 'ошибки', 'ошибок')}",
                "\n".join(new[:6]) + ("\n…" if len(new) > 6 else ""),
                "Если игра стала работать хуже, верните состояние до этого изменения. "
                "Если всё в порядке, скройте это предупреждение.",
                action="rollback", extra={"point": p.id, "when": d.get("t")},
            )]
        return []


    # -------------------------------------------------------------- downloads
    ANALYZE_PER_CALL = 4   # big archives are unpacked; spread the work over polls

    @property
    def download_dirs(self) -> List[str]:
        """Folders searched for downloaded mods. The system Downloads folder by default."""
        dirs = self.settings.get("download_dirs")
        if dirs is None:
            legacy = self.settings.get("downloads_dir")
            dirs = [legacy] if legacy else [installer.downloads_dir()]
        return [os.path.expanduser(d) for d in dirs]

    def _save_download_dirs(self, dirs: List[str]) -> None:
        self.settings["download_dirs"] = dirs
        self.settings.pop("downloads_dir", None)
        self._save_settings()

    def add_download_dir(self, path: str) -> None:
        with self.lock:
            path = os.path.realpath(os.path.expanduser((path or "").strip()))
            if not os.path.isdir(path):
                raise UserError("Такой папки нет")
            mod_dir = self.layout.active.mod_dir if self.layout and self.layout.active else None
            if mod_dir and os.path.realpath(mod_dir) == path:
                raise UserError("Это папка mod самой игры. Выберите папку, куда вы скачиваете моды.")
            dirs = self.download_dirs
            if path in [os.path.realpath(d) for d in dirs]:
                raise UserError("Эта папка уже в списке")
            self._save_download_dirs(dirs + [path])

    def remove_download_dir(self, path: str) -> None:
        with self.lock:
            target = os.path.realpath(os.path.expanduser(path))
            dirs = [d for d in self.download_dirs if os.path.realpath(d) != target]
            self._save_download_dirs(dirs)

    def browse(self, path: Optional[str]) -> dict:
        """List subfolders for the folder picker in the UI."""
        home = os.path.expanduser("~")
        path = os.path.realpath(os.path.expanduser(path or home))
        if not os.path.isdir(path):
            path = os.path.realpath(home)
        try:
            names = sorted((n for n in os.listdir(path)
                            if not n.startswith(".") and os.path.isdir(os.path.join(path, n))),
                           key=str.lower)
        except OSError:
            names = []
        try:
            archives = sum(1 for n in os.listdir(path) if n.lower().endswith(installer.ARCHIVE_EXTS))
        except OSError:
            archives = 0
        places = [{"title": "Домашняя папка", "path": home},
                  {"title": "Загрузки", "path": installer.downloads_dir()}]
        for base in ("/run/media", "/run/media/" + os.path.basename(home), "/media", "/Volumes"):
            try:
                for n in sorted(os.listdir(base)):
                    full = os.path.join(base, n)
                    if os.path.isdir(full) and os.path.ismount(full):
                        places.append({"title": f"Диск {n}", "path": full})
            except OSError:
                continue
        parent = os.path.dirname(path)
        return {"path": path, "parent": parent if parent != path else None, "dirs": names,
                "archives": archives, "places": places, "home": home}

    def downloads(self) -> dict:
        """Analyse new files in the download folders (cached by size and mtime)."""
        with self.lock:
            dirs = self.download_dirs
            default_dir = installer.downloads_dir()
            dismissed = set(self.settings.get("dismissed_downloads", []))
            mod_dir = self.layout.active.mod_dir if self.layout and self.layout.active else ""
            items, busy, keep = [], [], []
            budget = self.ANALYZE_PER_CALL
            pending = 0
            for folder in dirs:
                for f in installer.list_downloads(folder):
                    if f["busy"]:
                        busy.append(f["name"])
                        continue
                    fp = installer.fingerprint(f["path"], f["size"], f["mtime"])
                    if fp in dismissed:
                        continue
                    keep.append(fp)
                    cand = self.download_cache.get(fp)
                    if cand is None:
                        if budget <= 0:
                            pending += 1
                            items.append({"id": fp, "name": f["name"], "size": f["size"],
                                          "mtime": f["mtime"], "status": "pending", "error": "",
                                          "payloads": [], "folder": folder})
                            continue
                        budget -= 1
                        cand = installer.analyze(f["path"], self.staging_root)
                        self.download_cache[fp] = cand
                    self._tm_match_download(cand)
                    if mod_dir:
                        installer.compare_with_installed(cand, mod_dir, self.mods)
                    d = cand.public()
                    d["folder"] = folder
                    d["is_default_dir"] = os.path.realpath(folder) == os.path.realpath(default_dir)
                    for p in d["payloads"]:
                        comp = (p.get("mod") or {}).get("compatible") or []
                        p["compat"] = None if not comp or not self.game_version else (
                            sii_version_matches(self.game_version, comp)
                            or bool(self.full_version and sii_version_matches(self.full_version, comp))
                        )
                        mod = p.get("mod") or {}
                        p["compat_ok"] = p["compat"] is False and self._compat_mark_for(
                            mod.get("name", ""), mod.get("author", ""), mod.get("version", ""))
                    d["already"] = bool(d["payloads"]) and all(p["already"] for p in d["payloads"])
                    items.append(d)
            installer.cleanup_staging(self.staging_root, keep)
            self.download_cache = {k: v for k, v in self.download_cache.items() if k in keep}
            return {
                "home": os.path.expanduser("~"),
                "dirs": [{"path": d, "exists": os.path.isdir(d),
                          "default": os.path.realpath(d) == os.path.realpath(default_dir)} for d in dirs],
                "items": items, "busy": busy, "pending": pending,
                "tool": bool(installer.extract_tool()),
                "target": mod_dir, "game_running": bool(self.layout and paths.game_running(self.layout.game)),
            }

    def dismiss_download(self, fp: str) -> None:
        with self.lock:
            lst = self.settings.setdefault("dismissed_downloads", [])
            if fp not in lst:
                lst.append(fp)
                del lst[:-200]
            self._save_settings()

    def _insert_by_group(self, keys: List[str], new_key: str) -> List[str]:
        m = self.by_key.get(new_key)
        if m is None:
            return keys + [new_key]
        rules = self.rules
        gi = order.GROUP_INDEX[order.group_of(m, rules, self.overrides)]
        at = 0
        for i, k in enumerate(keys):
            other = self.by_key.get(k)
            if other is None or order.GROUP_INDEX[order.group_of(other, rules, self.overrides)] <= gi:
                at = i + 1
        return keys[:at] + [new_key] + keys[at:]

    def install_download(self, fp: str, payload_ids: List[int], enable: bool,
                         remove_old: bool, delete_download: bool) -> dict:
        with self.lock:
            if not self.layout or not self.layout.active:
                raise UserError("Не найдена папка игры. Запустите игру один раз и нажмите «Обновить».")
            cand = self.download_cache.get(fp)
            if cand is None:
                raise UserError("Файл изменился или пропал. Обновите список.")
            mod_dir = self.layout.active.mod_dir
            installer.compare_with_installed(cand, mod_dir, self.mods)
            point = self._point("install", f"Установка из «{cand.name}»")
            try:
                res = installer.install(cand, payload_ids, mod_dir, self.points.move_to_trash,
                                        remove_old, delete_download)
            except (installer.InstallError, OSError) as exc:
                restore.RestoreStore.record(point, [], summary=f"Неудачная установка из «{cand.name}»")
                raise UserError(str(exc))
            restore.RestoreStore.record(point, res["journal"], summary=self._install_summary(cand, payload_ids, res))
            self._remember_sources(cand, payload_ids, res)
            names = ", ".join(res["installed"])
            self.history.note("installed", f"Установлено из «{cand.name}»: {names}", point=point.id)
            self.download_cache.pop(fp, None)
            self.refresh()

            note = ""
            prof = self.profile
            if prof is not None and prof.writable:
                keys = [e.key for e in prof.active]
                before = list(keys)
                for r in res["replaced"]:
                    if r["old"] in keys:
                        if r["new"] in keys:
                            keys.remove(r["old"])
                        else:
                            keys[keys.index(r["old"])] = r["new"]
                if enable:
                    for k in res["installed"]:
                        if k not in keys:
                            keys = self._insert_by_group(keys, k)
                if keys != before:
                    if paths.game_running(self.layout.game):
                        note = "Игра запущена, поэтому профиль не изменён. Включите мод после выхода из игры."
                    else:
                        # The install point already holds the old profile.
                        profiles.write_order(self.state_dir, prof, self._entries(keys), backup=False)
                        self.refresh()
                        note = f"Профиль «{prof.name}» обновлён. Вернуть всё как было можно на вкладке «Точки восстановления»."
            return {"installed": res["installed"], "replaced": res["replaced"],
                    "trashed": len(res["trashed"]), "note": note, "point": point.id}

    def _remember_sources(self, cand, payload_ids, res) -> None:
        """Keep the "where to update" link: from the browser's download record,
        or carried over from the version this install replaces."""
        old_by_new = {r["new"]: r["old"] for r in res["replaced"]}
        for p in cand.payloads:
            if p.id not in payload_ids:
                continue
            key = os.path.splitext(p.target)[0]
            url = cand.source
            old = old_by_new.get(key)
            if not url and old and old in self._sources:
                url = self._sources[old].get("url", "")
            if url:
                m = p.mod or {}
                self._sources[key] = {"url": url, "name": m.get("name", ""), "author": m.get("author", "")}
        self._save_settings()

    def _install_summary(self, cand, payload_ids, res) -> str:
        parts = []
        for p in cand.payloads:
            if p.id not in payload_ids:
                continue
            m = p.mod or {}
            text = f"{m.get('name') or p.target} {m.get('version') or ''}".strip()
            olds = [f"{o['version']}" for o in p.replaces if o.get("version")]
            if olds:
                text += " вместо " + ", ".join(olds)
            parts.append(text)
        return "Установлен " + "; ".join(parts) if len(parts) == 1 else "Установлены " + "; ".join(parts)

    def backups(self) -> List[dict]:
        prof = self._require_profile()
        return profiles.list_backups(self.state_dir, prof)

    def restore(self, name: str) -> None:
        with self.lock:
            prof = self._require_profile()
            if paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её перед восстановлением.")
            self._point("restore", f"Возврат старой копии профиля {name}")
            profiles.restore_backup(self.state_dir, prof, name)
            self.history.note("restore", f"Профиль «{prof.name}» восстановлен из копии {name}")
            self.refresh()

    def backup_now(self) -> str:
        prof = self._require_profile()
        return os.path.basename(profiles.make_backup(self.state_dir, prof, "manual"))

    # ------------------------------------------------- confirmed compatibility
    @property
    def _compat_marks(self) -> Dict[str, dict]:
        return self.settings.setdefault("compat_ok", {})

    def compat_status(self, mod: Mod):
        """(confirmed for this game and mod version, game version of an outdated mark)."""
        mark = self._compat_marks.get(mod.key)
        if not mark:
            return False, None
        if mark.get("game") == self.game_version and mark.get("version") == mod.version:
            return True, None
        return False, mark.get("game")

    def _compat_mark_for(self, name: str, author: str, version: str) -> bool:
        """Same check for a file that is not installed yet, matched by its manifest."""
        for mark in self._compat_marks.values():
            if (mark.get("name", "").lower() == (name or "").lower()
                    and mark.get("author", "").lower() == (author or "").lower()
                    and mark.get("version") == version and mark.get("game") == self.game_version):
                return True
        return False

    def set_compat_ok(self, key: str, ok: bool) -> None:
        with self.lock:
            m = self.by_key.get(key)
            if m is None:
                raise UserError("Мод не найден")
            if ok:
                if not self.game_version:
                    raise UserError("Версия игры неизвестна. Запустите игру и нажмите «Обновить».")
                self._compat_marks[key] = {
                    "game": self.game_version, "version": m.version, "name": m.name,
                    "author": m.author, "t": time.time(),
                }
                self.history.note("compat_ok", f"«{m.name}» {m.version} отмечен как рабочий на "
                                  f"версии игры {self.game_version}", key=key)
            else:
                self._compat_marks.pop(key, None)
            self._save_settings()
            self._recheck()

    # --------------------------------------------------------- groups & rules
    def set_override(self, key: str, group: Optional[str]) -> None:
        with self.lock:
            if group and group not in order.GROUP_INDEX:
                raise UserError("Неизвестная группа")
            if group:
                self.overrides[key] = group
            else:
                self.overrides.pop(key, None)
            self._save_settings()
            self._recheck()

    def add_rule(self, above_key: str, below_key: str) -> None:
        with self.lock:
            a, b = self.by_key.get(above_key), self.by_key.get(below_key)
            if not a or not b or a is b:
                raise UserError("Выберите два разных мода")
            rules = self.rules
            rules.append(order.Rule(
                id=f"user-{int(time.time() * 1000)}",
                text=f"ваше правило: «{a.name}» выше «{b.name}»",
                above=[f"key:{a.key.lower()}"], below=[f"key:{b.key.lower()}"], source="user",
            ))
            order.save_user_rules(self.user_rules_path, rules)
            self._recheck()

    def delete_rule(self, rule_id: str) -> None:
        with self.lock:
            rules = [r for r in self.rules if not (r.source == "user" and r.id == rule_id)]
            order.save_user_rules(self.user_rules_path, rules)
            self._recheck()

    def set_game_version(self, version: Optional[str]) -> None:
        with self.lock:
            if version:
                self.settings["game_version"] = version.strip()
            else:
                self.settings.pop("game_version", None)
            self._save_settings()
            self.refresh()

    # ----------------------------------------------------------------- online
    WORKSHOP_CHECK_EVERY = 24 * 3600

    def online_check(self) -> int:
        from .workshop_online import fetch_details

        ids = [m.workshop_id for m in self.mods if m.workshop_id is not None]
        if not ids:
            return 0
        try:
            details = fetch_details(ids)
        except OSError as exc:
            raise UserError(f"Нет связи со Steam: {exc}")
        with self.lock:
            self.online = details
            self.online_checked = time.time()
            try:
                with open(self.online_path, "w", encoding="utf-8") as fh:
                    json.dump({"checked": self.online_checked, "details": details}, fh, ensure_ascii=False)
            except OSError:
                pass
            self._apply_online_titles()
            self._recheck()
        return len(details)

    def _apply_online_titles(self) -> None:
        for m in self.mods:
            info = self.online.get(m.workshop_id) if m.workshop_id else None
            if info and info.get("title") and m.name.startswith("Workshop "):
                m.name = info["title"]

    def workshop_status(self) -> dict:
        return {
            "auto": self.settings.get("auto_workshop_check", True),
            "checked": self.online_checked,
            "has_key": bool(self.settings.get("steam_api_key")),
            "count": sum(1 for m in self.mods if m.workshop_id is not None),
        }

    def workshop_auto(self) -> dict:
        """Run the daily Workshop check if it is due. Network errors stay quiet."""
        st = self.workshop_status()
        ran = False
        if st["auto"] and st["count"] and time.time() - self.online_checked > self.WORKSHOP_CHECK_EVERY:
            try:
                self.online_check()
                ran = True
            except UserError:
                pass
        st = self.workshop_status()
        st["ran"] = ran
        return st

    def set_auto_workshop_check(self, on: bool) -> None:
        self.settings["auto_workshop_check"] = bool(on)
        self._save_settings()

    # ------------------------------------------------- where mods get updates
    def set_steam_key(self, key: Optional[str]) -> None:
        key = (key or "").strip()
        if key and not re.fullmatch(r"[0-9A-Fa-f]{32}", key):
            raise UserError("Ключ Steam Web API — это 32 символа из цифр и букв A–F.")
        if key:
            self.settings["steam_api_key"] = key.upper()
        else:
            self.settings.pop("steam_api_key", None)
        self._save_settings()

    @staticmethod
    def _words(text: str) -> set:
        text = re.sub(r"\bv?\d+([._]\d+)*\b", " ", (text or "").lower())
        stop = {"ets2", "ets", "mod", "by", "for", "the", "and", "euro", "truck", "simulator", "2", "of"}
        return {w for w in re.findall(r"[\w]+", text) if len(w) > 1 and w not in stop}

    def find_in_workshop(self, key: str) -> dict:
        from .workshop_online import search

        m = self.by_key.get(key)
        if m is None:
            raise UserError("Мод не найден")
        api_key = self.settings.get("steam_api_key")
        if not api_key:
            raise UserError("Нужен ключ Steam Web API")
        query = re.sub(r"\bv?\d+([._]\d+)+\b", "", m.name).strip() or m.name
        try:
            found = search(api_key, self.layout.game.app_id, query)
        except OSError as exc:
            if "403" in str(exc) or "401" in str(exc):
                raise UserError("Steam не принял ключ. Проверьте его на steamcommunity.com/dev/apikey.")
            raise UserError(f"Нет связи со Steam: {exc}")
        mine = self._words(m.name)
        have = {x.workshop_id for x in self.mods if x.workshop_id is not None}
        for f in found:
            theirs = self._words(f["title"])
            f["score"] = round(len(mine & theirs) / len(mine | theirs), 2) if mine | theirs else 0.0
            f["installed"] = f["id"] in have
            f["url"] = f"https://steamcommunity.com/sharedfiles/filedetails/?id={f['id']}"
            f["steam_url"] = f"steam://url/CommunityFilePage/{f['id']}"
        found.sort(key=lambda f: (-f["score"], -f["subscriptions"]))
        return {"mod": m.name, "query": query, "results": found}

    @property
    def _sources(self) -> Dict[str, dict]:
        return self.settings.setdefault("mod_sources", {})

    def source_of(self, m: Mod) -> str:
        rec = self._sources.get(m.key)
        if rec:
            return rec.get("url", "")
        name, author = (m.name or "").lower(), (m.author or "").lower()
        for rec in self._sources.values():
            if name and rec.get("name", "").lower() == name and rec.get("author", "").lower() == author:
                return rec.get("url", "")
        return ""

    def set_mod_source(self, key: str, url: Optional[str]) -> None:
        with self.lock:
            m = self.by_key.get(key)
            if m is None:
                raise UserError("Мод не найден")
            url = (url or "").strip()
            if url and not re.match(r"https?://[^\s]+$", url):
                raise UserError("Ссылка должна начинаться с http:// или https://")
            if url:
                self._sources[key] = {"url": url, "name": m.name, "author": m.author}
            else:
                self._sources.pop(key, None)
            self._save_settings()

    def switch_to_workshop(self, local_key: str, ws_key: str) -> dict:
        """Use the Workshop copy instead of a local file, keeping its place in the order."""
        with self.lock:
            prof = self._require_profile()
            if paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её перед переключением.")
            loc, ws = self.by_key.get(local_key), self.by_key.get(ws_key)
            if not loc or not ws or loc.source != "local" or ws.source != "workshop":
                raise UserError("Не найдены обе копии мода")
            keys = [e.key for e in prof.active]
            if local_key in keys:
                if ws_key in keys:
                    keys.remove(local_key)
                else:
                    keys[keys.index(local_key)] = ws_key
            point = self._point("switch", f"«{loc.name}»: копия из Workshop вместо локальной {loc.version or ''}".strip())
            dst = self.points.move_to_trash(loc.path)
            restore.RestoreStore.record(point, [{"op": "moved", "src": loc.path, "dst": dst}])
            self.refresh()
            profiles.write_order(self.state_dir, self.profile, self._entries(keys), backup=False)
            self.history.note("switch", f"«{loc.name}» теперь из Workshop", point=point.id)
            self.refresh()
            return {"point": point.id}

    # ------------------------------------------------------------------ files
    def icon(self, key: str):
        m = self.by_key.get(key)
        if not m or not m.icon:
            return None
        try:
            with open_container(m.path) as c:
                data = c.read(m.icon)
        except (ArchiveError, OSError):
            return None
        if not data:
            return None
        mime = "image/png" if m.icon.lower().endswith(".png") else "image/jpeg"
        return data, mime

    def conflict_details(self, key: str, limit: int = 300) -> dict:
        """Name the overwritten files of one mod and which mod wins each of them."""
        with self.lock:
            prof = self._require_profile()
            active = [self.by_key[e.key] for e in prof.active if e.key in self.by_key]
            if key not in {m.key for m in active}:
                raise UserError("Мод не включён в профиле")
            owner = {}
            for m in active:
                for h in m.hashes:
                    owner.setdefault(h, m)
            me = self.by_key[key]
            lost = {h: owner[h] for h in me.hashes if owner[h] is not me}
        names = {}
        for m in [me] + list({id(v): v for v in lost.values()}.values()):
            try:
                with open_container(m.path) as c:
                    for h, p in c.file_hashes().items():
                        if p and h in lost:
                            names[h] = p
            except (ArchiveError, OSError):
                continue
        rows = sorted(
            ({"file": names.get(h, f"(имя скрыто автором, хеш {h:016x})"), "winner": w.name}
             for h, w in lost.items()),
            key=lambda r: r["file"],
        )
        return {"mod": me.name, "total": len(me.hashes), "lost": len(rows), "rows": rows[:limit]}

    # --------------------------------------------------------------- TruckyMods
    TM_CHECK_EVERY = 24 * 3600

    def _tm_page(self, m: Mod) -> Optional[str]:
        return truckymods.page_of(self.source_of(m)) if m.source == "local" else None

    def _tm_installed(self, url: str, title: str = "") -> Optional[Mod]:
        for m in self.mods:
            if self._tm_page(m) == url:
                return m
        t = (title or "").strip().lower()
        if t:
            for m in self.mods:
                if m.name.strip().lower() == t:
                    return m
        return None

    def _tm_card(self, c: dict) -> dict:
        m = self._tm_installed(c["url"], c.get("title", ""))
        c = dict(c)
        c["installed"] = m.key if m else None
        c["update"] = bool(m and m.key in self.tm_updates)
        return c

    def tm_list(self, kind: str, value: str = "", page: int = 1) -> dict:
        try:
            if kind == "search":
                if len((value or "").strip()) < 2:
                    raise UserError("Введите хотя бы две буквы")
                items = self.tm.search(value, page)
            elif kind == "category":
                items = self.tm.category(value, page)
            else:
                items = self.tm.trending()
        except truckymods.TruckyError as exc:
            raise UserError(str(exc))
        with self.lock:
            return {"items": [self._tm_card(c) for c in items], "page": page}

    def tm_mod(self, url: str) -> dict:
        try:
            d = dict(self.tm.mod(url))
        except truckymods.TruckyError as exc:
            raise UserError(str(exc))
        from .prelaunch import installed_dlc
        gv = self.game_version
        mm = ".".join(gv.split(".")[:2]) if gv else None
        d["game_version"] = gv
        d["compat"] = None if not (mm and d["game_versions"]) else any(
            g == mm or g.startswith(mm + ".") for g in d["game_versions"])
        best = truckymods.best_build(d, gv)
        d["best_build"] = best["build_id"] if best else None
        d["best_fits"] = bool(best and mm and any(g == mm or g.startswith(mm + ".") for g in best["game_versions"]))
        owned = installed_dlc(self.layout) if self.layout else None
        d["dlcs"] = [dict(x, installed=None if owned is None else x["app_id"] in owned) for x in d["dlcs"]]
        with self.lock:
            d["required_mods"] = [dict(r, installed=bool(self._tm_installed(r["url"], r["title"])))
                                  for r in d["required_mods"]]
            m = self._tm_installed(d["url"], d["title"])
            d["installed"] = {"key": m.key, "version": m.version, "name": m.name} if m else None
            d["update_available"] = bool(m and truckymods.is_newer(d["version"], m.version))
        return d

    def tm_download(self, url: str, build_id: Optional[int] = None) -> dict:
        """Remember what the user is about to download, return the official page to open."""
        d = self.tm_mod(url)
        build = next((b for b in d["builds"] if b["build_id"] == build_id), None) if build_id else None
        build = build or next((b for b in d["builds"] if b["build_id"] == d["best_build"]), None)
        target = (build or {}).get("download_url") or d["download_page"]
        latest = not d["builds"] or (build is not None and build["build_id"] == d["builds"][0]["build_id"])
        with self.lock:
            expect = self.settings.setdefault("tm_expect", [])
            expect[:] = [e for e in expect if time.time() - e["t"] < 6 * 3600][-19:]
            expect.append({"page": d["url"], "title": d["title"],
                           "version": (build or {}).get("version") or d["version"],
                           "size": d["size"] if latest else 0, "t": time.time()})
            self._save_settings()
        return {"open": target, "title": d["title"]}

    def _tm_match_download(self, cand) -> None:
        """Link a fresh download to the TruckyMods page it came from."""
        page = truckymods.page_of(cand.source)
        if page:
            cand.source = page
            return
        if cand.source:
            return
        pending = [e for e in self.settings.get("tm_expect", []) if cand.mtime >= e["t"] - 60]
        hit = next((e for e in pending if e.get("size") and e["size"] == cand.size), None)
        if hit is None and len(pending) == 1 and not pending[0].get("size"):
            hit = pending[0]
        if hit:
            cand.source = hit["page"]

    @property
    def tm_auto(self) -> bool:
        return self.settings.get("auto_trucky_check", True)

    def set_tm_auto(self, on: bool) -> None:
        self.settings["auto_trucky_check"] = bool(on)
        self._save_settings()

    def tm_status(self) -> dict:
        with self.lock:
            linked = sum(1 for m in self.mods if self._tm_page(m))
        return {"auto": self.tm_auto, "checked": self.settings.get("tm_checked", 0),
                "linked": linked, "updates": len(self.tm_updates), "running": self.tm_checking}

    def tm_check_updates(self, force: bool = False) -> dict:
        """Compare mods installed from TruckyMods with the site. Slow on purpose
        (polite pace), so the server runs it in a thread, at most once a day."""
        if self.tm_checking:
            return self.tm_status()
        if not force and (not self.tm_auto or time.time() - self.settings.get("tm_checked", 0) < self.TM_CHECK_EVERY):
            return self.tm_status()
        self.tm_checking = True
        try:
            with self.lock:
                linked = [(m.key, m.version, self._tm_page(m)) for m in self.mods if self._tm_page(m)]
            found: Dict[str, dict] = {}
            for key, version, page in linked[:80]:
                try:
                    d = self.tm.mod(page)
                except truckymods.TruckyError:
                    continue
                if truckymods.is_newer(d["version"], version):
                    found[key] = {"remote": d["version"], "local": version, "url": page, "title": d["title"],
                                  "game_versions": d["game_versions"]}
            with self.lock:
                self.tm_updates = found
                self.settings["tm_checked"] = time.time()
                self._save_settings()
                self._recheck()
        finally:
            self.tm_checking = False
        return self.tm_status()

    def _tm_issues(self, active_keys) -> List[checks.Issue]:
        out = []
        for key, u in self.tm_updates.items():
            m = self.by_key.get(key)
            if not m:
                continue
            for_game = (" для версии игры " + ", ".join(u["game_versions"])) if u["game_versions"] else ""
            urgent = key in active_keys and m.compatible_with(self.game_version) is False
            out.append(checks.Issue(
                "tm_update", checks.WARNING if urgent else checks.INFO,
                f"На TruckyMods вышла версия {u['remote']} мода «{m.name}»",
                f"У вас {u['local'] or 'версия без номера'}. Новая версия{for_game}.",
                "Нажмите «Скачать на TruckyMods»: откроется страница скачивания, а когда файл скачается, "
                "DeckHaul предложит поставить его вместо старой версии.",
                mod=key, action="tm_download", extra={"url": u["url"]},
            ))
        return out

    # --------------------------------------------------------------- mod sets
    @property
    def _sets(self) -> List[dict]:
        return self.settings.setdefault("mod_sets", [])

    def _set_entry(self, key: str, label: str) -> dict:
        m = self.by_key.get(key)
        return {"key": key, "label": label or (m.name if m else key),
                "name": m.name if m else label, "author": m.author if m else "",
                "version": m.version if m else "", "workshop_id": m.workshop_id if m else None}

    def _resolve(self, entry: dict) -> Optional[str]:
        """Find an installed mod for a set entry: same key, else same name and author."""
        if entry.get("key") in self.by_key:
            return entry["key"]
        wid = entry.get("workshop_id")
        if wid:
            from .mods import workshop_key
            if workshop_key(int(wid)) in self.by_key:
                return workshop_key(int(wid))
        name, author = (entry.get("name") or "").lower(), (entry.get("author") or "").lower()
        for m in self.mods:
            if name and m.name.lower() == name and (m.author or "").lower() == author:
                return m.key
        return None

    def _set_public(self, s: dict) -> dict:
        found = [self._resolve(e) for e in s["entries"]]
        return {"id": s["id"], "name": s["name"], "t": s["t"], "count": len(s["entries"]),
                "missing": [{"name": e.get("name") or e.get("label") or e["key"], "version": e.get("version"),
                             "workshop_id": e.get("workshop_id")}
                            for e, k in zip(s["entries"], found) if k is None]}

    def list_sets(self) -> List[dict]:
        return [self._set_public(s) for s in self._sets]

    def save_set(self, name: str) -> dict:
        with self.lock:
            prof = self._require_profile()
            name = (name or "").strip()[:80]
            if not name:
                raise UserError("Назовите набор")
            entries = [self._set_entry(e.key, e.label) for e in prof.active]
            existing = next((s for s in self._sets if s["name"].lower() == name.lower()), None)
            if existing:
                existing.update(entries=entries, t=time.time())
            else:
                self._sets.append({"id": f"set-{int(time.time() * 1000)}", "name": name,
                                   "t": time.time(), "entries": entries})
            self._save_settings()
            return {"sets": self.list_sets()}

    def apply_set(self, sid: str) -> dict:
        with self.lock:
            s = next((x for x in self._sets if x["id"] == sid), None)
            if s is None:
                raise UserError("Набор не найден")
            keys, missing = [], []
            for e in s["entries"]:
                k = self._resolve(e)
                if k is None:
                    missing.append(e.get("name") or e.get("label") or e["key"])
                elif k not in keys:
                    keys.append(k)
            prof = self._require_profile()
            if paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её перед сменой набора.")
            point = self._point("set", f"Включён набор «{s['name']}»")
            profiles.write_order(self.state_dir, prof, self._entries(keys), backup=False)
            self.history.note("set", f"Включён набор «{s['name']}» в профиле «{prof.name}»", point=point.id)
            self.refresh()
            return {"applied": len(keys), "missing": missing}

    def delete_set(self, sid: str) -> None:
        with self.lock:
            self.settings["mod_sets"] = [s for s in self._sets if s["id"] != sid]
            self._save_settings()

    def export_set(self, sid: str) -> dict:
        s = next((x for x in self._sets if x["id"] == sid), None)
        if s is None:
            raise UserError("Набор не найден")
        return {"deckhaul_set": 1, "game": self.game, "name": s["name"], "mods": s["entries"]}

    def import_set(self, data: dict) -> dict:
        with self.lock:
            if not isinstance(data, dict) or data.get("deckhaul_set") != 1 or not isinstance(data.get("mods"), list):
                raise UserError("Это не файл набора DeckHaul")
            if data.get("game") and data["game"] != self.game:
                raise UserError("Этот набор для другой игры")
            entries = []
            for e in data["mods"][:2000]:
                if isinstance(e, dict) and isinstance(e.get("key"), str):
                    wid = e.get("workshop_id")
                    entries.append({"key": e["key"][:200], "label": str(e.get("label", ""))[:200],
                                    "name": str(e.get("name", ""))[:200], "author": str(e.get("author", ""))[:200],
                                    "version": str(e.get("version", ""))[:50],
                                    "workshop_id": int(wid) if isinstance(wid, int) or str(wid).isdigit() else None})
            if not entries:
                raise UserError("В наборе нет модов")
            name = (str(data.get("name") or "Набор из файла"))[:80]
            s = {"id": f"set-{int(time.time() * 1000)}", "name": name, "t": time.time(), "entries": entries}
            self._sets.append(s)
            self._save_settings()
            return self._set_public(s)

    # ------------------------------------------------------------ disk space
    def disk_usage(self) -> dict:
        with self.lock:
            used_anywhere = {e.key for p in self.profiles for e in p.active}
            local = sum(m.size for m in self.mods if m.source == "local")
            workshop = sum(m.size for m in self.mods if m.source == "workshop")
            unused = [m for m in self.mods if m.key not in used_anywhere]
            free = None
            if self.layout and self.layout.active:
                try:
                    free = shutil.disk_usage(self.layout.active.path).free
                except OSError:
                    pass
            return {
                "local": local, "workshop": workshop, "free": free,
                "trash": self.points.trash_size(),
                "unused": [{"key": m.key, "name": m.name, "size": m.size, "source": m.source,
                            "workshop_id": m.workshop_id} for m in sorted(unused, key=lambda m: -m.size)],
            }

    def remove_unused(self, keys: List[str]) -> dict:
        with self.lock:
            used_anywhere = {e.key for p in self.profiles for e in p.active}
            targets = [self.by_key[k] for k in keys if k in self.by_key]
            targets = [m for m in targets if m.source == "local" and m.key not in used_anywhere]
            if not targets:
                raise UserError("Нечего убирать: выбранные моды включены в каком-то профиле или из Workshop")
            point = self._point("cleanup", f"Убраны неиспользуемые моды: {len(targets)}")
            journal = []
            for m in targets:
                journal.append({"op": "moved", "src": m.path, "dst": self.points.move_to_trash(m.path)})
            restore.RestoreStore.record(point, journal)
            self.history.note("cleanup", f"В корзину убрано неиспользуемых модов: {len(targets)}", point=point.id)
            self.refresh()
            return {"removed": len(targets), "size": sum(m.size for m in targets)}

    def empty_trash(self) -> int:
        with self.lock:
            freed = self.points.empty_trash()
            self.history.note("trash", f"Корзина DeckHaul очищена, освобождено {freed // (1 << 20)} МБ")
            return freed

    # -------------------------------------------------------- prelaunch check
    def _remember_buildid(self) -> None:
        from .prelaunch import game_buildid
        bid = game_buildid(self.layout) if self.layout else None
        if bid and self.settings.get("seen_buildid") != bid:
            self.settings["seen_buildid"] = bid
            self._save_settings()

    def prelaunch_status(self) -> dict:
        from . import prelaunch
        app_id = self.layout.game.app_id if self.layout else paths.GAMES[self.game].app_id
        return {"option": prelaunch.launch_option(), "configured": prelaunch.configured(app_id),
                "problems": prelaunch.quick_check(self.game, self.state_dir) if self.layout else []}

    # ----------------------------------------------------------------- report
    def save_report(self) -> str:
        from . import report
        path = report.save(self, installer.downloads_dir())
        self.history.note("report", "Сохранён отчёт о проблеме: " + os.path.basename(path))
        home = os.path.expanduser("~")
        return path.replace(home, "~", 1) if path.startswith(home) else path

    # ----------------------------------------------------------------- update
    @property
    def auto_update_check(self) -> bool:
        return self.settings.get("auto_update_check", True)

    def set_auto_update_check(self, on: bool) -> None:
        self.settings["auto_update_check"] = bool(on)
        self._save_settings()

    def update_status(self, force: bool = False) -> dict:
        if force or self.auto_update_check:
            st = self.updater.check(force=force)
        else:
            st = self.updater.status()
        st["auto"] = self.auto_update_check
        return st

    def apply_update(self) -> str:
        with self.lock:
            st = self.updater.status()
            if not st["available"]:
                raise UserError("Обновлений нет")
            try:
                tag = self.updater.apply(st["latest"])
            except updater.UpdateError as exc:
                raise UserError(str(exc))
            self.history.note("app_update", f"DeckHaul обновлён: {st['current']} → {tag.lstrip('v')}")
            return tag

    # --------------------------------------------------------------- snapshot
    def snapshot(self) -> dict:
        with self.lock:
            prof = self.profile
            active_keys = [e.key for e in prof.active] if prof else []
            labels = {e.key: e.label for e in prof.active} if prof else {}
            rules = self.rules
            mods = []
            for m in self.mods:
                d = m.public()
                d["group"] = order.group_of(m, rules, self.overrides)
                d["override"] = self.overrides.get(m.key)
                d["compat"] = m.compatible_with(self.game_version, self.full_version)
                d["compat_ok"], d["compat_ok_before"] = self.compat_status(m)
                d["source_url"] = self.source_of(m) if m.source == "local" else ""
                d["needs_update"] = d["compat"] is False and not d["compat_ok"]
                mods.append(d)
            for k in active_keys:
                if k not in self.by_key:
                    mods.append({"key": k, "name": labels.get(k) or k, "missing": True,
                                 "source": "workshop" if k.startswith("mod_workshop_package.") else "local",
                                 "group": "top"})
            return {
                "app": {"version": __version__, "scanned_at": self.scanned_at},
                "scan": dict(self.scan_state),
                "layout": self.layout.to_dict() if self.layout else None,
                "game_version": self.game_version,
                "game_version_full": self.full_version,
                "game_version_manual": bool(self.settings.get("game_version")),
                "game_running": bool(self.layout and paths.game_running(self.layout.game)),
                "profiles": [p.public() for p in self.profiles],
                "profile": prof.public() if prof else None,
                "active": active_keys,
                "mods": mods,
                "issues": [i.to_dict() for i in self.issues],
                "summary": checks.summary(self.issues),
                "groups": checks.group_titles(),
                "rules": [r.to_dict() for r in rules],
                "events": self.history.events[:100],
                "new_events": self.new_events,
                "log_errors": [l.__dict__ for l in (self.log.lines if self.log else [])][:50],
            }
