"""DeckHaul command line.

  python3 -m deckhaul            открыть интерфейс в браузере
  python3 -m deckhaul check      отчёт о проблемах в терминале
  python3 -m deckhaul sort       показать предлагаемый порядок (--apply, чтобы записать)
  python3 -m deckhaul profiles   список профилей
  python3 -m deckhaul backups    резервные копии профиля
  python3 -m deckhaul restore N  восстановить профиль из копии N
  python3 -m deckhaul downloads  что из «Загрузок» можно установить
  python3 -m deckhaul install N  установить файл номер N из списка downloads
  python3 -m deckhaul folders    папки, где искать скачанные моды
  python3 -m deckhaul update     проверить новую версию DeckHaul и установить её
  python3 -m deckhaul folder-add ПУТЬ / folder-remove ПУТЬ
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from .core import App, UserError

_MARK = {"error": "✖ ОШИБКА  ", "warning": "▲ ВНИМАНИЕ", "info": "• заметка "}


def _print_issues(issues) -> None:
    if not issues:
        print("Проблем не найдено.")
        return
    for i in issues:
        print(f"{_MARK[i.severity]} {i.title}")
        if i.detail:
            for line in i.detail.splitlines():
                print("            " + line)
        if i.fix:
            print("            Что сделать: " + i.fix)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="deckhaul", description="Менеджер модов ETS2/ATS для Steam Deck")
    ap.add_argument("command", nargs="?", default="serve",
                    choices=["serve", "check", "sort", "profiles", "backups", "restore", "downloads", "install", "folders", "folder-add", "folder-remove", "update"])
    ap.add_argument("arg", nargs="?")
    ap.add_argument("--game", default="ets2", choices=["ets2", "ats"])
    ap.add_argument("--data-dir", help="папка игры с profiles/ и mod/, если не нашлась сама")
    ap.add_argument("--profile", help="id профиля (см. команду profiles)")
    ap.add_argument("--apply", action="store_true", help="для sort: записать порядок в профиль")
    ap.add_argument("--json", action="store_true", help="вывод в JSON")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--idle-exit", type=int, default=0,
                    help="закрыться, если интерфейс не открыт столько секунд")
    a = ap.parse_args(argv)

    app = App(a.game, a.data_dir)
    try:
        app.refresh()
        if a.profile:
            app.select_profile(a.profile)

        if a.command == "serve":
            from .server import serve
            serve(app, port=a.port, open_browser=not a.no_browser, idle_exit=a.idle_exit)
            return 0

        if a.command == "profiles":
            for p in app.profiles:
                mark = "*" if app.profile and p.id == app.profile.id else " "
                print(f"{mark} {p.id:40} {p.name}  ({len(p.active)} модов, {p.storage})")
            return 0

        if a.command == "check":
            if a.json:
                print(json.dumps(app.snapshot(), ensure_ascii=False, indent=2))
                return 0
            lay = app.layout
            print(f"Игра: {lay.game.title}, версия {app.full_version or app.game_version or 'неизвестна'}")
            print(f"Папка игры: {lay.active.path if lay.active else 'не найдена'}")
            if app.profile:
                print(f"Профиль: {app.profile.name} — включено модов: {len(app.profile.active)}")
            print(f"Найдено модов: {len(app.mods)}\n")
            for ev in app.new_events:
                print("Изменение: " + ev["text"])
            if app.new_events:
                print()
            _print_issues(app.issues)
            return 1 if any(i.severity == "error" for i in app.issues) else 0

        if a.command == "sort":
            keys = [e.key for e in app.profile.active] if app.profile else []
            new, cycles = app.auto_sort(keys)
            names = {m.key: m.name for m in app.mods}
            for n, k in enumerate(new, 1):
                moved = "" if n - 1 < len(keys) and keys[n - 1] == k else "  ← перемещён"
                print(f"{n:3}. {names.get(k, k)}{moved}")
            if cycles:
                print("\nПравила противоречат друг другу для: " + ", ".join(cycles))
            if a.apply:
                backup = app.apply(new)
                print(f"\nПорядок записан. Копия старого профиля: {backup}")
            elif new != keys:
                print("\nЧтобы записать этот порядок, добавьте --apply")
            return 0

        if a.command == "update":
            st = app.update_status(force=True)
            if st["error"]:
                print(st["error"])
                return 2
            print(f"Установлена версия {st['current']}, последняя — {(st['latest'] or '?').lstrip('v')}")
            if not st["available"]:
                print("Обновлений нет.")
                return 0
            if st["notes"]:
                print("\n" + st["notes"] + "\n")
            if not st["can_apply"]:
                print("Эта копия запущена не из установленной папки. Обновите её командой git pull.")
                return 0
            print("Устанавливаю " + app.apply_update() + "…")
            print("Готово. Перезапустите DeckHaul, если он открыт.")
            return 0

        if a.command in ("folders", "folder-add", "folder-remove"):
            if a.command != "folders":
                if not a.arg:
                    print("Укажите путь к папке")
                    return 2
                if a.command == "folder-add":
                    app.add_download_dir(a.arg)
                else:
                    app.remove_download_dir(os.path.realpath(os.path.expanduser(a.arg)))
            for d in app.download_dirs:
                print(d)
            return 0

        if a.command in ("downloads", "install"):
            d = app.downloads()
            while d["pending"]:
                d = app.downloads()
            print("Ищу в: " + ", ".join(x["path"] for x in d["dirs"]))
            if d["busy"]:
                print("Ещё скачиваются: " + ", ".join(d["busy"]))
            if a.command == "downloads":
                if not d["items"]:
                    print("Новых модов нет.")
                for n, c in enumerate(d["items"], 1):
                    print(f"{n:3}. {c['name']}")
                    if c["error"]:
                        print("       " + c["error"])
                    for p in c["payloads"]:
                        m = p.get("mod") or {}
                        line = f"       → {p['target']}: {m.get('name', '?')} {m.get('version') or ''}".rstrip()
                        if p.get("compat") is False:
                            line += f" (не для версии {app.game_version})"
                        for r in p["replaces"]:
                            line += f"; заменит {r['name']} {r['version'] or ''}".rstrip()
                        print(line)
                if d["items"]:
                    print("\nУстановить: deckhaul install НОМЕР")
                return 0
            try:
                c = d["items"][int(a.arg) - 1]
            except (TypeError, ValueError, IndexError):
                print("Укажите номер из списка: deckhaul downloads")
                return 2
            if c["status"] != "ready":
                print(f"Этот файл установить нельзя: {c['error']}")
                return 2
            ids = [p["id"] for p in c["payloads"] if p["installable"]]
            r = app.install_download(c["id"], ids, enable=True, remove_old=True, delete_download=False)
            print("Установлено: " + ", ".join(r["installed"]))
            if r["note"]:
                print(r["note"])
            return 0

        if a.command == "backups":
            for b in app.backups():
                print(b["name"])
            return 0

        if a.command == "restore":
            if not a.arg:
                print("Укажите имя копии: deckhaul restore ИМЯ")
                return 2
            app.restore(a.arg)
            print("Профиль восстановлен.")
            return 0
    except UserError as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
