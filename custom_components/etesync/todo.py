"""To-do list entities for EteSync task lists."""

from __future__ import annotations

from homeassistant.components.todo import (
    TodoItem,
    TodoListEntity,
    TodoListEntityFeature,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceEntryType, DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import EteSyncConfigEntry, EteSyncCoordinator, TaskList

PARALLEL_UPDATES = 0

_WRITE_FEATURES = (
    TodoListEntityFeature.CREATE_TODO_ITEM
    | TodoListEntityFeature.UPDATE_TODO_ITEM
    | TodoListEntityFeature.DELETE_TODO_ITEM
    | TodoListEntityFeature.SET_DUE_DATE_ON_ITEM
    | TodoListEntityFeature.SET_DUE_DATETIME_ON_ITEM
    | TodoListEntityFeature.SET_DESCRIPTION_ON_ITEM
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EteSyncConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Create an entity for every task list and follow list additions and removals."""
    coordinator = entry.runtime_data
    known: set[str] = set()
    registry = er.async_get(hass)

    @callback
    def _sync_entities() -> None:
        current = set(coordinator.data)
        if new := current - known:
            known.update(new)
            async_add_entities(EteSyncTodoListEntity(coordinator, uid) for uid in new)
        for uid in known - current:
            known.discard(uid)
            entity_id = registry.async_get_entity_id(
                "todo", DOMAIN, EteSyncTodoListEntity.build_unique_id(coordinator, uid)
            )
            if entity_id:
                registry.async_remove(entity_id)

    _sync_entities()
    entry.async_on_unload(coordinator.async_add_listener(_sync_entities))


class EteSyncTodoListEntity(CoordinatorEntity[EteSyncCoordinator], TodoListEntity):
    """An EteSync task list."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: EteSyncCoordinator, list_uid: str) -> None:
        """Initialize the entity."""
        super().__init__(coordinator)
        self._list_uid = list_uid
        entry = coordinator.config_entry
        self._attr_unique_id = self.build_unique_id(coordinator, list_uid)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name=entry.title,
            manufacturer="EteSync",
            model="Etebase account",
            entry_type=DeviceEntryType.SERVICE,
        )
        self._update_from_list()

    @staticmethod
    def build_unique_id(coordinator: EteSyncCoordinator, list_uid: str) -> str:
        """Return the unique ID for a list of an account.

        Shared lists have the same collection uid in every account, so the
        account is part of the ID.
        """
        entry = coordinator.config_entry
        return f"{entry.unique_id or entry.entry_id}_{list_uid}"

    @property
    def _task_list(self) -> TaskList | None:
        return self.coordinator.data.get(self._list_uid)

    @property
    def available(self) -> bool:
        """Whether the list still exists."""
        return super().available and self._task_list is not None

    def _update_from_list(self) -> None:
        task_list = self._task_list
        if task_list is None:
            return
        self._attr_name = task_list.name
        self._attr_supported_features = (
            TodoListEntityFeature(0) if task_list.read_only else _WRITE_FEATURES
        )
        self._attr_todo_items = task_list.todo_items()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Refresh name, features and items from the coordinator."""
        self._update_from_list()
        super()._handle_coordinator_update()

    async def async_create_todo_item(self, item: TodoItem) -> None:
        """Add an item to the list."""
        await self.coordinator.async_create_task(self._list_uid, item)

    async def async_update_todo_item(self, item: TodoItem) -> None:
        """Update an item of the list."""
        await self.coordinator.async_update_task(self._list_uid, item)

    async def async_delete_todo_items(self, uids: list[str]) -> None:
        """Delete items from the list."""
        await self.coordinator.async_delete_tasks(self._list_uid, uids)
