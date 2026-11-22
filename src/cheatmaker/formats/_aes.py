# Adapted from ReaperPy aes_cbc.py (https://github.com/Trexxxo/ReaperPy).
#
# MIT License
#
# Copyright (c) 2026 ReaperPy contributors
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
# SOFTWARE.
#
# Changes for Cheat Maker: English messages and docstrings, type hints, strict
# zips, a strict PKCS#7 unpad, round keys flattened without sum(), formatted for
# ruff. The algorithm is unchanged.
"""AES (128/192/256) in CBC mode, pure Python (FIPS-197).

Only used for ``.mc4`` cheat files (base64 of AES-256-CBC). An mc4 file is a few KB, so
speed doesn't matter, and pure Python keeps a native dependency out of the app
(architecture-pro decision 2). Tested against the NIST SP 800-38A vectors.
"""

from __future__ import annotations

BLOCK = 16


def _build_sbox() -> list[int]:
    """The S-box from GF(2^8) inverses (instead of 256 typed-in constants)."""
    sbox = [0] * 256
    p = q = 1
    while True:
        # p *= 3 in GF(2^8)
        p = p ^ ((p << 1) & 0xFF) ^ (0x1B if p & 0x80 else 0)
        # q /= 3 in GF(2^8)
        q ^= (q << 1) & 0xFF
        q ^= (q << 2) & 0xFF
        q ^= (q << 4) & 0xFF
        if q & 0x80:
            q ^= 0x09
        x = (
            q
            ^ ((q << 1) | (q >> 7)) & 0xFF
            ^ ((q << 2) | (q >> 6)) & 0xFF
            ^ ((q << 3) | (q >> 5)) & 0xFF
            ^ ((q << 4) | (q >> 4)) & 0xFF
        )
        sbox[p] = (x ^ 0x63) & 0xFF
        if p == 1:
            break
    sbox[0] = 0x63
    return sbox


_SBOX = _build_sbox()
_INV = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV[_v] = _i


def _xtime(a: int) -> int:
    a <<= 1
    return (a ^ 0x11B) & 0xFF if a & 0x100 else a


def _mul(a: int, b: int) -> int:
    r = 0
    while b:
        if b & 1:
            r ^= a
        a = _xtime(a)
        b >>= 1
    return r


_M2 = [_mul(i, 2) for i in range(256)]
_M3 = [_mul(i, 3) for i in range(256)]
_M9 = [_mul(i, 9) for i in range(256)]
_M11 = [_mul(i, 11) for i in range(256)]
_M13 = [_mul(i, 13) for i in range(256)]
_M14 = [_mul(i, 14) for i in range(256)]


class AES:
    def __init__(self, key: bytes) -> None:
        key = bytes(key)
        if len(key) not in (16, 24, 32):
            raise ValueError("an AES key is 16, 24 or 32 bytes")
        self.nk = len(key) // 4
        self.nr = self.nk + 6
        self.rk = self._expand(key)

    def _expand(self, key: bytes) -> list[list[int]]:
        nk, nr = self.nk, self.nr
        w = [list(key[4 * i : 4 * i + 4]) for i in range(nk)]
        rcon = 1
        for i in range(nk, 4 * (nr + 1)):
            t = list(w[i - 1])
            if i % nk == 0:
                t = t[1:] + t[:1]
                t = [_SBOX[b] for b in t]
                t[0] ^= rcon
                rcon = _xtime(rcon)
            elif nk > 6 and i % nk == 4:
                t = [_SBOX[b] for b in t]
            w.append([a ^ b for a, b in zip(w[i - nk], t, strict=True)])
        # 16-byte round keys
        return [[b for c in range(4) for b in w[4 * r + c]] for r in range(nr + 1)]

    # The state is 16 bytes, column by column as in the standard: s[4*c + r].
    def encrypt_block(self, block: bytes) -> bytes:
        s = [b ^ k for b, k in zip(block, self.rk[0], strict=True)]
        for rnd in range(1, self.nr):
            s = [_SBOX[b] for b in s]
            s = self._shift(s)
            s = self._mix(s)
            s = [b ^ k for b, k in zip(s, self.rk[rnd], strict=True)]
        s = [_SBOX[b] for b in s]
        s = self._shift(s)
        return bytes(b ^ k for b, k in zip(s, self.rk[self.nr], strict=True))

    def decrypt_block(self, block: bytes) -> bytes:
        s = [b ^ k for b, k in zip(block, self.rk[self.nr], strict=True)]
        for rnd in range(self.nr - 1, 0, -1):
            s = self._ishift(s)
            s = [_INV[b] for b in s]
            s = [b ^ k for b, k in zip(s, self.rk[rnd], strict=True)]
            s = self._imix(s)
        s = self._ishift(s)
        s = [_INV[b] for b in s]
        return bytes(b ^ k for b, k in zip(s, self.rk[0], strict=True))

    @staticmethod
    def _shift(s: list[int]) -> list[int]:
        return [s[(4 * ((c + r) % 4)) + r] for c in range(4) for r in range(4)]

    @staticmethod
    def _ishift(s: list[int]) -> list[int]:
        return [s[(4 * ((c - r) % 4)) + r] for c in range(4) for r in range(4)]

    @staticmethod
    def _mix(s: list[int]) -> list[int]:
        out: list[int] = []
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
            out += [
                _M2[a0] ^ _M3[a1] ^ a2 ^ a3,
                a0 ^ _M2[a1] ^ _M3[a2] ^ a3,
                a0 ^ a1 ^ _M2[a2] ^ _M3[a3],
                _M3[a0] ^ a1 ^ a2 ^ _M2[a3],
            ]
        return out

    @staticmethod
    def _imix(s: list[int]) -> list[int]:
        out: list[int] = []
        for c in range(4):
            a0, a1, a2, a3 = s[4 * c : 4 * c + 4]
            out += [
                _M14[a0] ^ _M11[a1] ^ _M13[a2] ^ _M9[a3],
                _M9[a0] ^ _M14[a1] ^ _M11[a2] ^ _M13[a3],
                _M13[a0] ^ _M9[a1] ^ _M14[a2] ^ _M11[a3],
                _M11[a0] ^ _M13[a1] ^ _M9[a2] ^ _M14[a3],
            ]
        return out


def _check(iv: bytes, data: bytes) -> None:
    if len(iv) != BLOCK:
        raise ValueError("the IV is 16 bytes")
    if len(data) % BLOCK:
        raise ValueError("CBC data must be a multiple of 16 bytes")


def cbc_encrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    """``data`` must be a multiple of 16 bytes (pad it first, ``pkcs7_pad``)."""
    _check(iv, data)
    aes, prev, out = AES(key), bytes(iv), bytearray()
    for i in range(0, len(data), BLOCK):
        blk = bytes(a ^ b for a, b in zip(data[i : i + BLOCK], prev, strict=True))
        prev = aes.encrypt_block(blk)
        out += prev
    return bytes(out)


def cbc_decrypt(key: bytes, iv: bytes, data: bytes) -> bytes:
    _check(iv, data)
    aes, prev, out = AES(key), bytes(iv), bytearray()
    for i in range(0, len(data), BLOCK):
        blk = bytes(data[i : i + BLOCK])
        out += bytes(a ^ b for a, b in zip(aes.decrypt_block(blk), prev, strict=True))
        prev = blk
    return bytes(out)


def pkcs7_pad(data: bytes, n: int = BLOCK) -> bytes:
    k = n - len(data) % n
    return bytes(data) + bytes([k]) * k


def pkcs7_unpad(data: bytes, n: int = BLOCK) -> bytes:
    """Remove PKCS#7 padding. Raises ValueError when it isn't valid padding."""
    k = data[-1] if data else 0
    if not 1 <= k <= n or len(data) % n or not data.endswith(bytes([k]) * k):
        raise ValueError("bad PKCS#7 padding")
    return bytes(data[:-k])
