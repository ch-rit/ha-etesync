# SPDX-FileCopyrightText: © 2026 ha-etesync contributors
# SPDX-License-Identifier: LGPL-2.1-only
"""Minimal pure-Python Etebase client.

The official ``etebase`` package is a Rust extension without musllinux wheels,
so it cannot be installed on Home Assistant OS. This package implements the
subset of the protocol needed to sync task lists on top of PyNaCl.
"""

from .client import (
    DEFAULT_SERVER_URL,
    USER_NOT_INIT,
    Account,
    CollectionListResult,
    EtebaseClient,
    ItemListResult,
    normalize_server_url,
)
from .errors import (
    ConflictError,
    EncryptionError,
    EtebaseConnectionError,
    EtebaseError,
    HttpError,
    NotFoundError,
    PermissionDeniedError,
    ProtocolError,
    ServerError,
    TemporaryServerError,
    UnauthorizedError,
)
from .models import AccessLevel, Collection, Item

__all__ = [
    "DEFAULT_SERVER_URL",
    "USER_NOT_INIT",
    "AccessLevel",
    "Account",
    "Collection",
    "CollectionListResult",
    "ConflictError",
    "EncryptionError",
    "EtebaseClient",
    "EtebaseConnectionError",
    "EtebaseError",
    "HttpError",
    "Item",
    "ItemListResult",
    "NotFoundError",
    "PermissionDeniedError",
    "ProtocolError",
    "ServerError",
    "TemporaryServerError",
    "UnauthorizedError",
    "normalize_server_url",
]
