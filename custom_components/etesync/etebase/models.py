# SPDX-FileCopyrightText: © 2020 Etebase Authors
# SPDX-FileCopyrightText: © 2026 ha-etesync contributors
# SPDX-License-Identifier: LGPL-2.1-only
"""Encrypted Etebase objects and their decrypted views.

A Python port of ``encrypted_models.rs`` from etebase-rs.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from enum import IntEnum
from typing import Any

import msgpack

from .crypto import (
    CURRENT_VERSION,
    CryptoMac,
    CryptoManager,
    Rollsum,
    constant_time_eq,
    from_base64,
    pad,
    pad_fixed,
    pad_small,
    random_bytes,
    shuffle,
    to_base64,
    unpad,
    unpad_fixed,
)
from .errors import EncryptionError, ProtocolError

ACCOUNT_CONTEXT = b"Acct    "
COLLECTION_CONTEXT = b"Col     "
ITEM_CONTEXT = b"ColItem "
COLTYPE_PAD_SIZE = 32

_MIN_CHUNK = 1 << 14
_MAX_CHUNK = 1 << 16
_CHUNK_MASK = (1 << 12) - 1

type Chunk = tuple[str, bytes | None]


class AccessLevel(IntEnum):
    """Access level of the account to a collection."""

    READ_ONLY = 0
    ADMIN = 1
    READ_WRITE = 2


def msgpack_encode(obj: Any) -> bytes:
    """Serialize to msgpack the way the Etebase server expects."""
    return msgpack.packb(obj, use_bin_type=True)


def msgpack_decode(data: bytes) -> Any:
    """Deserialize msgpack sent by the Etebase server."""
    try:
        return msgpack.unpackb(data, raw=False)
    except (ValueError, msgpack.UnpackException) as err:
        raise ProtocolError("Failed decoding msgpack data") from err


def _decode_meta(raw: bytes) -> dict[str, Any]:
    meta = msgpack_decode(raw)
    if not isinstance(meta, dict):
        raise ProtocolError("Item metadata is not a map")
    return meta


def _chunk_content(crypto_manager: CryptoManager, content: bytes) -> list[Chunk]:
    """Split, deduplicate, shuffle and encrypt content into chunks."""
    chunks: list[tuple[str, bytes]] = []
    chunk_start = 0
    if len(content) > _MIN_CHUNK:
        chunker = Rollsum()
        for pos, byte in enumerate(content):
            chunker.update(byte)
            offset = pos - chunk_start
            if offset >= _MIN_CHUNK and (offset >= _MAX_CHUNK or chunker.split(_CHUNK_MASK)):
                buf = content[chunk_start:pos]
                chunks.append((to_base64(crypto_manager.calculate_mac(buf)), buf))
                chunk_start = pos
    if chunk_start < len(content):
        buf = content[chunk_start:]
        chunks.append((to_base64(crypto_manager.calculate_mac(buf)), buf))

    if chunks:
        indices = shuffle(chunks)
        uid_indices: dict[str, int] = {}
        unique: list[tuple[str, bytes]] = []
        for i, chunk in enumerate(chunks):
            previous = uid_indices.get(chunk[0])
            if previous is not None:
                indices[i] = previous
            else:
                uid_indices[chunk[0]] = i
                unique.append(chunk)
        chunks = unique
        if len(indices) > 1:
            header = msgpack_encode([indices])
            chunks.append((to_base64(crypto_manager.calculate_mac(header)), header))

    return [(uid, crypto_manager.encrypt(pad(buf))) for uid, buf in chunks]


@dataclass(slots=True)
class EncryptedRevision:
    """One revision of an item: encrypted metadata plus content chunks."""

    uid: str
    meta: bytes
    deleted: bool
    chunks: list[Chunk]

    @classmethod
    def create(
        cls,
        crypto_manager: CryptoManager,
        additional_data: bytes,
        meta: bytes,
        content: bytes,
    ) -> EncryptedRevision:
        """Encrypt a new revision."""
        rev = cls(uid="", meta=b"", deleted=False, chunks=[])
        rev.chunks = _chunk_content(crypto_manager, content)
        rev.set_meta(crypto_manager, additional_data, meta)
        return rev

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> EncryptedRevision:
        """Build from the server representation."""
        try:
            chunks: list[Chunk] = [
                (chunk[0], bytes(chunk[1]) if len(chunk) > 1 and chunk[1] is not None else None)
                for chunk in data["chunks"]
            ]
            return cls(
                uid=data["uid"],
                meta=bytes(data["meta"]),
                deleted=bool(data["deleted"]),
                chunks=chunks,
            )
        except (KeyError, TypeError, IndexError) as err:
            raise ProtocolError("Malformed item revision") from err

    def to_wire(self) -> dict[str, Any]:
        """Return the server representation."""
        return {
            "uid": self.uid,
            "meta": self.meta,
            "deleted": self.deleted,
            "chunks": [[uid, data] for uid, data in self.chunks],
        }

    def copy(self) -> EncryptedRevision:
        """Return a copy that can be modified independently."""
        return replace(self, chunks=list(self.chunks))

    def _calculate_hash(self, crypto_manager: CryptoManager, additional_data: bytes) -> bytes:
        mac = crypto_manager.crypto_mac()
        mac.update(bytes([self.deleted]))
        mac.update_with_len_prefix(additional_data)
        chunks_hash = CryptoMac(None)
        for uid, _ in self.chunks:
            chunks_hash.update(from_base64(uid))
        mac.update(chunks_hash.finalize())
        return mac.finalize()

    def get_meta(self, crypto_manager: CryptoManager, additional_data: bytes) -> bytes:
        """Verify the revision and return its decrypted metadata."""
        ad_hash = self._calculate_hash(crypto_manager, additional_data)
        return unpad(crypto_manager.decrypt_detached(self.meta, from_base64(self.uid), ad_hash))

    def set_meta(self, crypto_manager: CryptoManager, additional_data: bytes, meta: bytes) -> None:
        """Encrypt metadata; this also seals the chunk list and the deleted flag."""
        ad_hash = self._calculate_hash(crypto_manager, additional_data)
        tag, cipher = crypto_manager.encrypt_detached(pad_small(meta), ad_hash)
        self.uid = to_base64(tag)
        self.meta = cipher

    def set_content(
        self, crypto_manager: CryptoManager, additional_data: bytes, content: bytes
    ) -> None:
        """Replace the content, keeping the metadata."""
        meta = self.get_meta(crypto_manager, additional_data)
        self.chunks = _chunk_content(crypto_manager, content)
        self.set_meta(crypto_manager, additional_data, meta)

    def get_content(self, crypto_manager: CryptoManager) -> bytes:
        """Decrypt and reassemble the content."""
        decrypted: list[bytes] = []
        for uid, data in self.chunks:
            if data is None:
                raise ProtocolError("Got chunk without data")
            buf = unpad(crypto_manager.decrypt(data))
            if not constant_time_eq(from_base64(uid), crypto_manager.calculate_mac(buf)):
                raise EncryptionError("Got a wrong mac for chunk")
            decrypted.append(buf)

        if len(decrypted) > 1:
            header = msgpack_decode(decrypted.pop())
            try:
                indices: list[int] = header[0]
                return b"".join(decrypted[index] for index in indices)
            except (TypeError, IndexError, KeyError) as err:
                raise ProtocolError("Malformed chunk header") from err
        return decrypted[0] if decrypted else b""


@dataclass(slots=True)
class EncryptedItem:
    """An item as stored on the server."""

    uid: str
    version: int
    encryption_key: bytes | None
    content: EncryptedRevision
    # The revision uid last seen on (or saved to) the server, None for new items.
    last_etag: str | None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> EncryptedItem:
        """Build from the server representation (marks it as saved)."""
        try:
            content = EncryptedRevision.from_wire(data["content"])
            key = data.get("encryptionKey")
            return cls(
                uid=data["uid"],
                version=int(data["version"]),
                encryption_key=bytes(key) if key is not None else None,
                content=content,
                last_etag=content.uid,
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ProtocolError("Malformed item") from err

    def to_wire(self) -> dict[str, Any]:
        """Return the server representation."""
        return {
            "uid": self.uid,
            "version": self.version,
            "encryptionKey": self.encryption_key,
            "content": self.content.to_wire(),
            "etag": self.last_etag,
        }

    @property
    def additional_data(self) -> bytes:
        """Data authenticated together with every revision."""
        return self.uid.encode()

    def crypto_manager(self, parent: CryptoManager) -> CryptoManager:
        """Return the crypto manager of this item."""
        if self.encryption_key is not None:
            key = parent.decrypt(self.encryption_key)
        else:
            key = parent.derive_subkey(self.uid.encode())
        return CryptoManager(key, ITEM_CONTEXT, self.version)


class Item:
    """A decrypted view of an item that can be modified and uploaded."""

    def __init__(self, encrypted: EncryptedItem, crypto_manager: CryptoManager) -> None:
        """Wrap an encrypted item."""
        self.encrypted = encrypted
        self._cm = crypto_manager

    @classmethod
    def create(cls, parent: CryptoManager, meta: dict[str, Any], content: bytes) -> Item:
        """Create a new item encrypted for the collection of ``parent``."""
        uid = to_base64(random_bytes(24))
        crypto_manager = CryptoManager(
            parent.derive_subkey(uid.encode()), ITEM_CONTEXT, CURRENT_VERSION
        )
        revision = EncryptedRevision.create(
            crypto_manager, uid.encode(), msgpack_encode(meta), content
        )
        encrypted = EncryptedItem(
            uid=uid,
            version=CURRENT_VERSION,
            encryption_key=None,
            content=revision,
            last_etag=None,
        )
        return cls(encrypted, crypto_manager)

    def clone(self) -> Item:
        """Return a copy that can be modified without touching this item."""
        return Item(replace(self.encrypted, content=self.encrypted.content.copy()), self._cm)

    @property
    def uid(self) -> str:
        """The item uid (stable across revisions)."""
        return self.encrypted.uid

    @property
    def etag(self) -> str:
        """The uid of the current revision."""
        return self.encrypted.content.uid

    @property
    def deleted(self) -> bool:
        """Whether the item is marked as deleted."""
        return self.encrypted.content.deleted

    @property
    def missing_content(self) -> bool:
        """Whether some chunks still need to be downloaded."""
        return any(data is None for _, data in self.encrypted.content.chunks)

    @property
    def meta(self) -> dict[str, Any]:
        """The decrypted metadata."""
        return _decode_meta(
            self.encrypted.content.get_meta(self._cm, self.encrypted.additional_data)
        )

    @property
    def content(self) -> bytes:
        """The decrypted content."""
        self.encrypted.content.get_meta(self._cm, self.encrypted.additional_data)
        return self.encrypted.content.get_content(self._cm)

    def _new_revision(self) -> EncryptedRevision:
        revision = self.encrypted.content.copy()
        self.encrypted.content = revision
        return revision

    def update(self, meta: dict[str, Any], content: bytes) -> None:
        """Replace metadata and content in a new revision."""
        revision = self._new_revision()
        revision.chunks = _chunk_content(self._cm, content)
        revision.set_meta(self._cm, self.encrypted.additional_data, msgpack_encode(meta))

    def delete(self, meta: dict[str, Any]) -> None:
        """Mark the item deleted in a new revision (the content is kept)."""
        revision = self._new_revision()
        revision.deleted = True
        revision.set_meta(self._cm, self.encrypted.additional_data, msgpack_encode(meta))

    def mark_saved(self) -> None:
        """Record that the current revision is now on the server."""
        self.encrypted.last_etag = self.encrypted.content.uid


@dataclass(slots=True)
class EncryptedCollection:
    """A collection as returned by the server."""

    item: EncryptedItem
    access_level: int
    collection_key: bytes
    collection_type: bytes | None
    stoken: str | None

    @classmethod
    def from_wire(cls, data: dict[str, Any]) -> EncryptedCollection:
        """Build from the server representation."""
        try:
            collection_type = data.get("collectionType")
            return cls(
                item=EncryptedItem.from_wire(data["item"]),
                access_level=int(data["accessLevel"]),
                collection_key=bytes(data["collectionKey"]),
                collection_type=bytes(collection_type) if collection_type is not None else None,
                stoken=data.get("stoken"),
            )
        except (KeyError, TypeError, ValueError) as err:
            raise ProtocolError("Malformed collection") from err


class Collection:
    """A decrypted view of a collection."""

    def __init__(self, encrypted: EncryptedCollection, account_cm: CryptoManager) -> None:
        """Decrypt the collection key, type and metadata."""
        self.encrypted = encrypted
        key = account_cm.decrypt(encrypted.collection_key, encrypted.collection_type)
        self.crypto_manager = CryptoManager(key, COLLECTION_CONTEXT, encrypted.item.version)
        main_item = Item(encrypted.item, encrypted.item.crypto_manager(self.crypto_manager))
        self.meta = main_item.meta
        if encrypted.collection_type is not None:
            raw_type = unpad_fixed(
                account_cm.deterministic_decrypt(encrypted.collection_type), COLTYPE_PAD_SIZE
            )
            self.collection_type = raw_type.decode(errors="replace")
        else:
            self.collection_type = str(self.meta.get("type", ""))

    @property
    def uid(self) -> str:
        """The collection uid."""
        return self.encrypted.item.uid

    @property
    def stoken(self) -> str | None:
        """Sync token of the collection."""
        return self.encrypted.stoken

    @property
    def deleted(self) -> bool:
        """Whether the collection was deleted."""
        return self.encrypted.item.content.deleted

    @property
    def access_level(self) -> AccessLevel | int:
        """The access level of this account."""
        try:
            return AccessLevel(self.encrypted.access_level)
        except ValueError:
            return self.encrypted.access_level

    @property
    def read_only(self) -> bool:
        """Whether the account may not modify items."""
        return self.access_level == AccessLevel.READ_ONLY

    def item_from_wire(self, data: dict[str, Any]) -> Item:
        """Wrap an item of this collection returned by the server."""
        encrypted = EncryptedItem.from_wire(data)
        return Item(encrypted, encrypted.crypto_manager(self.crypto_manager))

    def create_item(self, meta: dict[str, Any], content: bytes) -> Item:
        """Create a new item in this collection (not uploaded yet)."""
        return Item.create(self.crypto_manager, meta, content)


def collection_type_uid(account_cm: CryptoManager, collection_type: str) -> bytes:
    """Return the encrypted identifier the server uses to filter collection types."""
    return account_cm.deterministic_encrypt(pad_fixed(collection_type.encode(), COLTYPE_PAD_SIZE))
