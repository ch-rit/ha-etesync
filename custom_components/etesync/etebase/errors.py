# SPDX-FileCopyrightText: © 2026 ha-etesync contributors
# SPDX-License-Identifier: LGPL-2.1-only
"""Exceptions raised by the Etebase client."""

from __future__ import annotations


class EtebaseError(Exception):
    """Base class for all Etebase errors."""


class EtebaseConnectionError(EtebaseError):
    """The server could not be reached."""


class ProtocolError(EtebaseError):
    """The server sent data the client could not understand."""


class EncryptionError(EtebaseError):
    """Decryption or integrity verification failed."""


class HttpError(EtebaseError):
    """The server answered with an unexpected HTTP status."""

    def __init__(self, status: int, detail: str, code: str | None = None) -> None:
        """Store the status, error code and detail."""
        super().__init__(detail)
        self.status = status
        self.code = code
        self.detail = detail


class UnauthorizedError(HttpError):
    """401: the token or the credentials were rejected."""


class PermissionDeniedError(HttpError):
    """403: the account may not perform this operation."""


class NotFoundError(HttpError):
    """404 (or a redirect): the resource does not exist."""


class ConflictError(HttpError):
    """409: the resource changed on the server."""


class TemporaryServerError(HttpError):
    """502-504: the server is temporarily unavailable."""


class ServerError(HttpError):
    """Other 5xx errors."""
