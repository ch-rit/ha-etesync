"""Config flow for EteSync."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from yarl import URL

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_PASSWORD, CONF_URL, CONF_USERNAME
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from . import create_client
from .const import CONF_SESSION, DOMAIN, LOGGER
from .etebase import (
    DEFAULT_SERVER_URL,
    USER_NOT_INIT,
    Account,
    EtebaseConnectionError,
    EtebaseError,
    NotFoundError,
    TemporaryServerError,
    UnauthorizedError,
    normalize_server_url,
)

_URL_SELECTOR = TextSelector(TextSelectorConfig(type=TextSelectorType.URL))
_USERNAME_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.TEXT, autocomplete="username")
)
_PASSWORD_SELECTOR = TextSelector(
    TextSelectorConfig(type=TextSelectorType.PASSWORD, autocomplete="current-password")
)


def _unique_id(account: Account) -> str:
    return f"{account.server_url}|{account.username}"


def _title(account: Account) -> str:
    if account.server_url == DEFAULT_SERVER_URL:
        return account.username
    return f"{account.username} ({URL(account.server_url).host})"


class EteSyncConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for EteSync accounts."""

    VERSION = 1

    async def _async_login(
        self, server_url: str, username: str, password: str
    ) -> tuple[Account | None, dict[str, str]]:
        """Log in; return the account or the form errors."""
        try:
            client = create_client(self.hass, normalize_server_url(server_url))
        except ValueError:
            return None, {CONF_URL: "invalid_url"}
        try:
            if not await client.is_etebase_server():
                return None, {CONF_URL: "not_etebase_server"}
            account = await Account.login(client, username, password)
        except UnauthorizedError as err:
            if err.code == USER_NOT_INIT:
                return None, {"base": "account_not_initialized"}
            return None, {"base": "invalid_auth"}
        except NotFoundError:
            return None, {CONF_URL: "not_etebase_server"}
        except EtebaseConnectionError, TemporaryServerError:
            return None, {"base": "cannot_connect"}
        except EtebaseError:
            LOGGER.exception("Unexpected error logging in to %s", server_url)
            return None, {"base": "unknown"}
        return account, {}

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Ask for the server and credentials."""
        errors: dict[str, str] = {}
        if user_input is not None:
            account, errors = await self._async_login(
                user_input[CONF_URL], user_input[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if account is not None:
                await self.async_set_unique_id(_unique_id(account))
                self._abort_if_unique_id_configured()
                return self.async_create_entry(
                    title=_title(account),
                    data={
                        CONF_URL: account.server_url,
                        CONF_USERNAME: account.username,
                        CONF_SESSION: account.save(),
                    },
                )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        vol.Required(CONF_URL): _URL_SELECTOR,
                        vol.Required(CONF_USERNAME): _USERNAME_SELECTOR,
                        vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR,
                    }
                ),
                user_input or {CONF_URL: DEFAULT_SERVER_URL},
            ),
            errors=errors,
        )

    async def async_step_reauth(self, entry_data: Mapping[str, Any]) -> ConfigFlowResult:
        """Start re-authentication after the session became invalid."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the password again."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}
        if user_input is not None:
            account, errors = await self._async_login(
                entry.data[CONF_URL], entry.data[CONF_USERNAME], user_input[CONF_PASSWORD]
            )
            if account is not None:
                await self.async_set_unique_id(_unique_id(account))
                self._abort_if_unique_id_mismatch(reason="wrong_account")
                return self.async_update_reload_and_abort(
                    entry, data_updates={CONF_SESSION: account.save()}
                )
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema({vol.Required(CONF_PASSWORD): _PASSWORD_SELECTOR}),
            description_placeholders={
                CONF_USERNAME: entry.data[CONF_USERNAME],
                CONF_URL: entry.data[CONF_URL],
            },
            errors=errors,
        )
