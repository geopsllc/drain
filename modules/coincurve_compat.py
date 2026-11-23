"""Pure-Python stand-in for the two coincurve classes arkecosystem-crypto uses.

coincurve has no wheels for Python 3.14 (e.g. Ubuntu 26.04, Debian testing) and fails to build
from source there. arkecosystem-crypto only needs it to derive a compressed public key from a
private key and to parse a public key; Schnorr signing is already pure Python in that library.
This module provides exactly that on top of the library's own secp256k1 point maths, and
drain.py installs it as `coincurve` only when the real package can't be imported.

Not constant-time, like the library's Schnorr code; fine for signing on your own machine.
"""
from binascii import hexlify, unhexlify

from crypto.schnorr.schnorr import G, encoded_from_point, n, p, point_from_encoded, point_mul

__version__ = 'compat'


class PublicKey:
    def __init__(self, data):
        data = bytes(data)
        if len(data) == 33 and data[0] in (2, 3):
            self._point = point_from_encoded(data)
        elif len(data) == 65 and data[0] == 4:
            x, y = int.from_bytes(data[1:33], 'big'), int.from_bytes(data[33:], 'big')
            if (y * y - x * x * x - 7) % p:
                raise ValueError('point not on the secp256k1 curve')
            self._point = [x, y]
        else:
            raise ValueError('public key must be 33 bytes compressed or 65 bytes uncompressed')

    @classmethod
    def from_point(cls, point):
        return cls(encoded_from_point(point))

    def format(self, compressed=True):
        if compressed:
            return encoded_from_point(self._point)
        return b'\x04' + self._point[0].to_bytes(32, 'big') + self._point[1].to_bytes(32, 'big')


class PrivateKey:
    def __init__(self, secret):
        k = int.from_bytes(bytes(secret), 'big')
        if len(secret) != 32 or not 0 < k < n:
            raise ValueError('private key must be 32 bytes and between 1 and n-1')
        self._k = k
        self.secret = bytes(secret)
        self.public_key = PublicKey.from_point(point_mul(G, k))

    @classmethod
    def from_hex(cls, hexed):
        return cls(unhexlify(hexed))

    def to_hex(self):
        return hexlify(self.secret).decode()

    def sign(self, message, hasher=None):
        raise NotImplementedError('ECDSA signing is not available without coincurve; drain only uses Schnorr')
