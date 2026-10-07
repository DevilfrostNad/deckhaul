"""Minimal pure-Python AES-256-CBC, enough to open and re-seal ScsC profile files.

SteamOS ships Python without third-party crypto packages and its root
filesystem is read-only, so we avoid any dependency. Speed is fine:
profile.sii is a few hundred kilobytes at most.
"""

from __future__ import annotations

from typing import List


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x1B) & 0xFF if a & 0x100 else a


def _build_sbox():
    sbox = [0] * 256
    p = q = 1
    while True:
        # multiply p by 3
        p = p ^ _xtime(p)
        # divide q by 3
        q ^= q << 1
        q ^= q << 2
        q ^= q << 4
        q &= 0xFF
        if q & 0x80:
            q ^= 0x09
        x = q ^ ((q << 1) | (q >> 7)) & 0xFF ^ ((q << 2) | (q >> 6)) & 0xFF \
            ^ ((q << 3) | (q >> 5)) & 0xFF ^ ((q << 4) | (q >> 4)) & 0xFF
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    inv = [0] * 256
    for i, v in enumerate(sbox):
        inv[v] = i
    return sbox, inv


SBOX, INV_SBOX = _build_sbox()


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


_M9 = [_mul(i, 9) for i in range(256)]
_M11 = [_mul(i, 11) for i in range(256)]
_M13 = [_mul(i, 13) for i in range(256)]
_M14 = [_mul(i, 14) for i in range(256)]
_M2 = [_mul(i, 2) for i in range(256)]
_M3 = [_mul(i, 3) for i in range(256)]


def _expand_key(key: bytes) -> List[List[int]]:
    if len(key) != 32:
        raise ValueError("AES-256 key must be 32 bytes")
    nk, nr = 8, 14
    w = [list(key[i : i + 4]) for i in range(0, 32, 4)]
    rcon = 1
    for i in range(nk, 4 * (nr + 1)):
        t = list(w[i - 1])
        if i % nk == 0:
            t = t[1:] + t[:1]
            t = [SBOX[b] for b in t]
            t[0] ^= rcon
            rcon = _xtime(rcon)
        elif i % nk == 4:
            t = [SBOX[b] for b in t]
        w.append([w[i - nk][j] ^ t[j] for j in range(4)])
    # one flat 16-byte list per round
    return [sum(w[r * 4 : r * 4 + 4], []) for r in range(nr + 1)]


def _shift_rows(s):
    return [s[0], s[5], s[10], s[15], s[4], s[9], s[14], s[3],
            s[8], s[13], s[2], s[7], s[12], s[1], s[6], s[11]]


def _inv_shift_rows(s):
    return [s[0], s[13], s[10], s[7], s[4], s[1], s[14], s[11],
            s[8], s[5], s[2], s[15], s[12], s[9], s[6], s[3]]


def _encrypt_block(rk, block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[0])]
    for r in range(1, 14):
        s = _shift_rows([SBOX[b] for b in s])
        m = []
        for c in range(4):
            a0, a1, a2, a3 = s[c * 4 : c * 4 + 4]
            m += [
                _M2[a0] ^ _M3[a1] ^ a2 ^ a3,
                a0 ^ _M2[a1] ^ _M3[a2] ^ a3,
                a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
                _M3[a0] ^ a1 ^ a2 ^ _M2[a3],
            ]
        s = [b ^ k for b, k in zip(m, rk[r])]
    s = _shift_rows([SBOX[b] for b in s])
    return bytes(b ^ k for b, k in zip(s, rk[14]))


def _decrypt_block(rk, block: bytes) -> bytes:
    s = [b ^ k for b, k in zip(block, rk[14])]
    for r in range(13, 0, -1):
        s = [INV_SBOX[b] for b in _inv_shift_rows(s)]
        s = [b ^ k for b, k in zip(s, rk[r])]
        m = []
        for c in range(4):
            a0, a1, a2, a3 = s[c * 4 : c * 4 + 4]
            m += [
                _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3],
            ]
        s = m
    s = [INV_SBOX[b] for b in _inv_shift_rows(s)]
    return bytes(b ^ k for b, k in zip(s, rk[0]))


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data) - len(data) % 16, 16):
        block = data[i : i + 16]
        plain = _decrypt_block(rk, block)
        out += bytes(a ^ b for a, b in zip(plain, prev))
        prev = block
    return bytes(out)


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    if len(data) % 16:
        raise ValueError("data must be padded to 16 bytes")
    rk = _expand_key(key)
    out = bytearray()
    prev = iv
    for i in range(0, len(data), 16):
        block = bytes(a ^ b for a, b in zip(data[i : i + 16], prev))
        prev = _encrypt_block(rk, block)
        out += prev
    return bytes(out)
