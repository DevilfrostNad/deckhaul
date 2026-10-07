"""SII files: ScsC decryption, a tolerant text parser and active_mods editing."""

from __future__ import annotations

import os
import re
import struct
import zlib
from typing import Any, Dict, List, Optional, Tuple

from . import aes

SCSC_MAGIC = b"ScsC"
SCS_KEY = bytes.fromhex("2A5FCB1791D22FB60245B3D8369ED0B2C27371563FBF1F3C9EDF6B11825A5D0A")


class SiiError(Exception):
    pass


class BinarySiiError(SiiError):
    """The file is binary SII (BSII); we can read mod names from it but not rewrite it."""

    def __init__(self, raw: bytes):
        super().__init__("профиль сохранён в двоичном формате")
        self.raw = raw


# ------------------------------------------------------------------ encoding

def decrypt_scsc(data: bytes) -> bytes:
    if data[:4] != SCSC_MAGIC or len(data) < 0x38:
        raise SiiError("не ScsC-файл")
    iv = data[0x24:0x34]
    blob = aes.cbc_decrypt(SCS_KEY, iv, data[0x38:])
    try:
        return zlib.decompress(blob)
    except zlib.error as exc:
        raise SiiError(f"не удалось расшифровать: {exc}")


def encrypt_scsc(plain: bytes) -> bytes:
    comp = zlib.compress(plain, 9)
    comp += b"\0" * ((-len(comp)) % 16)
    iv = os.urandom(16)
    return SCSC_MAGIC + b"\0" * 32 + iv + struct.pack("<I", len(plain)) + aes.cbc_encrypt(SCS_KEY, iv, comp)


def load_sii_bytes(data: bytes) -> Tuple[str, str]:
    """Return (text, storage) where storage is 'text' or 'scsc'. Raises BinarySiiError for BSII."""
    storage = "text"
    if data[:4] == SCSC_MAGIC:
        data = decrypt_scsc(data)
        storage = "scsc"
    if data[:4] == b"BSII":
        raise BinarySiiError(data)
    if data[:3] == b"\xef\xbb\xbf":
        data = data[3:]
    return data.decode("utf-8", "replace"), storage


# -------------------------------------------------------------------- parser

_TOKEN = re.compile(
    r'''\s*(?:
        (?P<comment>\#[^\n]*|//[^\n]*|/\*.*?\*/)
      | (?P<string>"(?:\\.|[^"\\])*")
      | (?P<tuple>\([^)]*\))
      | (?P<punct>[{}:])
      | (?P<word>[^\s{}:"]+)
    )''',
    re.S | re.X,
)


def unescape(s: str) -> str:
    raw = bytearray()
    i = 0
    while i < len(s):
        ch = s[i]
        if ch == "\\" and i + 1 < len(s):
            nxt = s[i + 1]
            if nxt == "x" and i + 3 < len(s) + 1:
                try:
                    raw.append(int(s[i + 2 : i + 4], 16))
                    i += 4
                    continue
                except ValueError:
                    pass
            raw += {"n": b"\n", "t": b"\t"}.get(nxt, nxt.encode("utf-8"))
            i += 2
            continue
        raw += ch.encode("utf-8")
        i += 1
    return raw.decode("utf-8", "replace")


def escape(s: str) -> str:
    out = []
    for ch in s:
        if ch in '"\\':
            out.append("\\" + ch)
        elif ord(ch) < 0x20 or ord(ch) > 0x7E:
            out.append("".join(f"\\x{b:02x}" for b in ch.encode("utf-8")))
        else:
            out.append(ch)
    return "".join(out)


def _tokens(text: str):
    pos = 0
    n = len(text)
    while pos < n:
        m = _TOKEN.match(text, pos)
        if not m or m.end() == pos:
            break
        pos = m.end()
        kind = m.lastgroup
        if kind == "comment" or kind is None:
            continue
        val = m.group(kind)
        if kind == "string":
            yield ("str", unescape(val[1:-1]))
        else:
            yield (kind, val)


class Unit:
    def __init__(self, cls: str, name: str):
        self.cls = cls
        self.name = name
        self.props: Dict[str, Any] = {}

    def get(self, key: str, default=None):
        return self.props.get(key, default)

    def __repr__(self):
        return f"Unit({self.cls} : {self.name})"


def parse_sii(text: str) -> List[Unit]:
    """Parse text SII into units. Arrays (`key[]:` or `key[3]:`) become lists."""
    toks = list(_tokens(text))
    units: List[Unit] = []
    i = 0
    # skip "SiiNunit {"
    while i < len(toks) and toks[i] != ("punct", "{"):
        i += 1
    i += 1
    while i < len(toks):
        if toks[i] == ("punct", "}"):
            i += 1
            continue
        # class : name {
        if i + 3 < len(toks) and toks[i + 1] == ("punct", ":") and toks[i + 3] == ("punct", "{"):
            unit = Unit(toks[i][1], toks[i + 2][1])
            i += 4
            while i < len(toks) and toks[i] != ("punct", "}"):
                if i + 2 < len(toks) and toks[i + 1] == ("punct", ":"):
                    key = toks[i][1]
                    val = toks[i + 2][1]
                    _set_prop(unit.props, key, val)
                    i += 3
                else:
                    i += 1
            i += 1
            units.append(unit)
        else:
            i += 1
    return units


_ARRAY_KEY = re.compile(r"^(\w+)\[(\d*)\]$")


def _set_prop(props: Dict[str, Any], key: str, val: str) -> None:
    m = _ARRAY_KEY.match(key)
    if m:
        name, idx = m.group(1), m.group(2)
        arr = props.get(name)
        if not isinstance(arr, list):
            arr = []
            props[name] = arr
        if idx == "":
            arr.append(val)
        else:
            k = int(idx)
            while len(arr) <= k:
                arr.append(None)
            arr[k] = val
        return
    if key in props and isinstance(props[key], list):
        return  # "active_mods: 5" after entries — keep the list
    props[key] = val


def first_unit(units: List[Unit], cls: str) -> Optional[Unit]:
    for u in units:
        if u.cls == cls:
            return u
    return None


# --------------------------------------------------------------- active_mods

_ACTIVE_LINE = re.compile(r"^[ \t]*active_mods(?:\[\d*\])?[ \t]*:.*(?:\r?\n|$)", re.M)


def read_active_mods(text: str) -> List[str]:
    unit = first_unit(parse_sii(text), "user_profile")
    if unit is None:
        raise SiiError("в profile.sii нет блока user_profile")
    mods = unit.get("active_mods")
    if not isinstance(mods, list):
        return []
    return [m for m in mods if m]


def write_active_mods(text: str, entries: List[str]) -> str:
    """Replace the active_mods block, keeping the rest of the file byte-for-byte."""
    matches = list(_ACTIVE_LINE.finditer(text))
    if not matches:
        raise SiiError("в profile.sii не найден список active_mods")
    first = matches[0]
    indent = re.match(r"[ \t]*", first.group(0)).group(0)
    nl = "\r\n" if first.group(0).endswith("\r\n") else "\n"
    block = [f"{indent}active_mods: {len(entries)}{nl}"]
    for i, e in enumerate(entries):
        block.append(f'{indent}active_mods[{i}]: "{escape(e)}"{nl}')
    out = []
    pos = 0
    for idx, m in enumerate(matches):
        out.append(text[pos : m.start()])
        if idx == 0:
            out.append("".join(block))
        pos = m.end()
    out.append(text[pos:])
    return "".join(out)


def bsii_guess_active_mods(raw: bytes) -> List[str]:
    """Best-effort: find a length-prefixed string array whose items look like 'id|Name'."""
    best: List[str] = []
    n = len(raw)
    i = 0
    while i + 8 < n:
        count = struct.unpack_from("<I", raw, i)[0]
        if 0 < count < 2000:
            p = i + 4
            items = []
            ok = True
            for _ in range(count):
                if p + 4 > n:
                    ok = False
                    break
                ln = struct.unpack_from("<I", raw, p)[0]
                if ln == 0 or ln > 1024 or p + 4 + ln > n:
                    ok = False
                    break
                s = raw[p + 4 : p + 4 + ln]
                if b"|" not in s:
                    ok = False
                    break
                items.append(s.decode("utf-8", "replace"))
                p += 4 + ln
            if ok and len(items) > len(best):
                best = items
                i = p
                continue
        i += 1
    return best


# ------------------------------------------------------------------- helpers

def version_matches(game_version: str, patterns: List[str]) -> bool:
    """compatible_versions uses globs like '1.53.*'. Missing list means 'any'."""
    import fnmatch

    if not patterns or not game_version:
        return True
    gv = game_version.strip()
    for p in patterns:
        p = (p or "").strip()
        if not p:
            continue
        if fnmatch.fnmatch(gv, p) or gv == p.rstrip(".*"):
            return True
    return False
