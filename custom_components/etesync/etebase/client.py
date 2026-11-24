# SPDX-FileCopyrightText: © 2020 Etebase Authors
# SPDX-FileCopyrightText: © 2026 ha-etesync contributors
# SPDX-License-Identifier: LGPL-2.1-only
"""Async Etebase client (port of ``service.rs`` and ``online_managers.rs``)."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from concurrent.futures import Executor
from dataclasses import dataclass, field
from typing import Any

import aiohttp
from yarl import URL

from .crypto import (
    CURRENT_VERSION,
    SYMMETRIC_KEY_SIZE,
    CryptoManager,
    LoginCryptoManager,
    derive_key,
    from_base64,
    to_base64,
)
from .errors import (
    ConflictError,
    EncryptionError,
    EtebaseConnectionError,
    HttpError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    ServerError,
    TemporaryServerError,
    UnauthorizedError,
)
from .models import (
    ACCOUNT_CONTEXT,
    Collection,
    EncryptedCollection,
    Item,
    collection_type_uid,
    msgpack_decode,
    msgpack_encode,
)

DEFAULT_SERVER_URL = "https://api.etebase.com/partner/etesync/"
MAIN_CONTEXT = b"Main    "
STORAGE_CONTEXT = b"Stor    "
USER_NOT_INIT = "user_not_init"
_CONTENT_TYPE = "application/msgpack"
_REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=60)
_PAGE_LIMIT = 50


def normalize_server_url(server_url: str) -> str:
    """Validate a server URL and make sure its path ends with a slash."""
    url = URL(server_url.strip())
    if url.scheme not in ("http", "https") or not url.host:
        raise ValueError(f"Invalid server URL: {server_url}")
    path = url.raw_path if url.raw_path.endswith("/") else f"{url.raw_path}/"
    # Always rebuild the path: yarl renders a bare host without the slash.
    return str(url.with_path(path, encoded=True).with_query(None).with_fragment(None))


def _error_for_status(status: int, body: bytes) -> HttpError:
    code: str | None = None
    detail: str | None = None
    try:
        content = msgpack_decode(body)
    except ProtocolError:
        content = None
    if isinstance(content, dict):
        code = content.get("code") if isinstance(content.get("code"), str) else None
        detail = content.get("detail") if isinstance(content.get("detail"), str) else None

    error_class: type[HttpError]
    if 300 <= status < 400:
        return NotFoundError(status, "Got a redirect - should never happen", code)
    if status == 401:
        error_class, default = UnauthorizedError, "Unauthorized"
    elif status == 403:
        error_class, default = PermissionDeniedError, "PermissionDenied"
    elif status == 404:
        error_class, default = NotFoundError, "NotFound"
    elif status == 409:
        error_class, default = ConflictError, "Conflict"
    elif 502 <= status <= 504:
        error_class, default = TemporaryServerError, "Temporary server error"
    elif status >= 500:
        error_class, default = ServerError, "Server error"
    else:
        error_class, default = HttpError, f"HTTP error {status}"
    return error_class(status, detail or default, code)


class EtebaseClient:
    """Thin msgpack-over-HTTP transport bound to one server."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        server_url: str,
        *,
        client_name: str = "ha-etesync",
    ) -> None:
        """Create a client for ``server_url`` using an existing aiohttp session."""
        self._session = session
        self._client_name = client_name
        self.server_url = normalize_server_url(server_url)
        self.token: str | None = None

    @property
    def login_host(self) -> str:
        """Host name used in signed login responses (like the Rust ``host_str``)."""
        host = URL(self.server_url).raw_host or self.server_url
        return f"[{host}]" if ":" in host else host

    def url(self, path: str) -> URL:
        """Return an absolute URL for a path relative to the API base."""
        return URL(self.server_url).join(URL(path))

    async def request(
        self,
        method: str,
        path: str,
        *,
        body: Any = None,
        params: dict[str, str] | None = None,
        authenticated: bool = True,
    ) -> bytes:
        """Perform a request; raise an :class:`HttpError` for non 2xx answers."""
        headers = {
            "Accept": _CONTENT_TYPE,
            "Content-Type": _CONTENT_TYPE,
            "User-Agent": self._client_name,
        }
        if authenticated and self.token is not None:
            headers["Authorization"] = f"Token {self.token}"
        data = msgpack_encode(body) if body is not None else None
        try:
            async with self._session.request(
                method,
                self.url(path),
                data=data,
                params=params,
                headers=headers,
                allow_redirects=False,
                timeout=_REQUEST_TIMEOUT,
            ) as resp:
                content = await resp.read()
                status = resp.status
        except (aiohttp.ClientError, TimeoutError) as err:
            raise EtebaseConnectionError(f"Error talking to {self.server_url}: {err}") from err
        if not 200 <= status < 300:
            raise _error_for_status(status, content)
        return content

    async def request_msgpack(self, method: str, path: str, **kwargs: Any) -> Any:
        """Perform a request and decode the msgpack answer."""
        return msgpack_decode(await self.request(method, path, **kwargs))

    async def is_etebase_server(self) -> bool:
        """Whether the URL points at an Etebase server."""
        try:
            await self.request("GET", "api/v1/authentication/is_etebase/", authenticated=False)
        except NotFoundError:
            return False
        return True


@dataclass(slots=True)
class CollectionListResult:
    """Result of an (incremental) collection listing."""

    collections: list[Collection] = field(default_factory=list)
    removed_memberships: list[str] = field(default_factory=list)
    stoken: str | None = None


@dataclass(slots=True)
class ItemListResult:
    """Result of an (incremental) item listing."""

    items: list[Item] = field(default_factory=list)
    stoken: str | None = None


class Account:
    """An authenticated Etebase account."""

    def __init__(
        self,
        client: EtebaseClient,
        *,
        main_key: bytes,
        version: int,
        user: dict[str, Any],
    ) -> None:
        """Set up the account crypto from the main key."""
        self._client = client
        self._main_key = main_key
        self._version = version
        self._user = user
        main_cm = CryptoManager(main_key, MAIN_CONTEXT, version)
        content = main_cm.decrypt(bytes(user["encryptedContent"]))
        if len(content) < SYMMETRIC_KEY_SIZE:
            raise EncryptionError("Server's login response too short to contain account key")
        self._account_cm = CryptoManager(content[:SYMMETRIC_KEY_SIZE], ACCOUNT_CONTEXT, version)

    @property
    def username(self) -> str:
        """The canonical user name as returned by the server."""
        return str(self._user["username"])

    @property
    def server_url(self) -> str:
        """The normalized server URL."""
        return self._client.server_url

    @classmethod
    async def login(
        cls,
        client: EtebaseClient,
        username: str,
        password: str,
        *,
        executor: Executor | None = None,
    ) -> Account:
        """Log in with a password.

        The Argon2id key derivation runs in ``executor`` (default executor if None).
        Accounts that were never initialized are not signed up; the server's
        ``user_not_init`` error is raised instead.
        """
        challenge = await _get_login_challenge(client, username)
        salt = bytes(challenge["salt"])
        loop = asyncio.get_running_loop()
        main_key = await loop.run_in_executor(executor, derive_key, salt, password)
        version = int(challenge["version"])
        user = await _login(client, username, main_key, version, challenge)
        return cls(client, main_key=main_key, version=version, user=user)

    async def fetch_token(self) -> None:
        """Get a fresh auth token using the stored main key."""
        self._client.token = None
        challenge = await _get_login_challenge(self._client, self.username)
        user = await _login(self._client, self.username, self._main_key, self._version, challenge)
        self._user = user

    async def logout(self) -> None:
        """Invalidate the auth token on the server."""
        await self._client.request("POST", "api/v1/authentication/logout/")
        self._client.token = None

    def save(self) -> str:
        """Serialize the session (compatible with ``etebase.Account.save(None)``)."""
        storage_cm = CryptoManager(bytes(32), STORAGE_CONTEXT, CURRENT_VERSION)
        account_data = {
            "version": CURRENT_VERSION,
            "key": storage_cm.encrypt(self._main_key),
            "user": {
                "username": self._user["username"],
                "email": self._user["email"],
                "pubkey": bytes(self._user["pubkey"]),
                "encryptedContent": bytes(self._user["encryptedContent"]),
            },
            "serverUrl": self._client.server_url,
            "authToken": self._client.token,
        }
        stored = {
            "version": CURRENT_VERSION,
            "encryptedData": storage_cm.encrypt(
                msgpack_encode(account_data), bytes([CURRENT_VERSION])
            ),
        }
        return to_base64(msgpack_encode(stored))

    @classmethod
    def restore(cls, client: EtebaseClient, stored: str) -> Account:
        """Restore a session created by :meth:`save` (or ``etebase.Account.save(None)``)."""
        try:
            outer = msgpack_decode(from_base64(stored))
            version = int(outer["version"])
            storage_cm = CryptoManager(bytes(32), STORAGE_CONTEXT, version)
            data = msgpack_decode(
                storage_cm.decrypt(bytes(outer["encryptedData"]), bytes([version]))
            )
            main_key = storage_cm.decrypt(bytes(data["key"]))
            client.server_url = normalize_server_url(data["serverUrl"])
            client.token = data.get("authToken")
            return cls(client, main_key=main_key, version=version, user=data["user"])
        except (KeyError, TypeError, ValueError) as err:
            raise ProtocolError("Malformed stored session") from err

    async def list_collections(
        self, collection_types: Iterable[str], stoken: str | None = None
    ) -> CollectionListResult:
        """List collections of the given types changed since ``stoken`` (all pages).

        Legacy collections without an encrypted type are always returned by the
        server, so the result is filtered by the decrypted type as well.
        """
        types = list(collection_types)
        body = {"collectionTypes": [collection_type_uid(self._account_cm, t) for t in types]}
        result = CollectionListResult(stoken=stoken)
        while True:
            params = {"limit": str(_PAGE_LIMIT)}
            if result.stoken is not None:
                params["stoken"] = result.stoken
            response = await self._client.request_msgpack(
                "POST", "api/v1/collection/list_multi/", body=body, params=params
            )
            try:
                for raw in response["data"]:
                    collection = Collection(EncryptedCollection.from_wire(raw), self._account_cm)
                    if collection.collection_type in types:
                        result.collections.append(collection)
                result.removed_memberships.extend(
                    removed["uid"] for removed in response.get("removedMemberships") or []
                )
                result.stoken = response.get("stoken") or result.stoken
                done = bool(response["done"])
            except (KeyError, TypeError) as err:
                raise ProtocolError("Malformed collection list") from err
            if done:
                return result

    async def list_items(self, collection: Collection, stoken: str | None = None) -> ItemListResult:
        """List the items of a collection changed since ``stoken`` (all pages)."""
        result = ItemListResult(stoken=stoken)
        path = f"api/v1/collection/{collection.uid}/item/"
        while True:
            params = {"limit": str(_PAGE_LIMIT)}
            if result.stoken is not None:
                params["stoken"] = result.stoken
            response = await self._client.request_msgpack("GET", path, params=params)
            try:
                for raw in response["data"]:
                    item = collection.item_from_wire(raw)
                    if item.missing_content:
                        await self._download_content(collection, item)
                    result.items.append(item)
                result.stoken = response.get("stoken") or result.stoken
                done = bool(response["done"])
            except (KeyError, TypeError) as err:
                raise ProtocolError("Malformed item list") from err
            if done:
                return result

    async def fetch_item(self, collection: Collection, item_uid: str) -> Item:
        """Fetch the current revision of a single item."""
        raw = await self._client.request_msgpack(
            "GET", f"api/v1/collection/{collection.uid}/item/{item_uid}/"
        )
        item = collection.item_from_wire(raw)
        if item.missing_content:
            await self._download_content(collection, item)
        return item

    async def _download_content(self, collection: Collection, item: Item) -> None:
        chunks = item.encrypted.content.chunks
        for index, (chunk_uid, data) in enumerate(chunks):
            if data is None:
                content = await self._client.request(
                    "GET",
                    f"api/v1/collection/{collection.uid}/item/{item.uid}/chunk/{chunk_uid}/download/",
                )
                chunks[index] = (chunk_uid, content)

    async def transaction(self, collection: Collection, items: list[Item]) -> None:
        """Upload items; fails with :class:`ConflictError` if any changed remotely."""
        await self._client.request(
            "POST",
            f"api/v1/collection/{collection.uid}/item/transaction/",
            body={"items": [item.encrypted.to_wire() for item in items], "deps": None},
        )
        for item in items:
            item.mark_saved()


async def _get_login_challenge(client: EtebaseClient, username: str) -> dict[str, Any]:
    challenge = await client.request_msgpack(
        "POST",
        "api/v1/authentication/login_challenge/",
        body={"username": username},
        authenticated=False,
    )
    if not isinstance(challenge, dict) or not {"challenge", "salt", "version"} <= challenge.keys():
        raise ProtocolError("Malformed login challenge")
    return challenge


async def _login(
    client: EtebaseClient,
    username: str,
    main_key: bytes,
    version: int,
    challenge: dict[str, Any],
) -> dict[str, Any]:
    main_cm = CryptoManager(main_key, MAIN_CONTEXT, version)
    login_cm = LoginCryptoManager(main_cm.asym_key_seed)
    response = msgpack_encode(
        {
            "username": username,
            "challenge": bytes(challenge["challenge"]),
            "host": client.login_host,
            "action": "login",
        }
    )
    result = await client.request_msgpack(
        "POST",
        "api/v1/authentication/login/",
        body={"response": response, "signature": login_cm.sign_detached(response)},
        authenticated=False,
    )
    try:
        client.token = result["token"]
        user = result["user"]
        return {
            "username": user["username"],
            "email": user["email"],
            "pubkey": bytes(user["pubkey"]),
            "encryptedContent": bytes(user["encryptedContent"]),
        }
    except (KeyError, TypeError) as err:
        raise ProtocolError("Malformed login response") from err
