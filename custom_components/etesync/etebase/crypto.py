# SPDX-FileCopyrightText: © 2020 Etebase Authors
# SPDX-FileCopyrightText: © 2026 ha-etesync contributors
# SPDX-License-Identifier: LGPL-2.1-only
"""Cryptographic primitives of the Etebase protocol.

A Python port of ``crypto.rs`` and ``utils.rs`` from etebase-rs, built on
PyNaCl (libsodium) and the BLAKE2b implementation of :mod:`hashlib`.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import struct

from nacl import bindings, pwhash, signing
from nacl.exceptions import CryptoError

from .errors import EncryptionError, ProtocolError

CURRENT_VERSION = 1
SALT_SIZE = 16
SYMMETRIC_KEY_SIZE = 32
SYMMETRIC_TAG_SIZE = 16
SYMMETRIC_NONCE_SIZE = 24
HASH_SIZE = 32

# Argon2id parameters used by every Etebase client to derive the main key.
_PWHASH_OPSLIMIT = pwhash.argon2id.OPSLIMIT_SENSITIVE
_PWHASH_MEMLIMIT = pwhash.argon2id.MEMLIMIT_MODERATE


def random_bytes(size: int) -> bytes:
    """Return ``size`` cryptographically secure random bytes."""
    return secrets.token_bytes(size)


def to_base64(data: bytes) -> str:
    """Encode bytes as unpadded URL-safe base64."""
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def from_base64(data: str) -> bytes:
    """Decode unpadded URL-safe base64."""
    try:
        return base64.b64decode(data + "=" * (-len(data) % 4), altchars=b"-_", validate=True)
    except (binascii.Error, ValueError) as err:
        raise ProtocolError("Failed decoding base64 string") from err


def generichash(msg: bytes, key: bytes | None = None) -> bytes:
    """BLAKE2b-256, optionally keyed (libsodium ``crypto_generichash``)."""
    return hashlib.blake2b(msg, digest_size=HASH_SIZE, key=key or b"").digest()


def kdf_derive(key: bytes, subkey_id: int, context: bytes) -> bytes:
    """Derive a 32 byte subkey (libsodium ``crypto_kdf_derive_from_key``)."""
    return hashlib.blake2b(
        digest_size=SYMMETRIC_KEY_SIZE,
        key=key,
        salt=struct.pack("<Q", subkey_id),
        person=context,
    ).digest()


def derive_key(salt: bytes, password: str) -> bytes:
    """Derive the account main key from the password.

    This is deliberately expensive (Argon2id, 256 MiB); run it in an executor.
    """
    return pwhash.argon2id.kdf(
        SYMMETRIC_KEY_SIZE,
        password.encode(),
        salt[:SALT_SIZE],
        opslimit=_PWHASH_OPSLIMIT,
        memlimit=_PWHASH_MEMLIMIT,
    )


def get_padding(length: int) -> int:
    """Return the padded size for a buffer (padmé, minimum 1 KiB blocks)."""
    if length < (1 << 14):
        return (length | ((1 << 10) - 1)) + 1
    exponent = length.bit_length() - 1
    size_bits = exponent.bit_length()
    bit_mask = (1 << (exponent - size_bits)) - 1
    return (length + bit_mask) & ~bit_mask


def pad_fixed(buf: bytes, blocksize: int) -> bytes:
    """ISO/IEC 7816-4 padding to a multiple of ``blocksize`` (``sodium_pad``)."""
    missing = blocksize - (len(buf) % blocksize)
    return buf + b"\x80" + b"\x00" * (missing - 1)


def unpad_fixed(buf: bytes, blocksize: int) -> bytes:
    """Reverse :func:`pad_fixed` (``sodium_unpad``)."""
    if not buf:
        return b""
    if blocksize <= 0 or len(buf) < blocksize:
        raise EncryptionError("Failed unpadding")
    limit = len(buf) - blocksize
    pos = len(buf) - 1
    while pos >= limit and buf[pos] == 0:
        pos -= 1
    if pos < limit or buf[pos] != 0x80:
        raise EncryptionError("Failed unpadding")
    return buf[:pos]


def pad(buf: bytes) -> bytes:
    """Pad content chunks to hide their exact size."""
    return pad_fixed(buf, get_padding(len(buf)))


def pad_small(buf: bytes) -> bytes:
    """Pad metadata (a single padding byte)."""
    return pad_fixed(buf, len(buf) + 1)


def unpad(buf: bytes) -> bytes:
    """Remove padding added by :func:`pad` or :func:`pad_small`."""
    return unpad_fixed(buf, len(buf))


def _aead_encrypt(msg: bytes, additional_data: bytes | None, nonce: bytes, key: bytes) -> bytes:
    return bindings.crypto_aead_xchacha20poly1305_ietf_encrypt(msg, additional_data, nonce, key)


def _aead_decrypt(cipher: bytes, additional_data: bytes | None, nonce: bytes, key: bytes) -> bytes:
    try:
        return bindings.crypto_aead_xchacha20poly1305_ietf_decrypt(
            cipher, additional_data, nonce, key
        )
    except CryptoError as err:
        raise EncryptionError("decryption failed") from err


def _split_nonce(cipher: bytes) -> tuple[bytes, bytes]:
    if len(cipher) < SYMMETRIC_NONCE_SIZE:
        raise EncryptionError("Got a nonce of a wrong size")
    return cipher[:SYMMETRIC_NONCE_SIZE], cipher[SYMMETRIC_NONCE_SIZE:]


class CryptoMac:
    """Incremental (optionally keyed) BLAKE2b-256."""

    def __init__(self, key: bytes | None) -> None:
        """Start a new hash."""
        self._state = hashlib.blake2b(digest_size=HASH_SIZE, key=key or b"")

    def update(self, msg: bytes) -> None:
        """Feed bytes into the hash."""
        self._state.update(msg)

    def update_with_len_prefix(self, msg: bytes) -> None:
        """Feed bytes prefixed by their little-endian u32 length."""
        self._state.update(struct.pack("<I", len(msg)))
        self._state.update(msg)

    def finalize(self) -> bytes:
        """Return the digest."""
        return self._state.digest()


class CryptoManager:
    """Symmetric crypto bound to a key and an 8 byte context."""

    def __init__(self, key: bytes, context: bytes, version: int) -> None:
        """Derive the sub keys for ``context`` from ``key``."""
        if len(key) != SYMMETRIC_KEY_SIZE:
            raise EncryptionError("Encryption key has wrong length")
        self.version = version
        self._cipher_key = kdf_derive(key, 1, context)
        self._mac_key = kdf_derive(key, 2, context)
        self.asym_key_seed = kdf_derive(key, 3, context)
        self._sub_derivation_key = kdf_derive(key, 4, context)
        self._deterministic_key = kdf_derive(key, 5, context)

    def encrypt(self, msg: bytes, additional_data: bytes | None = None) -> bytes:
        """Encrypt with a random nonce; returns ``nonce || ciphertext || tag``."""
        nonce = random_bytes(SYMMETRIC_NONCE_SIZE)
        return nonce + _aead_encrypt(msg, additional_data, nonce, self._cipher_key)

    def decrypt(self, cipher: bytes, additional_data: bytes | None = None) -> bytes:
        """Reverse :meth:`encrypt`."""
        nonce, body = _split_nonce(cipher)
        return _aead_decrypt(body, additional_data, nonce, self._cipher_key)

    def encrypt_detached(
        self, msg: bytes, additional_data: bytes | None = None
    ) -> tuple[bytes, bytes]:
        """Encrypt returning ``(tag, nonce || ciphertext)``."""
        nonce = random_bytes(SYMMETRIC_NONCE_SIZE)
        combined = _aead_encrypt(msg, additional_data, nonce, self._cipher_key)
        return combined[-SYMMETRIC_TAG_SIZE:], nonce + combined[:-SYMMETRIC_TAG_SIZE]

    def decrypt_detached(
        self, cipher: bytes, tag: bytes, additional_data: bytes | None = None
    ) -> bytes:
        """Reverse :meth:`encrypt_detached`."""
        if len(tag) != SYMMETRIC_TAG_SIZE:
            raise EncryptionError("Got a tag of a wrong size")
        nonce, body = _split_nonce(cipher)
        return _aead_decrypt(body + tag, additional_data, nonce, self._cipher_key)

    def deterministic_encrypt(self, msg: bytes, additional_data: bytes | None = None) -> bytes:
        """Encrypt with a nonce derived from the message MAC."""
        nonce = self.calculate_mac(msg)[:SYMMETRIC_NONCE_SIZE]
        return nonce + _aead_encrypt(msg, additional_data, nonce, self._deterministic_key)

    def deterministic_decrypt(self, cipher: bytes, additional_data: bytes | None = None) -> bytes:
        """Reverse :meth:`deterministic_encrypt`."""
        nonce, body = _split_nonce(cipher)
        return _aead_decrypt(body, additional_data, nonce, self._deterministic_key)

    def derive_subkey(self, salt: bytes) -> bytes:
        """Derive a per-item key."""
        return generichash(self._sub_derivation_key, salt)

    def crypto_mac(self) -> CryptoMac:
        """Return an incremental MAC keyed with this manager's MAC key."""
        return CryptoMac(self._mac_key)

    def calculate_mac(self, msg: bytes) -> bytes:
        """Return the keyed MAC of ``msg``."""
        return generichash(msg, self._mac_key)


class LoginCryptoManager:
    """Ed25519 key pair used to answer login challenges."""

    def __init__(self, seed: bytes) -> None:
        """Create the key pair from a 32 byte seed."""
        self._signing_key = signing.SigningKey(seed)

    @property
    def pubkey(self) -> bytes:
        """The public key."""
        return bytes(self._signing_key.verify_key)

    def sign_detached(self, msg: bytes) -> bytes:
        """Return the detached signature of ``msg``."""
        return self._signing_key.sign(msg).signature


def constant_time_eq(left: bytes, right: bytes) -> bool:
    """Compare two byte strings in constant time."""
    return hmac.compare_digest(left, right)


class Rollsum:
    """Rolling checksum used for content-defined chunking (from bup/librsync)."""

    WINDOW_SIZE = 64
    CHAR_OFFSET = 31
    _MASK32 = 0xFFFFFFFF

    def __init__(self) -> None:
        """Start a fresh rolling window."""
        self._s1 = self.WINDOW_SIZE * self.CHAR_OFFSET
        self._s2 = self.WINDOW_SIZE * (self.WINDOW_SIZE - 1) * self.CHAR_OFFSET
        self._window = bytearray(self.WINDOW_SIZE)
        self._wofs = 0

    def update(self, byte: int) -> None:
        """Roll one byte into the window."""
        drop = self._window[self._wofs]
        self._s1 = (self._s1 + byte - drop) & self._MASK32
        self._s2 = (
            self._s2 + self._s1 - self.WINDOW_SIZE * (drop + self.CHAR_OFFSET)
        ) & self._MASK32
        self._window[self._wofs] = byte
        self._wofs = (self._wofs + 1) % self.WINDOW_SIZE

    def split(self, mask: int) -> bool:
        """Whether the current position is a chunk boundary."""
        return self._s2 & mask == mask


def shuffle[T](items: list[T]) -> list[int]:
    """Shuffle ``items`` in place; return the new index of every original item."""
    length = len(items)
    shuffled = list(range(length))
    for i in range(length):
        j = i + secrets.randbelow(length - i)
        items[i], items[j] = items[j], items[i]
        shuffled[i], shuffled[j] = shuffled[j], shuffled[i]
    ret = [0] * length
    for i in range(length):
        ret[shuffled[i]] = i
    return ret
