"""An in-memory Etebase account built on the real crypto objects."""

from __future__ import annotations

from dataclasses import dataclass, field
import itertools

from custom_components.etesync.etebase import (
    AccessLevel,
    Collection,
    CollectionListResult,
    ConflictError,
    Item,
    ItemListResult,
    NotFoundError,
)
from custom_components.etesync.etebase.crypto import CryptoManager, random_bytes
from custom_components.etesync.etebase.models import (
    ACCOUNT_CONTEXT,
    COLLECTION_CONTEXT,
    EncryptedCollection,
    collection_type_uid,
)

SERVER_URL = "https://etebase.example.com/"
USERNAME = "alice"


def vtodo(uid: str, summary: str, extra: str = "") -> bytes:
    """Return a minimal VCALENDAR with one VTODO."""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"BEGIN:VTODO\r\nUID:{uid}\r\nSUMMARY:{summary}\r\n{extra}"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    ).encode()


@dataclass
class _StoredCollection:
    collection: Collection
    stoken: int
    items: dict[str, tuple[Item, int]] = field(default_factory=dict)


class FakeAccount:
    """Implements the subset of ``Account`` used by the integration."""

    def __init__(self, username: str = USERNAME, server_url: str = SERVER_URL) -> None:
        """Create an empty account."""
        self.username = username
        self.server_url = server_url
        self._account_cm = CryptoManager(random_bytes(32), ACCOUNT_CONTEXT, 1)
        self._counter = itertools.count(1)
        self._collections: dict[str, _StoredCollection] = {}
        self._removed_memberships: list[tuple[str, int]] = []
        self.session = "session-1"
        self.fetch_token_calls = 0
        self.logged_out = False
        # Exceptions raised by the next call to list_collections, in order.
        self.list_errors: list[Exception] = []
        self.fetch_token_error: Exception | None = None

    # -- helpers for tests -------------------------------------------------

    def add_collection(
        self,
        name: str,
        collection_type: str = "etebase.vtodo",
        access_level: AccessLevel = AccessLevel.ADMIN,
    ) -> Collection:
        """Create a collection as another client would."""
        type_uid = collection_type_uid(self._account_cm, collection_type)
        key = random_bytes(32)
        col_cm = CryptoManager(key, COLLECTION_CONTEXT, 1)
        main_item = Item.create(col_cm, {"name": name}, b"")
        main_item.mark_saved()
        encrypted = EncryptedCollection(
            item=main_item.encrypted,
            access_level=int(access_level),
            collection_key=self._account_cm.encrypt(key, type_uid),
            collection_type=type_uid,
            stoken=None,
        )
        collection = Collection(encrypted, self._account_cm)
        self._collections[collection.uid] = _StoredCollection(collection, next(self._counter))
        return collection

    def delete_collection(self, collection: Collection) -> None:
        """Delete a collection as another client would."""
        stored = self._collections[collection.uid]
        stored.collection.encrypted.item.content.deleted = True
        stored.stoken = next(self._counter)

    def remove_membership(self, collection: Collection) -> None:
        """Simulate losing access to a shared collection."""
        del self._collections[collection.uid]
        self._removed_memberships.append((collection.uid, next(self._counter)))

    def put_item(self, collection: Collection, content: bytes, name: str | None) -> Item:
        """Create or replace an item as another client would."""
        item = collection.create_item({"name": name} if name else {}, content)
        self._store(collection.uid, item)
        return item

    def edit_item(self, collection: Collection, uid: str, content: bytes) -> None:
        """Change an item as another client would."""
        item = self.item(collection, uid).clone()
        item.update(item.meta, content)
        self._store(collection.uid, item)

    def remote_delete_item(self, collection: Collection, uid: str) -> None:
        """Delete an item as another client would."""
        item = self.item(collection, uid).clone()
        item.delete(item.meta)
        self._store(collection.uid, item)

    def item(self, collection: Collection, uid: str) -> Item:
        """Return the stored item."""
        return self._collections[collection.uid].items[uid][0]

    def items(self, collection: Collection) -> list[Item]:
        """Return all stored items, including deleted ones."""
        return [item for item, _ in self._collections[collection.uid].items.values()]

    def _store(self, col_uid: str, item: Item) -> None:
        item = item.clone()
        item.mark_saved()
        stoken = next(self._counter)
        stored = self._collections[col_uid]
        stored.items[item.uid] = (item, stoken)
        stored.stoken = stoken

    # -- Account API -------------------------------------------------------

    def save(self) -> str:
        """Return the current session."""
        return self.session

    async def fetch_token(self) -> None:
        """Pretend to fetch a new token."""
        self.fetch_token_calls += 1
        if self.fetch_token_error is not None:
            raise self.fetch_token_error
        self.session = f"session-{self.fetch_token_calls + 1}"

    async def logout(self) -> None:
        """Pretend to log out."""
        self.logged_out = True

    async def list_collections(
        self, collection_types: list[str], stoken: str | None = None
    ) -> CollectionListResult:
        """Return collections changed since ``stoken``."""
        if self.list_errors:
            raise self.list_errors.pop(0)
        since = int(stoken or 0)
        return CollectionListResult(
            collections=[
                stored.collection
                for stored in self._collections.values()
                if stored.stoken > since and stored.collection.collection_type in collection_types
            ],
            removed_memberships=[uid for uid, at in self._removed_memberships if at > since],
            stoken=str(next(self._counter)),
        )

    async def list_items(self, collection: Collection, stoken: str | None = None) -> ItemListResult:
        """Return items changed since ``stoken``."""
        since = int(stoken or 0)
        stored = self._collections[collection.uid]
        return ItemListResult(
            items=[item.clone() for item, at in stored.items.values() if at > since],
            stoken=str(next(self._counter)),
        )

    async def fetch_item(self, collection: Collection, item_uid: str) -> Item:
        """Return the current revision of an item."""
        try:
            return self.item(collection, item_uid).clone()
        except KeyError as err:
            raise NotFoundError(404, "NotFound") from err

    async def transaction(self, collection: Collection, items: list[Item]) -> None:
        """Store items unless one of them changed in the meantime."""
        stored = self._collections[collection.uid]
        for item in items:
            current = stored.items.get(item.uid)
            expected = current[0].etag if current else None
            if item.encrypted.last_etag != expected:
                raise ConflictError(409, "Wrong etag", "wrong_etag")
        for item in items:
            self._store(collection.uid, item)
            item.mark_saved()
