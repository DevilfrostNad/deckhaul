"""Local web UI server. Listens on 127.0.0.1 only."""

from __future__ import annotations

import json
import mimetypes
import os
import sys
import threading
import time
import traceback
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .core import App, UserError
from .sii import SiiError

WEB = os.path.join(os.path.dirname(__file__), "web")


def _make_handler(app: App, port: int, state: dict):
    allowed_hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}

    class Handler(BaseHTTPRequestHandler):
        server_version = "DeckHaul"

        def log_message(self, fmt, *args):  # keep the terminal quiet
            pass

        # -------------------------------------------------------- helpers
        def _send(self, code: int, body: bytes, ctype: str, extra=None):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, obj, code=200):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8")

        def _guard(self) -> bool:
            # Blocks DNS-rebinding and cross-site requests from other pages.
            if self.headers.get("Host") not in allowed_hosts:
                self._send(403, b"forbidden", "text/plain")
                return False
            return True

        # ------------------------------------------------------------ GET
        def do_GET(self):
            if not self._guard():
                return
            state["last_seen"] = time.time()
            url = urlparse(self.path)
            if url.path == "/api/state":
                return self._json(app.snapshot())
            if url.path == "/api/ping":
                return self._json({"ok": True})
            if url.path == "/api/icon":
                key = parse_qs(url.query).get("key", [""])[0]
                res = app.icon(key)
                if not res:
                    return self._send(404, b"", "text/plain")
                data, mime = res
                return self._send(200, data, mime, {"Cache-Control": "max-age=3600"})
            if url.path == "/api/points":
                return self._safe(lambda b: {"points": app.list_points()}, {})
            if url.path == "/api/backups":
                return self._safe(lambda b: {"backups": app.backups()}, {})
            if url.path == "/api/downloads":
                return self._safe(lambda b: app.downloads(), {})
            if url.path == "/api/browse":
                path = parse_qs(url.query).get("path", [""])[0]
                return self._safe(lambda b: app.browse(path or None), {})
            if url.path == "/api/workshop/auto":
                return self._safe(lambda b: app.workshop_auto(), {})
            if url.path == "/api/update":
                return self._safe(lambda b: app.update_status(), {})
            if url.path == "/api/conflicts":
                key = parse_qs(url.query).get("key", [""])[0]
                return self._safe(lambda b: app.conflict_details(key), {})
            return self._static(url.path)

        def _static(self, path: str):
            if path in ("", "/"):
                path = "/index.html"
            full = os.path.realpath(os.path.join(WEB, path.lstrip("/")))
            if not full.startswith(os.path.realpath(WEB) + os.sep) or not os.path.isfile(full):
                return self._send(404, b"not found", "text/plain")
            ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
            if ctype.startswith("text/") or ctype.endswith("javascript"):
                ctype += "; charset=utf-8"
            with open(full, "rb") as fh:
                self._send(200, fh.read(), ctype)

        # ----------------------------------------------------------- POST
        def do_POST(self):
            if not self._guard():
                return
            if self.headers.get("X-DeckHaul") != "1":
                return self._send(403, b"forbidden", "text/plain")
            state["last_seen"] = time.time()
            n = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(n) or b"{}")
            except ValueError:
                return self._json({"error": "Неверный запрос"}, 400)
            routes = {
                "/api/refresh": lambda b: (app.refresh(), app.snapshot())[1],
                "/api/profile": lambda b: (app.select_profile(b["id"]), app.snapshot())[1],
                "/api/preview": lambda b: {"issues": [i.to_dict() for i in app.preview(b["order"])]},
                "/api/autosort": self._autosort,
                "/api/apply": lambda b: {"point": app.apply(b["order"]),
                                         "state": app.snapshot()},
                "/api/compat-ok": lambda b: (app.set_compat_ok(b["key"], bool(b.get("ok", True))), app.snapshot())[1],
                "/api/override": lambda b: (app.set_override(b["key"], b.get("group")), app.snapshot())[1],
                "/api/rule": lambda b: (app.add_rule(b["above"], b["below"]), app.snapshot())[1],
                "/api/rule/delete": lambda b: (app.delete_rule(b["id"]), app.snapshot())[1],
                "/api/points/save": lambda b: {"id": app.save_point(b.get("label", "")), "points": app.list_points()},
                "/api/points/rollback": lambda b: {"result": app.rollback(b["id"]), "state": app.snapshot()},
                "/api/points/delete": lambda b: (app.delete_point(b["id"]), {"points": app.list_points()})[1],
                "/api/points/ack": lambda b: (app.ack_point_errors(b["id"]), app.snapshot())[1],
                "/api/restore": lambda b: (app.restore(b["name"]), app.snapshot())[1],
                "/api/online": lambda b: {"checked": app.online_check(), "state": app.snapshot(),
                                          "workshop": app.workshop_status()},
                "/api/workshop/auto-set": lambda b: (app.set_auto_workshop_check(bool(b.get("on"))), app.workshop_status())[1],
                "/api/workshop/find": lambda b: app.find_in_workshop(b["key"]),
                "/api/workshop/switch": lambda b: (app.switch_to_workshop(b["local"], b["workshop"]), app.snapshot())[1],
                "/api/steam-key": lambda b: (app.set_steam_key(b.get("key")), app.workshop_status())[1],
                "/api/source": lambda b: (app.set_mod_source(b["key"], b.get("url")), app.snapshot())[1],
                "/api/downloads/install": lambda b: app.install_download(
                    b["id"], b["payloads"], bool(b.get("enable", True)), bool(b.get("remove_old", True)),
                    bool(b.get("delete_download", True))),
                "/api/downloads/dismiss": lambda b: (app.dismiss_download(b["id"]), {"ok": True})[1],
                "/api/downloads/dirs/add": lambda b: (app.add_download_dir(b["path"]), app.downloads())[1],
                "/api/downloads/dirs/remove": lambda b: (app.remove_download_dir(b["path"]), app.downloads())[1],
                "/api/update/check": lambda b: app.update_status(force=True),
                "/api/update/auto": lambda b: (app.set_auto_update_check(bool(b.get("on"))), app.update_status())[1],
                "/api/update/apply": self._apply_update,
                "/api/game-version": lambda b: (app.set_game_version(b.get("version")), app.snapshot())[1],
            }
            fn = routes.get(urlparse(self.path).path)
            if fn is None:
                return self._json({"error": "Нет такого действия"}, 404)
            self._safe(fn, body)

        @staticmethod
        def _apply_update(b):
            tag = app.apply_update()
            # The new code is on disk; restart into it once this response is sent.
            threading.Timer(1.0, _restart).start()
            return {"installed": tag, "restarting": True}

        @staticmethod
        def _autosort(b):
            order, cycles = app.auto_sort(b["order"])
            return {"order": order, "cycles": cycles,
                    "issues": [i.to_dict() for i in app.preview(order)]}

        def _safe(self, fn, body):
            try:
                return self._json(fn(body))
            except (UserError, SiiError, FileNotFoundError) as exc:
                return self._json({"error": str(exc)}, 400)
            except KeyError as exc:
                return self._json({"error": f"Не хватает поля {exc}"}, 400)
            except Exception as exc:  # show the user something instead of a dead page
                traceback.print_exc()
                return self._json({"error": f"Внутренняя ошибка: {exc}"}, 500)

    return Handler


def _restart() -> None:
    args = sys.argv[1:] or ["serve"]
    if "--no-browser" not in args:
        args.append("--no-browser")
    # Start from this copy's folder explicitly: "-m deckhaul" could pick up
    # another deckhaul folder from the current directory.
    boot = ("import sys; sys.path.insert(0, sys.argv.pop(1)); "
            "from deckhaul.__main__ import main; sys.exit(main())")
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.execv(sys.executable, [sys.executable, "-c", boot, here] + args)


def serve(app: App, port: int = 8765, open_browser: bool = True, idle_exit: int = 0) -> None:
    state = {"last_seen": time.time()}
    httpd = None
    for p in range(port, port + 20):
        try:
            httpd = ThreadingHTTPServer(("127.0.0.1", p), _make_handler(app, p, state))
            port = p
            break
        except OSError:
            continue
    if httpd is None:
        raise SystemExit("Не удалось открыть порт для интерфейса")
    url = f"http://127.0.0.1:{port}/"
    print(f"DeckHaul открыт: {url}  (Ctrl+C — выход)", flush=True)
    url_file = os.path.join(app.state_dir, "url")
    with open(url_file, "w") as fh:
        fh.write(url)
    if idle_exit:
        def watchdog():
            while True:
                time.sleep(15)
                if time.time() - state["last_seen"] > idle_exit:
                    httpd.shutdown()
                    return
        threading.Thread(target=watchdog, daemon=True).start()
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        httpd.server_close()
        try:
            os.remove(url_file)
        except OSError:
            pass
