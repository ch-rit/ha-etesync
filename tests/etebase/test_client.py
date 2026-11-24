"""Unit tests for the Etebase HTTP client helpers."""

from __future__ import annotations

from unittest.mock import MagicMock

import msgpack
import pytest

from custom_components.etesync.etebase import (
    ConflictError,
    EtebaseClient,
    HttpError,
    NotFoundError,
    PermissionDeniedError,
    ServerError,
    TemporaryServerError,
    UnauthorizedError,
    normalize_server_url,
)
from custom_components.etesync.etebase.client import _error_for_status


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://api.example.com", "https://api.example.com/"),
        ("https://api.example.com/", "https://api.example.com/"),
        (" https://Example.com/partner/etesync ", "https://example.com/partner/etesync/"),
        ("http://[::1]:8033", "http://[::1]:8033/"),
        ("https://h.example/a%20b?x=1#y", "https://h.example/a%20b/"),
    ],
)
def test_normalize_server_url(url: str, expected: str) -> None:
    """URLs get exactly one trailing slash and keep their path prefix."""
    assert normalize_server_url(url) == expected


@pytest.mark.parametrize("url", ["ftp://example.com", "example.com", "https://"])
def test_normalize_server_url_invalid(url: str) -> None:
    """Non-http URLs are rejected."""
    with pytest.raises(ValueError, match="Invalid server URL"):
        normalize_server_url(url)


def test_url_join_keeps_prefix() -> None:
    """API paths are joined below the configured prefix."""
    client = EtebaseClient(MagicMock(), "https://example.com/partner/etesync")
    assert str(client.url("api/v1/authentication/login/")) == (
        "https://example.com/partner/etesync/api/v1/authentication/login/"
    )
    assert EtebaseClient(MagicMock(), "http://[::1]:8033/").login_host == "[::1]"


@pytest.mark.parametrize(
    ("status", "error_class"),
    [
        (302, NotFoundError),
        (400, HttpError),
        (401, UnauthorizedError),
        (403, PermissionDeniedError),
        (404, NotFoundError),
        (409, ConflictError),
        (500, ServerError),
        (502, TemporaryServerError),
        (504, TemporaryServerError),
    ],
)
def test_error_for_status(status: int, error_class: type[HttpError]) -> None:
    """HTTP statuses map to the same errors as in etebase-rs."""
    assert type(_error_for_status(status, b"<html>bad gateway</html>")) is error_class


def test_error_details_are_decoded() -> None:
    """Server error codes and details are kept."""
    body = msgpack.packb({"code": "user_not_init", "detail": "User not properly init"})
    error = _error_for_status(401, body)
    assert isinstance(error, UnauthorizedError)
    assert error.code == "user_not_init"
    assert str(error) == "User not properly init"
