"""The EteSync integration: EteSync task lists as Home Assistant to-do lists."""

from __future__ import annotations

from homeassistant.const import CONF_URL, Platform, __version__ as ha_version
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .const import CONF_SESSION, DOMAIN, LOGGER
from .coordinator import EteSyncConfigEntry, EteSyncCoordinator
from .etebase import Account, EtebaseClient, EtebaseError

PLATFORMS: list[Platform] = [Platform.TODO]


def create_client(hass: HomeAssistant, server_url: str) -> EtebaseClient:
    """Create an Etebase client using the shared aiohttp session."""
    return EtebaseClient(
        async_get_clientsession(hass),
        server_url,
        client_name=f"HomeAssistant/{ha_version} ha-etesync",
    )


async def async_setup_entry(hass: HomeAssistant, entry: EteSyncConfigEntry) -> bool:
    """Set up an EteSync account."""
    try:
        account = Account.restore(
            create_client(hass, entry.data[CONF_URL]), entry.data[CONF_SESSION]
        )
    except EtebaseError as err:
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN, translation_key="invalid_session"
        ) from err
    coordinator = EteSyncCoordinator(hass, entry, account)
    await coordinator.async_config_entry_first_refresh()
    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EteSyncConfigEntry) -> bool:
    """Unload an EteSync account."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)


async def async_remove_entry(hass: HomeAssistant, entry: EteSyncConfigEntry) -> None:
    """Invalidate the session token when the account is removed."""
    try:
        account = Account.restore(
            create_client(hass, entry.data[CONF_URL]), entry.data[CONF_SESSION]
        )
        await account.logout()
    except EtebaseError as err:
        LOGGER.debug("Could not log out %s: %s", entry.title, err)
