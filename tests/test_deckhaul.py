import os
import shutil
import sys
import tempfile
import time
import unittest

HERE = os.path.dirname(__file__)
sys.path.insert(0, os.path.join(HERE, ".."))

from deckhaul import aes, sii  # noqa: E402
from deckhaul.archive import open_container  # noqa: E402
from deckhaul.cityhash import cityhash64, hash_path  # noqa: E402


# Real HashFS v2 archives from TruckLib.HashFs (GPL-2.0). They are not stored in
# this repository; the test downloads them once into tests/data.
SAMPLES_URL = "https://raw.githubusercontent.com/sk-zk/TruckLib.HashFs/master/TruckLib.HashFs.Tests/Data/"


def _sample(name):
    path = os.path.join(HERE, "data", name)
    if not os.path.isfile(path):
        import urllib.request
        os.makedirs(os.path.dirname(path), exist_ok=True)
        try:
            with urllib.request.urlopen(SAMPLES_URL + name, timeout=15) as r:
                data = r.read()
        except OSError:
            return None
        with open(path, "wb") as fh:
            fh.write(data)
    return path


class Primitives(unittest.TestCase):
    def test_cityhash(self):
        self.assertEqual(cityhash64(b""), 0x9AE16A3B2F90404F)
        # value from TruckLib.HashFs tests (the hash SCS uses)
        self.assertEqual(hash_path("/käsefondue.txt"), 8645157520230346068)
        self.assertEqual(hash_path("käsefondue.txt"), 8645157520230346068)

    def test_aes_fips197(self):
        key = bytes(range(32))
        pt = bytes.fromhex("00112233445566778899aabbccddeeff")
        ct = aes.cbc_encrypt(key, bytes(16), pt)
        self.assertEqual(ct.hex(), "8ea2b7ca516745bfeafc49904b496089")
        self.assertEqual(aes.cbc_decrypt(key, bytes(16), ct), pt)

    def test_scsc_roundtrip(self):
        text = 'SiiNunit\n{\nuser_profile : x {\n active_mods: 0\n}\n}\n'
        text2, storage = sii.load_sii_bytes(sii.encrypt_scsc(text.encode()))
        self.assertEqual((text2, storage), (text, "scsc"))

    def test_write_active_mods(self):
        text = ('SiiNunit\n{\nuser_profile : x {\n a: 1\n active_mods: 2\n'
                ' active_mods[0]: "b|B"\n active_mods[1]: "a|A"\n z: 2\n}\n}\n')
        out = sii.write_active_mods(text, ["a|A", "b|Б", "c|C"])
        self.assertEqual(sii.read_active_mods(out), ["a|A", "b|Б", "c|C"])
        self.assertIn(" a: 1\n", out)
        self.assertIn(" z: 2\n", out)
        self.assertIn('"b|\\xd0\\x91"', out)

    def test_version_match(self):
        self.assertTrue(sii.version_matches("1.53.3", ["1.53.*"]))
        self.assertFalse(sii.version_matches("1.53.3", ["1.49.*", "1.50.*"]))
        self.assertTrue(sii.version_matches("1.53.3", []))


class HashFs(unittest.TestCase):
    def test_v2_sample(self):
        path = _sample("simple_v2.scs")
        if path is None:
            self.skipTest("нет сети: образец HashFS v2 не скачан")
        with open_container(path) as c:
            self.assertEqual(c.kind, "hashfs2")
            self.assertEqual(sorted(c.root_names()), ["somedir", "uncompressed.txt"])
            self.assertEqual(c.read("uncompressed.txt"), b"my hovercraft is full of eels.")
            self.assertEqual(len(c.read("/somedir/long.txt")), 3228)

    def test_v1_roundtrip(self):
        from fixture import make_hashfs_v1
        d = tempfile.mkdtemp()
        try:
            p = os.path.join(d, "x.scs")
            make_hashfs_v1(p, {"manifest.sii": b"m", "def/a/b.sii": b"hello"})
            with open_container(p) as c:
                self.assertEqual(c.kind, "hashfs1")
                self.assertEqual(c.read("def/a/b.sii"), b"hello")
                self.assertIn("def/a/b.sii", c.file_hashes().values())
        finally:
            shutil.rmtree(d)


class _FakeDeck(unittest.TestCase):
    def setUp(self):
        from fixture import build
        self.home = tempfile.mkdtemp()
        build(self.home)
        os.environ["HOME"] = self.home
        os.environ["DECKHAUL_STATE_DIR"] = os.path.join(self.home, "state")
        import importlib
        from deckhaul import paths
        importlib.reload(paths)
        from deckhaul import core
        importlib.reload(core)
        self.App = core.App

    def tearDown(self):
        shutil.rmtree(self.home)


class EndToEnd(_FakeDeck):

    def test_full_flow(self):
        app = self.App()
        app.refresh()
        self.assertEqual(app.game_version, "1.53.3")
        self.assertEqual(app.profile.name, "Тест")
        codes = {i.code for i in app.issues}
        for c in ("missing", "incompatible", "broken_archive", "nested_archive", "nested_folder",
                  "junk_archive", "order", "conflict", "duplicate_installed", "workshop_pending",
                  "workshop_empty", "wrong_mod_dir", "log_errors"):
            self.assertIn(c, codes)

        # workshop item resolved to the slot for 1.53
        gfx = app.by_key["mod_workshop_package.00000000499602D2"]
        self.assertEqual((gfx.name, gfx.version, gfx.workshop_slot), ("Realistic Graphics", "3.0", "153"))

        keys = [e.key for e in app.profile.active]
        new, cycles = app.auto_sort(keys)
        self.assertEqual(cycles, [])
        self.assertLess(new.index("promods-def-v275"), new.index("promods-map-v275"))
        self.assertLess(new.index("real_sounds"), new.index("scania_super"))
        self.assertEqual(new[-1], "promods-map-v275")

        new.remove("gone_mod")
        app.apply(new)
        self.assertEqual([e.key for e in app.profile.active], new)
        codes = {i.code for i in app.issues}
        self.assertNotIn("missing", codes)
        self.assertNotIn("order", codes)
        points = app.list_points()
        self.assertEqual(len(points), 1)
        self.assertEqual(points[0]["reason"], "apply")
        self.assertIn("выключены", points[0]["summary"])

        app.rollback(points[0]["id"])
        self.assertIn("gone_mod", [e.key for e in app.profile.active])

    def test_history_detects_update(self):
        app = self.App()
        app.refresh()
        from fixture import make_zip, manifest
        mod = os.path.join(app.layout.active.mod_dir, "real_sounds.scs")
        os.remove(mod)
        make_zip(mod, {"manifest.sii": manifest("Real Sounds", "5.1", cats=("sound",)),
                       "sound/x.bank": "y"})
        app.refresh()
        kinds = [(e["kind"], e.get("new")) for e in app.new_events]
        self.assertIn(("updated", "5.1"), kinds)


class CompatMarks(_FakeDeck):
    def _incompat(self, app):
        return [i for i in app.issues if i.code == "incompatible" and i.mod == "old_trailer"]

    def test_mark_and_reset(self):
        app = self.App()
        app.refresh()
        issue = self._incompat(app)[0]
        self.assertEqual(issue.severity, "warning")          # not an error any more
        self.assertEqual(issue.action, "compat_ok")

        app.set_compat_ok("old_trailer", True)
        self.assertEqual(self._incompat(app), [])
        again = self.App()
        again.refresh()
        self.assertEqual(self._incompat(again), [])

        # the game updates: the warning comes back and remembers the old mark
        log = os.path.join(app.layout.active.path, "game.log.txt")
        with open(log, encoding="utf-8") as fh:
            text = fh.read().replace("ver.1.53.3.14s", "ver.1.54.0.5s")
        with open(log, "w", encoding="utf-8") as fh:
            fh.write(text)
        app.refresh()
        issue = self._incompat(app)[0]
        self.assertIn("1.53.3", issue.detail)

        app.set_compat_ok("old_trailer", True)
        self.assertEqual(self._incompat(app), [])
        app.set_compat_ok("old_trailer", False)
        self.assertEqual(len(self._incompat(app)), 1)

    def test_installer_sees_mark(self):
        app = self.App()
        app.refresh()
        app.set_compat_ok("old_trailer", True)
        from fixture import make_zip, manifest
        dl = os.path.join(self.home, "Downloads")
        make_zip(os.path.join(dl, "old_trailer_copy.zip"), {
            "manifest.sii": manifest("Old Trailer", "0.9", cats=("trailer",), compat=("1.49.*",)),
            "def/vehicle/trailer/old/data.sii": "SiiNunit { }",
        })
        old = time.time() - 600
        os.utime(os.path.join(dl, "old_trailer_copy.zip"), (old, old))
        d = app.downloads()
        while d["pending"]:
            d = app.downloads()
        p = {c["name"]: c for c in d["items"]}["old_trailer_copy.zip"]["payloads"][0]
        self.assertFalse(p["compat"])
        self.assertTrue(p["compat_ok"])


class RestorePoints(_FakeDeck):
    def setUp(self):
        super().setUp()
        old = time.time() - 600
        dl = os.path.join(self.home, "Downloads")
        for n in os.listdir(dl):
            os.utime(os.path.join(dl, n), (old, old))

    def _install(self, app, name):
        d = app.downloads()
        while d["pending"]:
            d = app.downloads()
        c = {c["name"]: c for c in d["items"]}[name]
        return app.install_download(c["id"], [p["id"] for p in c["payloads"]], True, True, True)

    def test_rollback_install_restores_files_and_profile(self):
        app = self.App()
        app.refresh()
        mod_dir = app.layout.active.mod_dir
        keys_before = [e.key for e in app.profile.active]
        files_before = sorted(os.listdir(mod_dir))
        dl = os.path.join(self.home, "Downloads")

        r = self._install(app, "real_sounds_5.1.zip")
        self.assertIn("real_sounds_5.1.scs", os.listdir(mod_dir))
        self.assertNotIn("real_sounds_5.1.zip", os.listdir(dl))
        point = app.list_points()[0]
        self.assertEqual(point["id"], r["point"])
        self.assertIn("Real Sounds 5.1 вместо 5.0", point["summary"])

        app.rollback(r["point"])
        self.assertEqual(sorted(os.listdir(mod_dir)), files_before)       # 5.0 back, 5.1 gone
        self.assertEqual([e.key for e in app.profile.active], keys_before)
        self.assertIn("real_sounds_5.1.zip", os.listdir(dl))             # download is back too
        missing = {i.mod for i in app.issues if i.code == "missing"}
        self.assertEqual(missing, {"gone_mod"})                          # only the fixture's own

        # the rollback itself can be undone
        undo = app.list_points()[0]
        self.assertEqual(undo["reason"], "rollback")
        app.rollback(undo["id"])
        self.assertIn("real_sounds_5.1.scs", os.listdir(mod_dir))
        self.assertIn("real_sounds_5.1", [e.key for e in app.profile.active])

    def test_manual_point_undoes_everything_after_it(self):
        app = self.App()
        app.refresh()
        mod_dir = app.layout.active.mod_dir
        files_before = sorted(os.listdir(mod_dir))
        keys_before = [e.key for e in app.profile.active]
        pid = app.save_point("Всё работает")
        self._install(app, "real_sounds_5.1.zip")
        self._install(app, "trailer_pack_v3.zip")
        keys = [e.key for e in app.profile.active]
        keys.reverse()
        app.apply(keys)
        res = app.rollback(pid)
        self.assertEqual(sorted(os.listdir(mod_dir)), files_before)
        self.assertEqual([e.key for e in app.profile.active], keys_before)
        self.assertEqual(res["removed"], 3)
        # the saved state stays usable: change things again and come back again
        self._install(app, "Cool Lights 2.0.zip")
        self.assertIn("Cool_Lights.scs", os.listdir(mod_dir))
        app.rollback(pid)
        self.assertEqual(sorted(os.listdir(mod_dir)), files_before)
        self.assertEqual([e.key for e in app.profile.active], keys_before)

    def test_jumping_between_points(self):
        app = self.App()
        app.refresh()
        mod_dir = app.layout.active.mod_dir
        p1 = self._install(app, "trailer_pack_v3.zip")["point"]     # before trailers
        p2 = self._install(app, "Cool Lights 2.0.zip")["point"]     # before lights
        app.rollback(p1)                                           # nothing installed
        self.assertNotIn("part1.scs", os.listdir(mod_dir))
        self.assertNotIn("Cool_Lights.scs", os.listdir(mod_dir))
        app.rollback(p2)                                           # trailers yes, lights no
        self.assertIn("part1.scs", os.listdir(mod_dir))
        self.assertNotIn("Cool_Lights.scs", os.listdir(mod_dir))
        self.assertIn("part1", [e.key for e in app.profile.active])
        self.assertNotIn("Cool_Lights", [e.key for e in app.profile.active])

    def test_errors_after_change(self):
        app = self.App()
        app.refresh()
        self._install(app, "real_sounds_5.1.zip")
        self.assertNotIn("after_change", {i.code for i in app.issues})
        # the game runs after the install and logs a new error
        log = os.path.join(app.layout.active.path, "game.log.txt")
        with open(log, "a", encoding="utf-8") as fh:
            fh.write("00:01:00.000 : <ERROR> [sound] Cannot load 'sound/truck/engine.bank'\n")
        future = time.time() + 5
        os.utime(log, (future, future))
        app.refresh()
        issue = [i for i in app.issues if i.code == "after_change"][0]
        self.assertIn("engine.bank", issue.detail)
        self.assertNotIn("scania.r", issue.detail)                       # old error is not "new"
        app.ack_point_errors(issue.extra["point"])
        self.assertNotIn("after_change", {i.code for i in app.issues})

    def test_prune_keeps_manual_and_cleans_trash(self):
        from deckhaul import restore
        store = restore.RestoreStore(os.path.join(self.home, "st"))
        f = os.path.join(self.home, "a.scs")
        open(f, "w").close()
        manual = store.create(reason="manual", summary="", profile_id=None, profile_name="",
                              profile_path=None, mod_dir="", kind="manual", label="keep")
        first = store.create(reason="install", summary="", profile_id=None, profile_name="",
                             profile_path=None, mod_dir="")
        dst = store.move_to_trash(f)
        store.record(first, [{"op": "moved", "src": f, "dst": dst}])
        for _ in range(restore.KEEP_AUTO):
            time.sleep(0.001)
            store.create(reason="apply", summary="", profile_id=None, profile_name="",
                         profile_path=None, mod_dir="")
        ids = [p.id for p in store.points()]
        self.assertIn(manual.id, ids)
        self.assertNotIn(first.id, ids)
        self.assertFalse(os.path.exists(dst))


class ModUpdates(_FakeDeck):
    def _with_local_copy_of_workshop_mod(self, app):
        """A local copy of the Workshop 'Realistic Graphics', enabled instead of it."""
        from fixture import make_zip, manifest
        make_zip(os.path.join(app.layout.active.mod_dir, "realistic_gfx_local.scs"), {
            "manifest.sii": manifest("Realistic Graphics", "2.5", cats=("graphics",), compat=("1.53.*",)),
            "def/climate/default/weather.sii": "local",
        })
        app.refresh()
        keys = [e.key for e in app.profile.active]
        ws = "mod_workshop_package.00000000499602D2"
        keys[keys.index(ws)] = "realistic_gfx_local"
        app.apply(keys)
        return ws

    def test_switch_to_workshop(self):
        app = self.App()
        app.refresh()
        ws = self._with_local_copy_of_workshop_mod(app)
        issue = [i for i in app.issues if i.code == "local_has_workshop"][0]
        self.assertEqual(issue.extra, {"local": "realistic_gfx_local", "workshop": ws})
        pos = [e.key for e in app.profile.active].index("realistic_gfx_local")
        r = app.switch_to_workshop("realistic_gfx_local", ws)
        keys = [e.key for e in app.profile.active]
        self.assertEqual(keys[pos], ws)
        self.assertNotIn("realistic_gfx_local", keys)
        self.assertFalse(os.path.exists(os.path.join(app.layout.active.mod_dir, "realistic_gfx_local.scs")))
        app.rollback(r["point"])
        self.assertIn("realistic_gfx_local", [e.key for e in app.profile.active])

    def test_find_in_workshop(self):
        from deckhaul import workshop_online
        app = self.App()
        app.refresh()
        with self.assertRaises(Exception):
            app.find_in_workshop("real_sounds")                     # no key yet
        with self.assertRaises(Exception):
            app.set_steam_key("not-a-key")
        app.set_steam_key("0123456789abcdef0123456789ABCDEF")
        seen = {}

        def fake_search(key, app_id, text, count=8, timeout=15.0):
            seen.update(key=key, app_id=app_id, text=text)
            return [
                {"id": 1, "title": "Truck Lights Pack", "description": "", "updated": 1, "subscriptions": 900, "preview": ""},
                {"id": 2, "title": "Real Sounds v5.1 by Tester", "description": "", "updated": 2, "subscriptions": 10, "preview": ""},
                {"id": 1234567890, "title": "Real Sounds", "description": "", "updated": 3, "subscriptions": 5, "preview": ""},
            ]
        orig = workshop_online.search
        workshop_online.search = fake_search
        try:
            r = app.find_in_workshop("real_sounds")
        finally:
            workshop_online.search = orig
        self.assertEqual(seen["text"], "Real Sounds")
        self.assertEqual(seen["app_id"], 227300)
        self.assertEqual([f["id"] for f in r["results"]][:2], [1234567890, 2])   # exact title first
        self.assertTrue(r["results"][0]["installed"])                           # already subscribed
        self.assertEqual(r["results"][-1]["score"], 0.0)

    def test_sources_survive_update(self):
        app = self.App()
        app.refresh()
        with self.assertRaises(Exception):
            app.set_mod_source("real_sounds", "ftp://x")
        app.set_mod_source("real_sounds", "https://example.com/real-sounds")
        m = {d["key"]: d for d in app.snapshot()["mods"]}
        self.assertEqual(m["real_sounds"]["source_url"], "https://example.com/real-sounds")
        dl = os.path.join(self.home, "Downloads")
        old = time.time() - 600
        for n in os.listdir(dl):
            os.utime(os.path.join(dl, n), (old, old))
        d = app.downloads()
        while d["pending"]:
            d = app.downloads()
        c = {c["name"]: c for c in d["items"]}["real_sounds_5.1.zip"]
        app.install_download(c["id"], [0], True, True, True)
        m = {d["key"]: d for d in app.snapshot()["mods"]}
        self.assertEqual(m["real_sounds_5.1"]["source_url"], "https://example.com/real-sounds")

    def test_needs_update_and_daily_check(self):
        from deckhaul import workshop_online
        app = self.App()
        app.refresh()
        m = {d["key"]: d for d in app.snapshot()["mods"]}
        self.assertTrue(m["old_trailer"]["needs_update"])
        self.assertFalse(m["scania_super"]["needs_update"])
        calls = []

        def fake_details(ids, timeout=15.0):
            calls.append(ids)
            return {1234567890: {"result": 1, "time_updated": 10 ** 10, "title": "Realistic Graphics"}}
        orig = workshop_online.fetch_details
        workshop_online.fetch_details = fake_details
        try:
            self.assertTrue(app.workshop_auto()["ran"])
            self.assertFalse(app.workshop_auto()["ran"])            # once a day
            self.assertEqual(len(calls), 1)
            self.assertIn("workshop_outdated", {i.code for i in app.issues})
            again = self.App()                                      # result survives a restart
            again.refresh()
            self.assertIn("workshop_outdated", {i.code for i in again.issues})
            app.set_auto_workshop_check(False)
            app.online_checked = 0
            self.assertFalse(app.workshop_auto()["ran"])
        finally:
            workshop_online.fetch_details = orig


class Downloads(_FakeDeck):
    def setUp(self):
        super().setUp()
        # Fresh files look like downloads still in progress; age them.
        old = time.time() - 600
        for d in ("Downloads", os.path.join("Games", "ETS2 Mods")):
            dl = os.path.join(self.home, d)
            for n in os.listdir(dl):
                os.utime(os.path.join(dl, n), (old, old))
    def _items(self, app):
        d = app.downloads()
        while d["pending"]:
            d = app.downloads()
        return {c["name"]: c for c in d["items"]}

    def test_analysis(self):
        app = self.App()
        app.refresh()
        d = app.downloads()
        self.assertGreater(d["pending"], 0)               # big folders are analysed in portions
        while d["pending"]:
            d = app.downloads()
        self.assertIn("big_map.zip", d["busy"])           # .crdownload next to it
        items = {c["name"]: c for c in d["items"]}
        self.assertNotIn("big_map.zip", items)
        self.assertEqual(items["photos.zip"]["status"], "error")
        tp = items["trailer_pack_v3.zip"]["payloads"]
        self.assertEqual([p["target"] for p in tp], ["part1.scs", "part2.scs"])
        cl = items["Cool Lights 2.0.zip"]["payloads"][0]
        self.assertEqual(cl["target"], "Cool_Lights.scs")
        self.assertTrue(any("лишняя папка" in n for n in cl["notes"]))
        rs = items["real_sounds_5.1.zip"]["payloads"][0]
        self.assertEqual([r["key"] for r in rs["replaces"]], ["real_sounds"])
        old_map = items["old_map.rar"]
        if d["tool"]:
            self.assertFalse(old_map["payloads"][0]["compat"])
        else:
            self.assertEqual(old_map["status"], "needs_tool")

    def test_install_update_keeps_position(self):
        app = self.App()
        app.refresh()
        pos = [e.key for e in app.profile.active].index("real_sounds")
        c = self._items(app)["real_sounds_5.1.zip"]
        r = app.install_download(c["id"], [0], enable=True, remove_old=True, delete_download=True)
        self.assertEqual(r["installed"], ["real_sounds_5.1"])
        keys = [e.key for e in app.profile.active]
        self.assertEqual(keys[pos], "real_sounds_5.1")
        self.assertNotIn("real_sounds", keys)
        mod_dir = app.layout.active.mod_dir
        self.assertFalse(os.path.exists(os.path.join(mod_dir, "real_sounds.scs")))
        self.assertTrue(os.path.exists(os.path.join(mod_dir, "real_sounds_5.1.scs")))
        self.assertNotIn("real_sounds_5.1.zip", self._items(app))
        self.assertEqual(r["trashed"], 2)

    def test_install_wrapped_folder(self):
        app = self.App()
        app.refresh()
        c = self._items(app)["Cool Lights 2.0.zip"]
        app.install_download(c["id"], [0], enable=False, remove_old=False, delete_download=False)
        m = app.by_key["Cool_Lights"]
        self.assertEqual((m.name, m.version), ("Cool Lights", "2.0"))
        self.assertEqual(m.scan_problems, [])
        self.assertNotIn("Cool_Lights", [e.key for e in app.profile.active])

    def test_dismiss(self):
        app = self.App()
        app.refresh()
        c = self._items(app)["photos.zip"]
        app.dismiss_download(c["id"])
        self.assertNotIn("photos.zip", self._items(app))


    def test_extra_folder(self):
        app = self.App()
        app.refresh()
        lib = os.path.join(self.home, "Games", "ETS2 Mods")
        self.assertNotIn("daf_xg_interior.scs", self._items(app))
        app.add_download_dir(lib)
        with self.assertRaises(Exception):
            app.add_download_dir(lib)                      # no duplicates
        with self.assertRaises(Exception):
            app.add_download_dir(app.layout.active.mod_dir)  # never the game's own mod folder
        items = self._items(app)
        self.assertFalse(items["daf_xg_interior.scs"]["already"])
        self.assertFalse(items["daf_xg_interior.scs"]["is_default_dir"])
        self.assertTrue(items["scania_super.scs"]["already"])
        self.assertTrue(items["volvo_fh_tuning.scs"]["is_default_dir"])
        # settings survive a restart
        self.assertIn(os.path.realpath(lib), [os.path.realpath(d) for d in self.App().download_dirs])
        app.remove_download_dir(lib)
        self.assertNotIn("daf_xg_interior.scs", self._items(app))

    def test_browse(self):
        app = self.App()
        r = app.browse(os.path.join(self.home, "Games"))
        self.assertEqual(r["dirs"], ["ETS2 Mods"])
        self.assertEqual(app.browse(os.path.join(self.home, "Games", "ETS2 Mods"))["archives"], 2)
        self.assertEqual(app.browse("/no/such/dir")["path"], os.path.realpath(self.home))


class Updates(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()

    def tearDown(self):
        shutil.rmtree(self.tmp)

    @staticmethod
    def _fetch(responses):
        def fetch(url, timeout=15.0):
            for key, body in responses.items():
                if key in url:
                    return body
            raise OSError("404 " + url)
        return fetch

    def test_versions_and_notes(self):
        from deckhaul import updater
        self.assertEqual(updater.parse_version("v1.10.2"), (1, 10, 2))
        self.assertIsNone(updater.parse_version("latest"))
        tags = b'[{"name": "v1.0.0"}, {"name": "v9.2.0"}, {"name": "v9.10.0"}, {"name": "test"}]'
        notes = "# x\n\n## 9.10.0 \u2014 date\n\n- one\n- two\n\n## 9.2.0\n\n- old\n".encode()
        u = updater.Updater(self.tmp, fetch=self._fetch({"/tags": tags, "CHANGELOG.md": notes}))
        st = u.check(force=True)
        self.assertEqual(st["latest"], "v9.10.0")
        self.assertTrue(st["available"])
        self.assertEqual(st["notes"], "- one\n- two")
        # cached: no network on the next call
        u.fetch = self._fetch({})
        self.assertTrue(u.check()["available"])

    def test_offline(self):
        from deckhaul import updater
        st = updater.Updater(self.tmp, fetch=self._fetch({})).check(force=True)
        self.assertFalse(st["available"])
        self.assertIn("GitHub", st["error"])

    def _tarball(self, extra=None):
        import tarfile
        repo = os.path.join(HERE, "..")
        path = os.path.join(self.tmp, "src.tar.gz")
        with tarfile.open(path, "w:gz") as tf:
            for name in ("deckhaul", "install.sh", "assets"):
                tf.add(os.path.join(repo, name), arcname="deckhaul-9.9.9/" + name,
                       filter=lambda ti: None if "__pycache__" in ti.name else ti)
            if extra:
                extra(tf)
        return path

    def test_apply_runs_installer(self):
        from deckhaul import updater
        tarball = self._tarball()
        with open(tarball, "rb") as fh:
            blob = fh.read()
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        old_home = os.environ.get("HOME")
        os.environ["HOME"] = home
        try:
            u = updater.Updater(self.tmp, fetch=self._fetch({"archive": blob}))
            self.assertEqual(u.apply("v9.9.9", archive_url="https://example/archive.tar.gz"), "v9.9.9")
        finally:
            os.environ["HOME"] = old_home
        app_dir = os.path.join(home, ".local/share/deckhaul-app/deckhaul")
        self.assertTrue(os.path.isfile(os.path.join(app_dir, "updater.py")))
        self.assertTrue(os.access(os.path.join(home, ".local/bin/deckhaul"), os.X_OK))

    def test_rejects_path_escape(self):
        import io
        import tarfile
        from deckhaul import updater

        def evil(tf):
            data = b"x"
            ti = tarfile.TarInfo("deckhaul-9.9.9/../../escape.txt")
            ti.size = len(data)
            tf.addfile(ti, io.BytesIO(data))
        tarball = self._tarball(evil)
        with open(tarball, "rb") as fh:
            blob = fh.read()
        u = updater.Updater(self.tmp, fetch=self._fetch({"archive": blob}))
        with self.assertRaises(updater.UpdateError):
            u.apply("v9.9.9", archive_url="https://example/archive.tar.gz")
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "..", "escape.txt")))


if __name__ == "__main__":
    unittest.main()
