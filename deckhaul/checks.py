"""All the checks that turn a scan + a profile into a list of human-readable issues."""

from __future__ import annotations

import os
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .gamelog import GameLog
from .mods import Mod, parse_workshop_key
from .order import GROUPS, analyse
from .paths import Layout
from .profiles import ActiveEntry

ERROR, WARNING, INFO = "error", "warning", "info"
_SEV_RANK = {ERROR: 0, WARNING: 1, INFO: 2}


@dataclass
class Issue:
    code: str
    severity: str
    title: str
    detail: str = ""
    fix: str = ""
    mod: Optional[str] = None
    action: Optional[str] = None      # an action the UI can run in one tap
    extra: dict = field(default_factory=dict)

    def to_dict(self):
        return self.__dict__.copy()


def plural(n: int, one: str, few: str, many: str) -> str:
    n10, n100 = n % 10, n % 100
    if n10 == 1 and n100 != 11:
        return f"{n} {one}"
    if 2 <= n10 <= 4 and not 12 <= n100 <= 14:
        return f"{n} {few}"
    return f"{n} {many}"


def _label(m: Optional[Mod], entry: Optional[ActiveEntry] = None) -> str:
    if m is not None:
        return m.name
    if entry is not None:
        return entry.label or entry.key
    return "?"


def check_all(
    layout: Layout,
    mods: List[Mod],
    junk: List[str],
    active: List[ActiveEntry],
    game_version: Optional[str],
    full_version: Optional[str],
    log: Optional[GameLog],
    rules,
    overrides: Dict[str, str],
    workshop_state: Dict[int, dict],
) -> List[Issue]:
    issues: List[Issue] = []
    by_key: Dict[str, Mod] = {}
    for m in mods:
        by_key.setdefault(m.key, m)

    issues += _check_setup(layout, game_version)

    # --- the active list itself
    seen = set()
    active_mods: List[Mod] = []
    for e in active:
        if e.key in seen:
            issues.append(Issue(
                "duplicate_active", WARNING, f"«{e.label or e.key}» включён дважды",
                "Мод записан в список активных два раза. Игра загрузит его один раз, но порядок "
                "становится непредсказуемым.", "Уберите повтор: примените порядок из DeckHaul.",
                mod=e.key, action="dedupe",
            ))
            continue
        seen.add(e.key)
        m = by_key.get(e.key)
        if m is None:
            wid = parse_workshop_key(e.key)
            if wid is not None:
                fix = ("Подпишитесь на мод в Steam Workshop заново или уберите его из списка. "
                       f"Страница мода: steamcommunity.com/sharedfiles/filedetails/?id={wid}")
            else:
                fix = "Верните файл мода в папку mod или уберите его из списка."
            issues.append(Issue(
                "missing", ERROR, f"Включённого мода нет на диске: «{e.label or e.key}»",
                "Игра покажет ошибку при загрузке профиля или тихо пропустит мод, "
                "а сохранения, сделанные с ним, могут не открыться.",
                fix, mod=e.key, action="remove_missing", extra={"workshop_id": wid},
            ))
            continue
        active_mods.append(m)

    # --- per mod
    active_keys = {m.key for m in active_mods}
    for m in mods:
        is_active = m.key in active_keys
        for code, text in m.scan_problems:
            sev = ERROR if (is_active or code in ("broken_archive", "nested_archive", "nested_folder")) else WARNING
            if code == "unknown_layout":
                sev = WARNING
            issues.append(Issue(code, sev, f"«{m.name}»: {text}", fix=_FIXES.get(code, ""), mod=m.key))
        comp = m.compatible_with(game_version, full_version)
        if comp is False:
            issues.append(Issue(
                "incompatible", ERROR if is_active else INFO,
                f"«{m.name}» не рассчитан на версию игры {game_version}",
                "Автор указал совместимость: " + ", ".join(m.compatible) +
                ". Устаревшие моды — частая причина вылетов после обновления игры.",
                "Обновите мод. Если обновления нет, отключите его или проверьте в игре на "
                "отдельном профиле." if is_active else "",
                mod=m.key,
            ))
        if is_active and not m.has_manifest and m.source == "local" and not m.scan_problems:
            issues.append(Issue(
                "no_manifest", INFO, f"«{m.name}»: нет manifest.sii",
                "Мод без описания: версия и совместимость неизвестны, проверить их нельзя.",
                mod=m.key,
            ))

    issues += _check_duplicates(mods, active_keys)
    issues += _check_workshop(mods, workshop_state, active_keys)

    for path in junk:
        issues.append(Issue(
            "junk_archive", WARNING, f"Игра не читает файл «{os.path.basename(path)}»",
            "В папке mod лежит архив RAR, 7z или tar. Игра загружает только .scs, .zip и папки.",
            "Распакуйте архив и положите в папку mod файлы .scs из него, а сам архив удалите.",
            extra={"path": path},
        ))

    # --- order
    for p in analyse(active_mods, rules, overrides):
        if p.problem:
            m = by_key[p.key]
            issues.append(Issue(
                "order", WARNING, f"«{m.name}» стоит не на своём месте",
                p.problem[0].upper() + p.problem[1:] + ".",
                "Кнопка «Упорядочить» расставит моды по правилам, затем порядок нужно сохранить.", mod=m.key, action="autosort",
            ))

    issues += _check_conflicts(active_mods)
    issues += _check_log(log, by_key, game_version)

    issues.sort(key=lambda i: (_SEV_RANK[i.severity], i.code))
    return issues


_FIXES = {
    "broken_archive": "Скачайте мод заново. Если это Workshop, отпишитесь и подпишитесь снова.",
    "nested_archive": "Распакуйте архив и положите вложенные .scs в папку mod.",
    "nested_folder": "Перепакуйте мод без лишней папки или распакуйте его папкой в mod.",
    "manifest_broken": "Сообщите автору мода или скачайте другую версию.",
    "unknown_layout": "Проверьте, что это действительно мод для ETS2, а не архив с инструкцией.",
    "workshop_empty": "В Steam: свойства игры → Установленные файлы → Проверить целостность.",
}


def _check_setup(layout: Layout, game_version: Optional[str]) -> List[Issue]:
    out: List[Issue] = []
    if layout.active is None:
        out.append(Issue(
            "no_data_dir", ERROR, "Не найдена папка с профилями игры",
            "Запустите игру хотя бы один раз, чтобы она создала свои папки.",
        ))
        return out
    if not game_version:
        out.append(Issue(
            "no_version", WARNING, "Версия игры неизвестна",
            "Не найден game.log.txt, поэтому совместимость модов проверить нельзя.",
            "Запустите игру и выйдите в главное меню, затем нажмите «Обновить».",
        ))
    # Mods dropped into the folder the game is not using: a classic on the Deck.
    for d in layout.data_dirs:
        if d is layout.active:
            continue
        try:
            n = len([f for f in os.listdir(d.mod_dir) if not f.startswith(".")])
        except OSError:
            n = 0
        if n:
            where = "Proton" if d.flavor == "proton" else "Linux-версии"
            out.append(Issue(
                "wrong_mod_dir", WARNING,
                f"{plural(n, 'мод лежит', 'мода лежат', 'модов лежат')} в папке {where}, которую игра сейчас не использует",
                f"Папка: {d.mod_dir}. Игра запускается "
                f"{'через Proton' if layout.active.flavor == 'proton' else 'в Linux-версии'} "
                f"и читает моды из {layout.active.mod_dir}.",
                "Перенесите моды в рабочую папку или поменяйте способ запуска игры в Steam.",
                extra={"path": d.mod_dir},
            ))
    return out


def _check_duplicates(mods: List[Mod], active_keys) -> List[Issue]:
    out: List[Issue] = []
    groups = defaultdict(list)
    for m in mods:
        if m.has_manifest and m.name:
            groups[(m.name.strip().lower(), (m.author or "").strip().lower())].append(m)
    for (_name, _a), items in groups.items():
        if len(items) < 2:
            continue
        on = [m for m in items if m.key in active_keys]
        vers = ", ".join(f"{m.version or '?'} ({'Workshop' if m.source == 'workshop' else m.key})" for m in items)
        if len(on) > 1:
            out.append(Issue(
                "duplicate_enabled", ERROR, f"«{items[0].name}» включён в нескольких копиях",
                f"Найдены версии: {vers}. Две копии одного мода перезаписывают друг друга, "
                "и работает не та, которую вы ждёте.",
                "Оставьте включённой только самую новую копию.", mod=on[0].key,
            ))
        else:
            out.append(Issue(
                "duplicate_installed", INFO, f"«{items[0].name}» установлен несколько раз",
                f"Найдены версии: {vers}.", "Старые копии можно удалить, чтобы не перепутать.",
                mod=items[0].key,
            ))
    return out


def _check_workshop(mods: List[Mod], state: Dict[int, dict], active_keys) -> List[Issue]:
    out: List[Issue] = []
    by_id = {m.workshop_id: m for m in mods if m.workshop_id is not None}
    for wid, info in state.items():
        inst = info.get("installed") or {}
        det = info.get("details") or {}
        m = by_id.get(wid)
        pending = False
        for a, b in (("manifest", "latest_manifest"), ("timeupdated", "latest_timeupdated")):
            if det.get(b) and det.get(a) and det.get(a) != det.get(b):
                pending = True
        if inst.get("timeupdated") and det.get("timeupdated"):
            try:
                pending = pending or int(det["timeupdated"]) > int(inst["timeupdated"])
            except ValueError:
                pass
        if pending:
            name = m.name if m else f"Workshop {wid}"
            out.append(Issue(
                "workshop_pending", WARNING if (m and m.key in active_keys) else INFO,
                f"Steam ещё не скачал обновление «{name}»",
                "На диске старая версия мода, новая уже вышла.",
                "Откройте Steam в режиме рабочего стола и дождитесь загрузки, "
                "или переключите режим «Загрузки» на «Всегда обновлять».",
                mod=m.key if m else None,
            ))
    return out


def _check_conflicts(active_mods: List[Mod]) -> List[Issue]:
    """Higher mod wins each overlapping file. Flag mods that lose everything."""
    out: List[Issue] = []
    owner: Dict[int, int] = {}
    for idx, m in enumerate(active_mods):
        for h in m.hashes:
            owner.setdefault(h, idx)
    for idx, m in enumerate(active_mods):
        if not m.hashes:
            continue
        lost = defaultdict(int)
        for h in m.hashes:
            o = owner[h]
            if o != idx:
                lost[o] += 1
        if not lost:
            continue
        total_lost = sum(lost.values())
        winners = sorted(lost.items(), key=lambda kv: -kv[1])
        win_names = ", ".join(f"«{active_mods[o].name}» ({n})" for o, n in winners[:4])
        both_maps = all(active_mods[o].category == "map" for o, _ in winners) and m.category == "map"
        if total_lost == len(m.hashes):
            sev, title = (INFO if both_maps else WARNING), f"«{m.name}» полностью перекрыт другими модами"
            detail = ("Все его файлы заменяют моды выше по списку: " + win_names +
                      ". В игре он ничего не меняет.")
            fix = "Если мод нужен, поднимите его выше тех, что его перекрывают, или отключите лишний."
        else:
            sev, title = INFO, f"«{m.name}»: {total_lost} из {len(m.hashes)} файлов заменены"
            detail = "Эти файлы берутся из модов выше по списку: " + win_names + "."
            fix = ("Часто это нормально. Если мод работает не так, как задумано, "
                   "поднимите его выше.")
        out.append(Issue(
            "conflict", sev, title, detail, fix, mod=m.key,
            extra={"winners": [active_mods[o].key for o, _ in winners]},
        ))
    return out


def _check_log(log: Optional[GameLog], by_key: Dict[str, Mod], game_version) -> List[Issue]:
    if log is None:
        return []
    out: List[Issue] = []
    errs = [l for l in log.lines if l.level == "error"]
    if not errs:
        return out
    sample = "\n".join(l.text for l in errs[:8])
    out.append(Issue(
        "log_errors", WARNING, f"При последнем запуске игра записала {plural(len(errs), 'ошибку', 'ошибки', 'ошибок')}",
        sample + ("\n…" if len(errs) > 8 else ""),
        "Ошибки с упоминанием def/, vehicle/ или map/ обычно вызваны модом, который "
        "указан в той же строке. Отключите его и проверьте снова.",
        extra={"log": log.path},
    ))
    return out


def summary(issues: List[Issue]) -> dict:
    s = {ERROR: 0, WARNING: 0, INFO: 0}
    for i in issues:
        s[i.severity] += 1
    return s


def group_titles():
    return [{"id": g[0], "title": g[1]} for g in GROUPS]
