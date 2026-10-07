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
        backups = app.backups()
        self.assertEqual(len(backups), 1)

        app.restore(backups[0]["name"])
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


if __name__ == "__main__":
    unittest.main()
