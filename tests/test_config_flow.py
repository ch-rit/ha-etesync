"""Tests for the EteSync config flow."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.etesync.const import CONF_SESSION, DOMAIN
from custom_components.etesync.etebase import (
    DEFAULT_SERVER_URL,
    Account,
    EtebaseClient,
    EtebaseConnectionError,
    NotFoundError,
    ProtocolError,
    UnauthorizedError,
)
from homeassistant.config_entries import SOURCE_USER
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from .fake_etebase import SERVER_URL, USERNAME, FakeAccount

USER_INPUT = {CONF_URL: "https://etebase.example.com", CONF_USERNAME: "Alice", CONF_PASSWORD: "pw"}


@pytest.fixture(autouse=True)
def mock_setup_entry() -> Generator[AsyncMock]:
    """Do not set up the entry after the flow."""
    with patch("custom_components.etesync.async_setup_entry", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_server() -> Generator[AsyncMock]:
    """Pretend every URL is an Etebase server."""
    with patch.object(EtebaseClient, "is_etebase_server", return_value=True) as mock:
        yield mock


@pytest.fixture
def mock_login(fake_account: FakeAccount, mock_server: AsyncMock) -> Generator[AsyncMock]:
    """Make logins succeed with the fake account."""
    with patch.object(Account, "login", return_value=fake_account) as mock:
        yield mock


async def test_user_flow(hass: HomeAssistant, mock_login: AsyncMock) -> None:
    """A successful login creates an entry with the session, not the password."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {}

    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "alice (etebase.example.com)"
    assert result["data"] == {
        CONF_URL: SERVER_URL,
        CONF_USERNAME: USERNAME,
        CONF_SESSION: "session-1",
    }
    assert result["result"].unique_id == f"{SERVER_URL}|{USERNAME}"
    client, username, password = mock_login.call_args.args
    assert client.server_url == SERVER_URL
    assert (username, password) == ("Alice", "pw")


async def test_default_server_title(hass: HomeAssistant, mock_server: AsyncMock) -> None:
    """Accounts on the hosted service are titled with the username only."""
    account = FakeAccount(server_url=DEFAULT_SERVER_URL)
    with patch.object(Account, "login", return_value=account):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {**USER_INPUT, CONF_URL: DEFAULT_SERVER_URL}
        )
    assert result["title"] == USERNAME


@pytest.mark.parametrize(
    ("error", "errors"),
    [
        (
            UnauthorizedError(401, "Wrong password for user.", "login_bad_signature"),
            {"base": "invalid_auth"},
        ),
        (
            UnauthorizedError(401, "User not properly init", "user_not_init"),
            {"base": "account_not_initialized"},
        ),
        (EtebaseConnectionError("boom"), {"base": "cannot_connect"}),
        (NotFoundError(404, "NotFound"), {CONF_URL: "not_etebase_server"}),
        (ProtocolError("garbage"), {"base": "unknown"}),
    ],
)
async def test_login_errors(
    hass: HomeAssistant,
    mock_login: AsyncMock,
    error: Exception,
    errors: dict[str, str],
) -> None:
    """Login errors are shown and the flow can recover."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    mock_login.side_effect = error
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == errors

    mock_login.side_effect = None
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_not_etebase_server(
    hass: HomeAssistant, mock_login: AsyncMock, mock_server: AsyncMock
) -> None:
    """URLs that are no Etebase server are rejected before logging in."""
    mock_server.return_value = False
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["errors"] == {CONF_URL: "not_etebase_server"}
    mock_login.assert_not_called()


async def test_invalid_url(hass: HomeAssistant, mock_login: AsyncMock) -> None:
    """Non-http URLs are rejected."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**USER_INPUT, CONF_URL: "ftp://example.com"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    mock_login.assert_not_called()


async def test_already_configured(
    hass: HomeAssistant, mock_login: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """The same account cannot be added twice."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_second_account(
    hass: HomeAssistant, mock_server: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Additional accounts can be added."""
    with patch.object(Account, "login", return_value=FakeAccount(username="bob")):
        result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
        result = await hass.config_entries.flow.async_configure(result["flow_id"], USER_INPUT)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(hass.config_entries.async_entries(DOMAIN)) == 2


async def test_reauth(
    hass: HomeAssistant,
    mock_login: AsyncMock,
    fake_account: FakeAccount,
    config_entry: MockConfigEntry,
) -> None:
    """Re-authentication stores a fresh session."""
    result = await config_entry.start_reauth_flow(hass)
    assert result["step_id"] == "reauth_confirm"

    mock_login.side_effect = UnauthorizedError(401, "Wrong password for user.")
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "wrong"}
    )
    assert result["errors"] == {"base": "invalid_auth"}

    mock_login.side_effect = None
    fake_account.session = "fresh-session"
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {CONF_PASSWORD: "right"}
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reauth_successful"
    assert config_entry.data[CONF_SESSION] == "fresh-session"
    assert mock_login.call_args.args[1:] == (USERNAME, "right")


async def test_reauth_wrong_account(
    hass: HomeAssistant, mock_server: AsyncMock, config_entry: MockConfigEntry
) -> None:
    """Re-authenticating as somebody else is refused."""
    result = await config_entry.start_reauth_flow(hass)
    with patch.object(Account, "login", return_value=FakeAccount(username="mallory")):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"], {CONF_PASSWORD: "pw"}
        )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_account"
    assert config_entry.data[CONF_SESSION] == "session-1"
