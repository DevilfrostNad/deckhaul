"""The application service: one object the CLI and the web UI both talk to."""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Dict, List, Optional

from . import checks, order, paths, profiles
from .archive import ArchiveError, open_container
from .gamelog import read_game_log
from .history import History
from .mods import Mod, ScanCache, scan
from .profiles import ActiveEntry


class UserError(Exception):
    """Something the user can fix; the message is shown as is."""


class App:
    def __init__(self, game: str = "ets2", data_dir: Optional[str] = None, state_dir: Optional[str] = None):
        self.lock = threading.RLock()
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
        with self.lock:
            self.layout = paths.discover(self.game, self.data_dir_override)
            act = self.layout.active
            self.log = read_game_log(act.path) if act else None
            res = scan(
                act.mod_dir if act else "", self.layout.workshop_dirs, self.cache,
                self.game_version, self.full_version, self.layout.workshop_acf,
            )
            self.mods, self.junk, self.workshop_state = res.mods, res.junk_files, res.workshop_state
            self.by_key = {}
            for m in self.mods:
                self.by_key.setdefault(m.key, m)
            self.profiles = profiles.list_profiles(act.path) if act else []
            want = self.settings.get("profile")
            self.profile = next((p for p in self.profiles if p.id == want), None) or (
                self.profiles[0] if self.profiles else None
            )
            self.new_events = self.history.update(self.mods, self.full_version or self.game_version)
            self._recheck()
            self.scanned_at = time.time()

    def _recheck(self, active: Optional[List[ActiveEntry]] = None) -> List[checks.Issue]:
        current = active is None
        if current:
            active = self.profile.active if self.profile else []
        issues = checks.check_all(
            self.layout, self.mods, self.junk, active, self.game_version, self.full_version,
            self.log, self.rules, self.overrides, self.workshop_state,
        )
        issues += self._online_issues(active)
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
            backup = profiles.write_order(self.state_dir, prof, entries)
            self.history.note("apply", f"Новый порядок модов записан в профиль «{prof.name}» "
                              f"({len(entries)} шт.)", backup=os.path.basename(backup))
            self.refresh()
            return backup

    def backups(self) -> List[dict]:
        prof = self._require_profile()
        return profiles.list_backups(self.state_dir, prof)

    def restore(self, name: str) -> None:
        with self.lock:
            prof = self._require_profile()
            if paths.game_running(self.layout.game):
                raise UserError("Игра запущена. Закройте её перед восстановлением.")
            profiles.restore_backup(self.state_dir, prof, name)
            self.history.note("restore", f"Профиль «{prof.name}» восстановлен из копии {name}")
            self.refresh()

    def backup_now(self) -> str:
        prof = self._require_profile()
        return os.path.basename(profiles.make_backup(self.state_dir, prof, "manual"))

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
            for m in self.mods:
                info = details.get(m.workshop_id) if m.workshop_id else None
                if info and info.get("title") and m.name.startswith("Workshop "):
                    m.name = info["title"]
            self._recheck()
        return len(details)

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
                mods.append(d)
            for k in active_keys:
                if k not in self.by_key:
                    mods.append({"key": k, "name": labels.get(k) or k, "missing": True,
                                 "source": "workshop" if k.startswith("mod_workshop_package.") else "local",
                                 "group": "top"})
            return {
                "app": {"version": "1.0.0", "scanned_at": self.scanned_at},
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
