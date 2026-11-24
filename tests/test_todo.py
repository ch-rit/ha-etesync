"""Tests for setting up EteSync accounts and their to-do list entities."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

from icalendar import Calendar
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import ClientSessionGenerator

from custom_components.etesync.const import CONF_SESSION, DOMAIN
from custom_components.etesync.etebase import (
    AccessLevel,
    Account,
    Collection,
    EtebaseConnectionError,
    ProtocolError,
    UnauthorizedError,
)
from homeassistant.components.todo import (
    DOMAIN as TODO_DOMAIN,
    TodoListEntityFeature,
    TodoServices,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import ATTR_ENTITY_ID, ATTR_SUPPORTED_FEATURES, STATE_UNAVAILABLE
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.setup import async_setup_component

from .fake_etebase import FakeAccount, vtodo

GROCERIES = "todo.alice_groceries"


@pytest.fixture
def groceries(fake_account: FakeAccount) -> Collection:
    """A task list with two open tasks and one completed task."""
    collection = fake_account.add_collection("Groceries")
    fake_account.put_item(collection, vtodo("milk", "Milk", "PRIORITY:1\r\n"), "milk")
    fake_account.put_item(
        collection,
        vtodo(
            "eggs",
            "Eggs",
            "X-OTHER:keep\r\nBEGIN:VALARM\r\nACTION:DISPLAY\r\n"
            "TRIGGER:-PT5M\r\nDESCRIPTION:x\r\nEND:VALARM\r\n",
        ),
        "eggs",
    )
    fake_account.put_item(collection, vtodo("tea", "Tea", "STATUS:COMPLETED\r\n"), "tea")
    # Deleted tasks and calendars are ignored.
    gone = fake_account.put_item(collection, vtodo("gone", "Gone"), "gone")
    fake_account.remote_delete_item(collection, gone.uid)
    fake_account.add_collection("Calendar", "etebase.vevent")
    return collection


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_restore: None,
    groceries: Collection,
) -> MockConfigEntry:
    """Set up the integration with the fake account."""
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()
    return config_entry


async def _get_items(hass: HomeAssistant, entity_id: str = GROCERIES) -> list[dict[str, Any]]:
    result = await hass.services.async_call(
        TODO_DOMAIN,
        TodoServices.GET_ITEMS,
        {},
        target={ATTR_ENTITY_ID: entity_id},
        blocking=True,
        return_response=True,
    )
    return result[entity_id]["items"]


async def _call(
    hass: HomeAssistant, service: str, data: dict[str, Any], entity_id: str = GROCERIES
) -> None:
    await hass.services.async_call(
        TODO_DOMAIN, service, data, target={ATTR_ENTITY_ID: entity_id}, blocking=True
    )


async def _refresh(entry: MockConfigEntry) -> None:
    await entry.runtime_data.async_refresh()
    await entry.runtime_data.hass.async_block_till_done()


def _content(fake_account: FakeAccount, collection: Collection, uid: str) -> dict[str, str]:
    todo = Calendar.from_ical(fake_account.item(collection, uid).content).walk("VTODO")[0]
    return {name: todo[name].to_ical().decode() for name in todo}


def _uid(items: list[dict[str, Any]], summary: str) -> str:
    return next(item["uid"] for item in items if item["summary"] == summary)


@pytest.mark.usefixtures("setup_integration")
async def test_setup(hass: HomeAssistant, entity_registry: er.EntityRegistry) -> None:
    """Every task list becomes an entity counting its open tasks."""
    state = hass.states.get(GROCERIES)
    assert state is not None
    assert state.state == "2"
    assert state.attributes["friendly_name"] == "alice Groceries"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == (
        TodoListEntityFeature.CREATE_TODO_ITEM
        | TodoListEntityFeature.UPDATE_TODO_ITEM
        | TodoListEntityFeature.DELETE_TODO_ITEM
        | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
        | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
        | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
    )
    assert [(i["summary"], i["status"]) for i in await _get_items(hass)] == [
        ("Milk", "needs_action"),
        ("Eggs", "needs_action"),
        ("Tea", "completed"),
    ]
    assert len(hass.states.async_entity_ids(TODO_DOMAIN)) == 1


async def test_create_item(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_account: FakeAccount,
    groceries: Collection,
) -> None:
    """New items are uploaded as VTODOs named after their iCal UID."""
    await _call(
        hass,
        TodoServices.ADD_ITEM,
        {"item": "Bread", "due_date": "2026-03-05", "description": "Whole grain"},
    )
    items = await _get_items(hass)
    bread = next(item for item in items if item["summary"] == "Bread")
    assert bread["due"] == "2026-03-05"
    assert bread["description"] == "Whole grain"
    assert hass.states.get(GROCERIES).state == "3"

    stored = fake_account.item(groceries, bread["uid"])
    props = _content(fake_account, groceries, bread["uid"])
    assert stored.meta["name"] == props["UID"]
    assert props["SUMMARY"] == "Bread"
    assert props["STATUS"] == "NEEDS-ACTION"

    # The new item survives the next sync.
    await _refresh(setup_integration)
    assert len(await _get_items(hass)) == 4


@pytest.mark.usefixtures("setup_integration")
async def test_update_item(
    hass: HomeAssistant, fake_account: FakeAccount, groceries: Collection
) -> None:
    """Updates are written to the server and keep data of other clients."""
    eggs = _uid(await _get_items(hass), "Eggs")
    await _call(
        hass,
        TodoServices.UPDATE_ITEM,
        {"item": eggs, "rename": "Free range eggs", "status": "completed"},
    )
    props = _content(fake_account, groceries, eggs)
    assert props["SUMMARY"] == "Free range eggs"
    assert props["STATUS"] == "COMPLETED"
    assert "COMPLETED" in props
    assert props["X-OTHER"] == "keep"
    assert b"TRIGGER:-PT5M" in fake_account.item(groceries, eggs).content
    assert fake_account.item(groceries, eggs).meta["name"] == "eggs"
    assert hass.states.get(GROCERIES).state == "1"


@pytest.mark.usefixtures("setup_integration")
async def test_delete_items(
    hass: HomeAssistant, fake_account: FakeAccount, groceries: Collection
) -> None:
    """Deleted items are marked deleted on the server."""
    items = await _get_items(hass)
    uids = [_uid(items, "Milk"), _uid(items, "Tea")]
    await _call(hass, TodoServices.REMOVE_ITEM, {"item": ["Milk", "Tea"]})
    assert all(fake_account.item(groceries, uid).deleted for uid in uids)
    assert [item["summary"] for item in await _get_items(hass)] == ["Eggs"]


@pytest.mark.usefixtures("setup_integration")
async def test_unknown_item(hass: HomeAssistant) -> None:
    """Updating a missing item fails with a validation error."""
    with pytest.raises(ServiceValidationError):
        await _call(hass, TodoServices.UPDATE_ITEM, {"item": "Nope", "rename": "x"})


async def test_read_only_list(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_restore: None,
    fake_account: FakeAccount,
) -> None:
    """Lists shared read-only cannot be modified."""
    shared = fake_account.add_collection("Shared", access_level=AccessLevel.READ_ONLY)
    fake_account.put_item(shared, vtodo("a", "A"), "a")
    assert await hass.config_entries.async_setup(config_entry.entry_id)
    await hass.async_block_till_done()

    state = hass.states.get("todo.alice_shared")
    assert state.state == "1"
    assert state.attributes[ATTR_SUPPORTED_FEATURES] == 0
    with pytest.raises(ServiceValidationError):
        await _call(hass, TodoServices.ADD_ITEM, {"item": "B"}, "todo.alice_shared")


async def test_remote_changes(
    hass: HomeAssistant,
    setup_integration: MockConfigEntry,
    fake_account: FakeAccount,
    groceries: Collection,
    entity_registry: er.EntityRegistry,
) -> None:
    """Changes made by other clients are picked up incrementally."""
    milk = _uid(await _get_items(hass), "Milk")
    fake_account.edit_item(groceries, milk, vtodo("milk", "Oat milk"))
    fake_account.put_item(groceries, vtodo("jam", "Jam"), "jam")
    work = fake_account.add_collection("Work")
    fake_account.put_item(work, vtodo("w", "Report"), "w")
    await _refresh(setup_integration)

    assert [i["summary"] for i in await _get_items(hass)] == ["Eggs", "Jam", "Oat milk", "Tea"]
    assert hass.states.get("todo.alice_work").state == "1"

    fake_account.delete_collection(work)
    fake_account.remove_membership(groceries)
    await _refresh(setup_integration)
    assert hass.states.get("todo.alice_work") is None
    assert hass.states.get(GROCERIES) is None
    assert not er.async_entries_for_config_entry(entity_registry, setup_integration.entry_id)


@pytest.mark.usefixtures("setup_integration")
async def test_conflict_is_retried(
    hass: HomeAssistant, fake_account: FakeAccount, groceries: Collection
) -> None:
    """A task changed elsewhere since the last sync is updated on its latest revision."""
    milk = _uid(await _get_items(hass), "Milk")
    fake_account.edit_item(groceries, milk, vtodo("milk", "Milk", "DESCRIPTION:2 liters\r\n"))

    await _call(hass, TodoServices.UPDATE_ITEM, {"item": milk, "status": "completed"})

    props = _content(fake_account, groceries, milk)
    assert props["STATUS"] == "COMPLETED"
    assert props["DESCRIPTION"] == "2 liters"


@pytest.mark.usefixtures("setup_integration")
async def test_update_item_deleted_remotely(
    hass: HomeAssistant, fake_account: FakeAccount, groceries: Collection
) -> None:
    """Updating a task that was deleted elsewhere fails and drops it."""
    milk = _uid(await _get_items(hass), "Milk")
    fake_account.remote_delete_item(groceries, milk)
    with pytest.raises(ServiceValidationError):
        await _call(hass, TodoServices.UPDATE_ITEM, {"item": milk, "status": "completed"})
    assert fake_account.item(groceries, milk).deleted
    assert "Milk" not in [item["summary"] for item in await _get_items(hass)]


async def test_write_error(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_account: FakeAccount
) -> None:
    """Network errors while writing are reported."""
    with (
        patch.object(fake_account, "transaction", side_effect=EtebaseConnectionError("down")),
        pytest.raises(HomeAssistantError, match="down"),
    ):
        await _call(hass, TodoServices.ADD_ITEM, {"item": "Bread"})


async def test_token_refresh(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_account: FakeAccount
) -> None:
    """An expired token is renewed and the new session is stored."""
    fake_account.list_errors = [UnauthorizedError(401, "Invalid token")]
    await _refresh(setup_integration)
    assert fake_account.fetch_token_calls == 1
    assert setup_integration.data[CONF_SESSION] == "session-2"
    assert hass.states.get(GROCERIES).state == "2"


async def test_auth_failed_starts_reauth(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_account: FakeAccount
) -> None:
    """If the token cannot be renewed, re-authentication is requested."""
    fake_account.list_errors = [UnauthorizedError(401, "Invalid token")]
    fake_account.fetch_token_error = UnauthorizedError(401, "Wrong password for user.")
    await _refresh(setup_integration)
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_update_failed(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_account: FakeAccount
) -> None:
    """Entities become unavailable while the server is unreachable."""
    fake_account.list_errors = [EtebaseConnectionError("down")]
    await _refresh(setup_integration)
    assert hass.states.get(GROCERIES).state == STATE_UNAVAILABLE
    await _refresh(setup_integration)
    assert hass.states.get(GROCERIES).state == "2"


async def test_setup_retry(
    hass: HomeAssistant,
    config_entry: MockConfigEntry,
    mock_restore: None,
    fake_account: FakeAccount,
) -> None:
    """Setup is retried while the server is unreachable."""
    fake_account.list_errors = [EtebaseConnectionError("down")]
    assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_RETRY


async def test_invalid_session(hass: HomeAssistant, config_entry: MockConfigEntry) -> None:
    """A stored session that cannot be read requires re-authentication."""
    with patch.object(Account, "restore", side_effect=ProtocolError("bad")):
        assert not await hass.config_entries.async_setup(config_entry.entry_id)
    assert config_entry.state is ConfigEntryState.SETUP_ERROR
    flows = hass.config_entries.flow.async_progress()
    assert [flow["context"]["source"] for flow in flows] == ["reauth"]


async def test_unload_and_remove(
    hass: HomeAssistant, setup_integration: MockConfigEntry, fake_account: FakeAccount
) -> None:
    """Unloading works and removing the account logs out."""
    assert await hass.config_entries.async_unload(setup_integration.entry_id)
    assert setup_integration.state is ConfigEntryState.NOT_LOADED
    await hass.config_entries.async_remove(setup_integration.entry_id)
    assert fake_account.logged_out
    assert not hass.config_entries.async_entries(DOMAIN)


async def test_diagnostics(
    hass: HomeAssistant,
    hass_client: ClientSessionGenerator,
    setup_integration: MockConfigEntry,
) -> None:
    """Diagnostics contain counts but neither credentials nor task content."""
    assert await async_setup_component(hass, "diagnostics", {})
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, setup_integration)
    assert diagnostics["entry"]["data"][CONF_SESSION] == "**REDACTED**"
    assert diagnostics["task_lists"] == [
        {"access_level": str(AccessLevel.ADMIN), "tasks": 3, "open_tasks": 2}
    ]
    assert "Milk" not in str(diagnostics)
