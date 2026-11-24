"""Interop tests against a real Etebase server and the official Rust client.

Start a server with ``scripts/run_test_server.sh`` and set ``ETEBASE_TEST_SERVER``
(for example ``http://127.0.0.1:8033``). The official ``etebase`` wheel must be
installed; these tests are skipped otherwise.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
import os
import secrets
import time
from typing import Any

import aiohttp
import pytest

from custom_components.etesync.etebase import (
    Account,
    ConflictError,
    EtebaseClient,
    UnauthorizedError,
)

SERVER = os.environ.get("ETEBASE_TEST_SERVER", "")
PASSWORD = "correct horse battery staple"

pytestmark = [
    pytest.mark.live_server,
    pytest.mark.skipif(not SERVER, reason="ETEBASE_TEST_SERVER not set"),
    pytest.mark.usefixtures("socket_enabled"),
]

etebase = pytest.importorskip("etebase")


def _vtodo(uid: str, summary: str) -> bytes:
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nPRODID:-//test//EN\r\n"
        f"BEGIN:VTODO\r\nUID:{uid}\r\nSUMMARY:{summary}\r\nSTATUS:NEEDS-ACTION\r\n"
        "END:VTODO\r\nEND:VCALENDAR\r\n"
    ).encode()


@dataclass
class OfficialUser:
    """A user created with the official client."""

    username: str
    account: Any
    collection: Any


@pytest.fixture
def official() -> OfficialUser:
    """Sign up a fresh user with the official client and add a task list."""
    client = etebase.Client("ha-etesync-tests", SERVER)
    username = f"test_user_{secrets.token_hex(6)}"
    account = etebase.Account.signup(
        client, etebase.User(username, f"{username}@example.com"), PASSWORD
    )
    col_mgr = account.get_collection_manager()
    collection = col_mgr.create("etebase.vtodo", {"name": "Groceries", "color": "#8BC34A"}, b"")
    col_mgr.upload(collection)
    # A calendar must not show up as a task list.
    col_mgr.upload(col_mgr.create("etebase.vevent", {"name": "Calendar"}, b""))
    item_mgr = col_mgr.get_item_manager(collection)
    item = item_mgr.create(
        {"name": "milk-uid", "mtime": int(time.time() * 1000)}, _vtodo("milk-uid", "Milk")
    )
    item_mgr.batch([item])
    return OfficialUser(username, account, collection)


@pytest.fixture
async def session() -> AsyncIterator[aiohttp.ClientSession]:
    """Yield an aiohttp session."""
    async with aiohttp.ClientSession() as client_session:
        yield client_session


async def test_roundtrip_with_official_client(
    official: OfficialUser, session: aiohttp.ClientSession
) -> None:
    """Data written by either client is readable by the other."""
    # Sessions saved by the official client can be restored.
    account = Account.restore(EtebaseClient(session, SERVER), official.account.save(None))
    assert account.username == official.username
    await account.fetch_token()

    listing = await account.list_collections(["etebase.vtodo"])
    assert [c.meta["name"] for c in listing.collections] == ["Groceries"]
    collection = listing.collections[0]
    assert collection.uid == official.collection.uid
    assert collection.meta["color"] == "#8BC34A"
    assert not collection.read_only

    items = await account.list_items(collection)
    assert len(items.items) == 1
    milk = items.items[0]
    assert milk.meta["name"] == "milk-uid"
    assert b"SUMMARY:Milk" in milk.content

    # Incremental sync returns nothing new.
    assert (await account.list_items(collection, items.stoken)).items == []
    assert (await account.list_collections(["etebase.vtodo"], listing.stoken)).collections == []

    # Update, create (including multi-chunk content) and delete with our client.
    milk.update({**milk.meta, "mtime": 1}, _vtodo("milk-uid", "Oat milk"))
    bread = collection.create_item({"name": "bread-uid", "mtime": 2}, _vtodo("bread-uid", "Bread"))
    big = _vtodo("big-uid", "Big " + "x" * 100_000)
    large = collection.create_item({"name": "big-uid", "mtime": 3}, big)
    assert len(large.encrypted.content.chunks) > 2
    await account.transaction(collection, [milk, bread, large])
    bread.delete({**bread.meta, "mtime": 4})
    await account.transaction(collection, [bread])

    changes = await account.list_items(collection, items.stoken)
    assert {item.uid for item in changes.items} == {milk.uid, bread.uid, large.uid}
    assert (await account.list_collections(["etebase.vtodo"], listing.stoken)).collections

    # Stale revisions are rejected.
    stale_milk = next(i for i in (await account.list_items(collection)).items if i.uid == milk.uid)
    milk.update(milk.meta, _vtodo("milk-uid", "Soy milk"))
    await account.transaction(collection, [milk])
    stale_milk.update(stale_milk.meta, _vtodo("milk-uid", "Rice milk"))
    with pytest.raises(ConflictError):
        await account.transaction(collection, [stale_milk])

    # The official client sees exactly what we wrote.
    col_mgr = official.account.get_collection_manager()
    item_mgr = col_mgr.get_item_manager(col_mgr.fetch(collection.uid))
    seen = {item.uid: item for item in item_mgr.list().data}
    assert b"SUMMARY:Soy milk" in bytes(seen[milk.uid].content)
    assert seen[bread.uid].deleted
    assert bytes(seen[large.uid].content) == big
    assert seen[large.uid].meta == {"name": "big-uid", "mtime": 3}

    # The official client can restore our sessions.
    restored = etebase.Account.restore(
        etebase.Client("ha-etesync-tests", SERVER), account.save(), None
    )
    assert len(list(restored.get_collection_manager().list("etebase.vtodo").data)) == 1

    # A revoked token is rejected with 401 and fetch_token() recovers the session.
    saved = account.save()
    await account.logout()
    revoked = Account.restore(EtebaseClient(session, SERVER), saved)
    with pytest.raises(UnauthorizedError):
        await revoked.list_items(collection)
    await revoked.fetch_token()
    assert len((await revoked.list_items(collection)).items) == 3


async def test_password_login(official: OfficialUser, session: aiohttp.ClientSession) -> None:
    """Password login works and a wrong password is rejected."""
    account = await Account.login(EtebaseClient(session, SERVER), official.username, PASSWORD)
    assert account.username == official.username
    listing = await account.list_collections(["etebase.vtodo"])
    assert len(listing.collections) == 1

    with pytest.raises(UnauthorizedError):
        await Account.login(EtebaseClient(session, SERVER), official.username, "wrong password")
