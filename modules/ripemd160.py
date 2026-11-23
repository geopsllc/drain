"""Pure-Python RIPEMD-160, used only when hashlib can't provide it.

arkecosystem-crypto derives addresses with hashlib.new('ripemd160', ...). On systems whose
OpenSSL 3 doesn't enable the legacy provider (e.g. Ubuntu 22.04 with Python 3.10) that raises
"unsupported hash type ripemd160". install() then routes just that algorithm here.
Implementation follows the RIPEMD-160 specification (structure as in Bitcoin Core's test framework).
"""
import hashlib

ML = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15,
      7, 4, 13, 1, 10, 6, 15, 3, 12, 0, 9, 5, 2, 14, 11, 8,
      3, 10, 14, 4, 9, 15, 8, 1, 2, 7, 0, 6, 13, 11, 5, 12,
      1, 9, 11, 10, 0, 8, 12, 4, 13, 3, 7, 15, 14, 5, 6, 2,
      4, 0, 5, 9, 7, 12, 2, 10, 14, 1, 3, 8, 11, 6, 15, 13]
MR = [5, 14, 7, 0, 9, 2, 11, 4, 13, 6, 15, 8, 1, 10, 3, 12,
      6, 11, 3, 7, 0, 13, 5, 10, 14, 15, 8, 12, 4, 9, 1, 2,
      15, 5, 1, 3, 7, 14, 6, 9, 11, 8, 12, 2, 10, 0, 4, 13,
      8, 6, 4, 1, 3, 11, 15, 0, 5, 12, 2, 13, 9, 7, 10, 14,
      12, 15, 10, 4, 1, 5, 8, 7, 6, 2, 13, 14, 0, 3, 9, 11]
RL = [11, 14, 15, 12, 5, 8, 7, 9, 11, 13, 14, 15, 6, 7, 9, 8,
      7, 6, 8, 13, 11, 9, 7, 15, 7, 12, 15, 9, 11, 7, 13, 12,
      11, 13, 6, 7, 14, 9, 13, 15, 14, 8, 13, 6, 5, 12, 7, 5,
      11, 12, 14, 15, 14, 15, 9, 8, 9, 14, 5, 6, 8, 6, 5, 12,
      9, 15, 5, 11, 6, 8, 13, 12, 5, 12, 13, 14, 11, 8, 5, 6]
RR = [8, 9, 9, 11, 13, 15, 15, 5, 7, 7, 8, 11, 14, 14, 12, 6,
      9, 13, 15, 7, 12, 8, 9, 11, 7, 7, 12, 7, 6, 15, 13, 11,
      9, 7, 15, 11, 8, 6, 6, 14, 12, 13, 5, 14, 13, 13, 7, 5,
      15, 5, 8, 11, 14, 14, 6, 14, 6, 9, 12, 9, 12, 5, 15, 8,
      8, 5, 12, 9, 12, 5, 14, 6, 8, 13, 6, 5, 15, 13, 11, 11]
KL = [0x00000000, 0x5a827999, 0x6ed9eba1, 0x8f1bbcdc, 0xa953fd4e]
KR = [0x50a28be6, 0x5c4dd124, 0x6d703ef3, 0x7a6d76e9, 0x00000000]
MASK = 0xffffffff


def _f(x, y, z, i):
    if i == 0:
        return x ^ y ^ z
    if i == 1:
        return (x & y) | (~x & z)
    if i == 2:
        return (x | ~y) ^ z
    if i == 3:
        return (x & z) | (y & ~z)
    return x ^ (y | ~z)


def _rol(x, i):
    x &= MASK
    return ((x << i) | (x >> (32 - i))) & MASK


def _compress(h0, h1, h2, h3, h4, block):
    al, bl, cl, dl, el = h0, h1, h2, h3, h4
    ar, br, cr, dr, er = h0, h1, h2, h3, h4
    x = [int.from_bytes(block[4 * i:4 * i + 4], 'little') for i in range(16)]
    for j in range(80):
        rnd = j >> 4
        al = (_rol(al + _f(bl, cl, dl, rnd) + x[ML[j]] + KL[rnd], RL[j]) + el) & MASK
        al, bl, cl, dl, el = el, al, bl, _rol(cl, 10), dl
        ar = (_rol(ar + _f(br, cr, dr, 4 - rnd) + x[MR[j]] + KR[rnd], RR[j]) + er) & MASK
        ar, br, cr, dr, er = er, ar, br, _rol(cr, 10), dr
    return ((h1 + cl + dr) & MASK, (h2 + dl + er) & MASK, (h3 + el + ar) & MASK,
            (h4 + al + br) & MASK, (h0 + bl + cr) & MASK)


def ripemd160(data):
    data = bytes(data)
    state = (0x67452301, 0xefcdab89, 0x98badcfe, 0x10325476, 0xc3d2e1f0)
    full = len(data) & ~63
    for i in range(0, full, 64):
        state = _compress(*state, data[i:i + 64])
    tail = data[full:] + b'\x80' + b'\x00' * ((119 - len(data)) & 63) + (8 * len(data)).to_bytes(8, 'little')
    for i in range(0, len(tail), 64):
        state = _compress(*state, tail[i:i + 64])
    return b''.join(h.to_bytes(4, 'little') for h in state)


class RIPEMD160:
    name = 'ripemd160'
    digest_size = 20
    block_size = 64

    def __init__(self, data=b''):
        self._data = bytearray(data)

    def update(self, data):
        self._data += data

    def digest(self):
        return ripemd160(self._data)

    def hexdigest(self):
        return self.digest().hex()

    def copy(self):
        return RIPEMD160(self._data)


def install():
    """Make hashlib.new('ripemd160') work if the system's hashlib refuses it. Returns True if patched."""
    try:
        hashlib.new('ripemd160', b'')
        return False
    except ValueError:
        pass
    original = hashlib.new

    def new(name, data=b'', **kwargs):
        if str(name).lower().replace('-', '') == 'ripemd160':
            return RIPEMD160(data)
        return original(name, data, **kwargs)

    hashlib.new = new
    return True
