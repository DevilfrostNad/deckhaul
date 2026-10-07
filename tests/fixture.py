"""Build a fake Steam Deck home folder with a realistic set of good and broken mods."""

from __future__ import annotations

import io
import os
import struct
import sys
import zipfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from deckhaul.cityhash import hash_path  # noqa: E402
from deckhaul.sii import encrypt_scsc  # noqa: E402

GAME = "Euro Truck Simulator 2"


def manifest(name, version="1.0", author="Tester", cats=("truck",), compat=()):
    lines = ["SiiNunit", "{", "mod_package : .package_name", "{",
             f'    package_version: "{version}"', f'    display_name: "{name}"', f'    author: "{author}"']
    lines += [f'    category[]: "{c}"' for c in cats]
    lines += [f'    compatible_versions[]: "{v}"' for v in compat]
    lines += ['    icon: "icon.jpg"', '    description_file: "description.txt"', "}", "}"]
    return "\n".join(lines).encode()


def make_zip(path, files):
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for n, d in files.items():
            z.writestr(n, d)


def make_hashfs_v1(path, files):
    """Minimal HashFS v1 writer (uncompressed, with directory listings)."""
    dirs = {"": set()}
    for f in files:
        parts = f.split("/")
        for i in range(len(parts)):
            parent = "/".join(parts[:i])
            dirs.setdefault(parent, set())
            if i == len(parts) - 1:
                dirs[parent].add(parts[i])
            else:
                dirs[parent].add("*" + parts[i])
    blobs = []
    for f, data in files.items():
        blobs.append((hash_path(f), data, False))
    for d, items in dirs.items():
        blobs.append((hash_path(d), "\n".join(sorted(items)).encode(), True))
    out = io.BytesIO()
    out.write(b"\0" * 24)
    entries = []
    for h, data, is_dir in blobs:
        entries.append((h, out.tell(), 1 if is_dir else 0, len(data)))
        out.write(data)
    start = out.tell()
    for h, off, flags, size in sorted(entries):
        out.write(struct.pack("<QQIIII", h, off, flags, 0, size, size))
    buf = bytearray(out.getvalue())
    struct.pack_into("<4sHH4sIQ", buf, 0, b"SCS#", 1, 0, b"CITY", len(entries), start)
    with open(path, "wb") as fh:
        fh.write(buf)


def build(home):
    steam = os.path.join(home, ".local/share/Steam")
    apps = os.path.join(steam, "steamapps")
    os.makedirs(os.path.join(apps, "common", GAME), exist_ok=True)
    with open(os.path.join(apps, "libraryfolders.vdf"), "w") as fh:
        fh.write('"libraryfolders"\n{\n\t"0"\n\t{\n\t\t"path"\t\t"%s"\n\t}\n}\n' % steam)

    data = os.path.join(home, ".local/share", GAME)
    mod = os.path.join(data, "mod")
    os.makedirs(mod, exist_ok=True)
    with open(os.path.join(data, "game.log.txt"), "w") as fh:
        fh.write(
            "00:00:00.000 : [sys] Euro Truck Simulator 2 init ver.1.53.3.14s (rev. 1)\n"
            "00:00:05.000 : <ERROR> [unit] Unable to find unit '/def/vehicle/truck/scania.r/engine/x.sii'\n"
            "00:00:05.100 : <WARNING> [mat] Missing texture 'vehicle/truck/x.tobj'\n"
        )

    # good truck mod (zip)
    make_zip(os.path.join(mod, "scania_super.scs"), {
        "manifest.sii": manifest("Scania Super", "2.1", cats=("truck",), compat=("1.53.*",)),
        "description.txt": "Новый грузовик",
        "icon.jpg": b"\xff\xd8\xff\xe0fake",
        "def/vehicle/truck/scania.super/data.sii": "SiiNunit { }",
        "vehicle/truck/scania.super/model.pmd": "x" * 100,
    })
    # sound mod overriding the truck's file (it will sit below the truck = wrong order)
    make_zip(os.path.join(mod, "real_sounds.scs"), {
        "manifest.sii": manifest("Real Sounds", "5.0", cats=("sound",), compat=("1.53.*",)),
        "sound/truck/engine.bank": "s" * 50,
        "def/vehicle/truck/scania.super/data.sii": "SiiNunit { sound }",
    })
    # outdated mod
    make_zip(os.path.join(mod, "old_trailer.scs"), {
        "manifest.sii": manifest("Old Trailer", "0.9", cats=("trailer",), compat=("1.49.*",)),
        "def/vehicle/trailer/old/data.sii": "SiiNunit { }",
    })
    # HashFS v1 map mod
    make_hashfs_v1(os.path.join(mod, "promods-def-v275.scs"), {
        "manifest.sii": manifest("ProMods Definition Package", "2.75", "ProMods", ("map",), ("1.53.*",)),
        "def/world/city.promods.sii": b"SiiNunit { }",
    })
    make_hashfs_v1(os.path.join(mod, "promods-map-v275.scs"), {
        "manifest.sii": manifest("ProMods Map", "2.75", "ProMods", ("map",), ("1.53.*",)),
        "map/europe/sec+0001+0001.base": b"m" * 64,
        "def/world/city.promods.sii": b"SiiNunit { older }",
    })
    # wrongly packed: zip with scs inside
    make_zip(os.path.join(mod, "mega_pack.zip"), {"part1.scs": "x", "part2.scs": "y", "readme.txt": "hi"})
    # wrongly packed: extra folder level
    make_zip(os.path.join(mod, "wrapped.scs"), {
        "wrapped/manifest.sii": manifest("Wrapped Mod"),
        "wrapped/def/x.sii": "x",
    })
    # junk rar
    with open(os.path.join(mod, "cool_mod.rar"), "wb") as fh:
        fh.write(b"Rar!\x1a\x07\x00")
    # broken
    with open(os.path.join(mod, "broken.scs"), "wb") as fh:
        fh.write(b"this is not an archive")
    # duplicate of Scania Super, older version
    make_zip(os.path.join(mod, "scania_super_old.scs"), {
        "manifest.sii": manifest("Scania Super", "1.8", cats=("truck",), compat=("1.53.*",)),
        "def/vehicle/truck/scania.super/data.sii": "old",
    })
    # unpacked folder mod
    fm = os.path.join(mod, "my_paint")
    os.makedirs(os.path.join(fm, "def/vehicle/truck"), exist_ok=True)
    with open(os.path.join(fm, "manifest.sii"), "wb") as fh:
        fh.write(manifest("My Paint", "1.0", cats=("paint_job",)))
    with open(os.path.join(fm, "def/vehicle/truck/paint.sii"), "w") as fh:
        fh.write("SiiNunit { }")

    # workshop: item with versions.sii and two slots
    ws = os.path.join(apps, "workshop", "content", "227300")
    item = os.path.join(ws, "1234567890")
    os.makedirs(item, exist_ok=True)
    with open(os.path.join(item, "versions.sii"), "w") as fh:
        fh.write('SiiNunit\n{\npackage_version_info : .153 {\n package_name: "153"\n compatible_versions[]: "1.53.*"\n}\n'
                 'package_version_info : .universal {\n package_name: "universal"\n}\n}\n')
    make_zip(os.path.join(item, "153.scs"), {
        "manifest.sii": manifest("Realistic Graphics", "3.0", cats=("graphics",), compat=("1.53.*",)),
        "def/climate/default/weather.sii": "w",
    })
    make_zip(os.path.join(item, "universal.scs"), {
        "manifest.sii": manifest("Realistic Graphics", "2.0", cats=("graphics",)),
    })
    os.makedirs(os.path.join(ws, "999"), exist_ok=True)  # empty item
    with open(os.path.join(apps, "workshop", "appworkshop_227300.acf"), "w") as fh:
        fh.write('"AppWorkshop"\n{\n"WorkshopItemsInstalled"\n{\n"1234567890"\n{\n"timeupdated" "100"\n"manifest" "1"\n}\n}\n'
                 '"WorkshopItemDetails"\n{\n"1234567890"\n{\n"timeupdated" "200"\n"manifest" "1"\n"latest_manifest" "2"\n}\n}\n}\n')

    # profile: encrypted, wrong order, a missing mod and a duplicate entry
    prof = os.path.join(data, "profiles", "Тест".encode().hex().upper())
    os.makedirs(prof, exist_ok=True)
    text = (
        "SiiNunit\n{\nuser_profile : _nameless.1234 {\n"
        " face: 0\n"
        " active_mods: 7\n"
        ' active_mods[0]: "promods-map-v275|ProMods Map"\n'
        ' active_mods[1]: "promods-def-v275|ProMods Definition Package"\n'
        ' active_mods[2]: "real_sounds|Real Sounds"\n'
        ' active_mods[3]: "scania_super|Scania Super"\n'
        ' active_mods[4]: "old_trailer|Old Trailer"\n'
        ' active_mods[5]: "gone_mod|Удалённый мод"\n'
        ' active_mods[6]: "mod_workshop_package.00000000499602D2|Realistic Graphics"\n'
        ' profile_name: "\\xd0\\xa2\\xd0\\xb5\\xd1\\x81\\xd1\\x82"\n'
        " company_name: \"Deck Logistics\"\n"
        "}\n}\n"
    )
    with open(os.path.join(prof, "profile.sii"), "wb") as fh:
        fh.write(encrypt_scsc(text.encode()))

    # a mod lost in the Proton folder
    pmod = os.path.join(apps, "compatdata", "227300", "pfx", "drive_c", "users", "steamuser",
                        "Documents", GAME, "mod")
    os.makedirs(pmod, exist_ok=True)
    make_zip(os.path.join(pmod, "forgotten.scs"), {"manifest.sii": manifest("Forgotten")})
    return home


if __name__ == "__main__":
    print(build(sys.argv[1]))
