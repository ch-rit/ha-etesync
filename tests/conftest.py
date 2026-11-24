"""Fixtures for the EteSync tests."""

from __future__ import annotations

from collections.abc import Generator
from unittest.mock import patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.etesync.const import CONF_SESSION, DOMAIN
from custom_components.etesync.etebase import Account
from homeassistant.const import CONF_URL, CONF_USERNAME
from homeassistant.core import HomeAssistant

from .fake_etebase import SERVER_URL, USERNAME, FakeAccount


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations: None) -> None:
    """Enable loading custom integrations in all tests."""


@pytest.fixture
def fake_account() -> FakeAccount:
    """Return an empty in-memory account."""
    return FakeAccount()


@pytest.fixture
def mock_restore(fake_account: FakeAccount) -> Generator[None]:
    """Make restoring the stored session return the fake account."""
    with patch.object(Account, "restore", return_value=fake_account):
        yield


@pytest.fixture
def config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a config entry for the fake account."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=USERNAME,
        unique_id=f"{SERVER_URL}|{USERNAME}",
        data={CONF_URL: SERVER_URL, CONF_USERNAME: USERNAME, CONF_SESSION: "session-1"},
    )
    entry.add_to_hass(hass)
    return entry
