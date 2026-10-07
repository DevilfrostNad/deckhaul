"""CityHash64 (Google CityHash v1.0.x), the path hash used by SCS HashFS archives.

Pure-Python port. SCS uses the original 1.0.x variant, whose short-string
branch differs from later CityHash releases, so a library from PyPI would
produce different values.

CityHash: Copyright (c) 2011 Google, Inc. Released under the MIT license.
"""

from __future__ import annotations

import struct

M64 = 0xFFFFFFFFFFFFFFFF
K0 = 0xC3A5C85C97CB3127
K1 = 0xB492B66FBE98F273
K2 = 0x9AE16A3B2F90404F
K3 = 0xC949D7C7509E6557
KMUL = 0x9DDFEA08EB382D69


def _f64(s: bytes, p: int) -> int:
    return struct.unpack_from("<Q", s, p)[0]


def _f32(s: bytes, p: int) -> int:
    return struct.unpack_from("<I", s, p)[0]


def _rot(v: int, shift: int) -> int:
    if shift == 0:
        return v
    return ((v >> shift) | (v << (64 - shift))) & M64


def _shift_mix(v: int) -> int:
    return v ^ (v >> 47)


def _hash16(u: int, v: int) -> int:
    a = ((u ^ v) * KMUL) & M64
    a ^= a >> 47
    b = ((v ^ a) * KMUL) & M64
    b ^= b >> 47
    return (b * KMUL) & M64


def _len0to16(s: bytes, n: int) -> int:
    if n > 8:
        a = _f64(s, 0)
        b = _f64(s, n - 8)
        return _hash16(a, _rot((b + n) & M64, n)) ^ b
    if n >= 4:
        a = _f32(s, 0)
        return _hash16((n + (a << 3)) & M64, _f32(s, n - 4))
    if n > 0:
        a, b, c = s[0], s[n >> 1], s[n - 1]
        y = (a + (b << 8)) & M64
        z = (n + (c << 2)) & M64
        return (_shift_mix(((y * K2) ^ (z * K3)) & M64) * K2) & M64
    return K2


def _len17to32(s: bytes, n: int) -> int:
    a = (_f64(s, 0) * K1) & M64
    b = _f64(s, 8)
    c = (_f64(s, n - 8) * K2) & M64
    d = (_f64(s, n - 16) * K0) & M64
    return _hash16(
        (_rot((a - b) & M64, 43) + _rot(c, 30) + d) & M64,
        (a + _rot(b ^ K3, 20) - c + n) & M64,
    )


def _len33to64(s: bytes, n: int) -> int:
    z = _f64(s, 24)
    a = (_f64(s, 0) + (n + _f64(s, n - 16)) * K0) & M64
    b = _rot((a + z) & M64, 52)
    c = _rot(a, 37)
    a = (a + _f64(s, 8)) & M64
    c = (c + _rot(a, 7)) & M64
    a = (a + _f64(s, 16)) & M64
    vf = (a + z) & M64
    vs = (b + _rot(a, 31) + c) & M64
    a = (_f64(s, 16) + _f64(s, n - 32)) & M64
    z = _f64(s, n - 8)
    b = _rot((a + z) & M64, 52)
    c = _rot(a, 37)
    a = (a + _f64(s, n - 24)) & M64
    c = (c + _rot(a, 7)) & M64
    a = (a + _f64(s, n - 16)) & M64
    wf = (a + z) & M64
    ws = (b + _rot(a, 31) + c) & M64
    r = _shift_mix(((vf + ws) * K2 + (wf + vs) * K0) & M64)
    return (_shift_mix((r * K0 + vs) & M64) * K2) & M64


def _weak32(w: int, x: int, y: int, z: int, a: int, b: int):
    a = (a + w) & M64
    b = _rot((b + a + z) & M64, 21)
    c = a
    a = (a + x + y) & M64
    b = (b + _rot(a, 44)) & M64
    return (a + z) & M64, (b + c) & M64


def _weak32s(s: bytes, p: int, a: int, b: int):
    return _weak32(_f64(s, p), _f64(s, p + 8), _f64(s, p + 16), _f64(s, p + 24), a, b)


def cityhash64(s: bytes) -> int:
    n = len(s)
    if n <= 16:
        return _len0to16(s, n)
    if n <= 32:
        return _len17to32(s, n)
    if n <= 64:
        return _len33to64(s, n)

    x = _f64(s, n - 40)
    y = (_f64(s, n - 16) + _f64(s, n - 56)) & M64
    z = _hash16((_f64(s, n - 48) + n) & M64, _f64(s, n - 24))
    v = _weak32s(s, n - 64, n, z)
    w = _weak32s(s, n - 32, (y + K1) & M64, x)
    x = (x * K1 + _f64(s, 0)) & M64

    pos = 0
    left = (n - 1) & ~63
    while True:
        x = (_rot((x + y + v[0] + _f64(s, pos + 8)) & M64, 37) * K1) & M64
        y = (_rot((y + v[1] + _f64(s, pos + 48)) & M64, 42) * K1) & M64
        x ^= w[1]
        y = (y + v[0] + _f64(s, pos + 40)) & M64
        z = (_rot((z + w[0]) & M64, 33) * K1) & M64
        v = _weak32s(s, pos, (v[1] * K1) & M64, (x + w[0]) & M64)
        w = _weak32s(s, pos + 32, (z + w[1]) & M64, (y + _f64(s, pos + 16)) & M64)
        z, x = x, z
        pos += 64
        left -= 64
        if left == 0:
            break

    return _hash16(
        (_hash16(v[0], w[0]) + _shift_mix(y) * K1 + z) & M64,
        (_hash16(v[1], w[1]) + x) & M64,
    )


def hash_path(path: str, salt: int = 0) -> int:
    """Hash an archive path the way HashFS does: no leading slash, salt as decimal prefix."""
    if path.startswith("/"):
        path = path[1:]
    if salt:
        path = str(salt) + path
    return cityhash64(path.encode("utf-8"))
