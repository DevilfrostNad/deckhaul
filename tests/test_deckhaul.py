import os
import shutil
import sys
import tempfile
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


class EndToEnd(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
