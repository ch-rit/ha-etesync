"""Data update coordinator for an EteSync account."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from functools import partial
import time
from typing import Any
import uuid

from homeassistant.components.todo import TodoItem
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import CONF_SESSION, DOMAIN, LOGGER, SCAN_INTERVAL, TASKS_COLLECTION_TYPE
from .etebase import (
    Account,
    Collection,
    ConflictError,
    EtebaseError,
    Item,
    UnauthorizedError,
)
from .tasks import InvalidTaskError, ParsedTask, new_task, parse_task, update_task

type EteSyncConfigEntry = ConfigEntry[EteSyncCoordinator]


@dataclass(slots=True)
class Task:
    """A decrypted task and the item it came from."""

    item: Item
    parsed: ParsedTask


@dataclass(slots=True)
class TaskList:
    """A task list (an ``etebase.vtodo`` collection) and its tasks."""

    collection: Collection
    tasks: dict[str, Task] = field(default_factory=dict)
    stoken: str | None = None

    @property
    def name(self) -> str:
        """Name of the list."""
        return str(self.collection.meta.get("name") or "Tasks")

    @property
    def read_only(self) -> bool:
        """Whether the account may only read this list."""
        return self.collection.read_only

    def todo_items(self) -> list[TodoItem]:
        """Return the to-do items in display order."""
        return [
            task.parsed.todo_item
            for task in sorted(self.tasks.values(), key=lambda task: task.parsed.sort_key)
        ]


def _now_ms() -> int:
    return int(time.time() * 1000)


def _decode_items(items: list[Item]) -> list[tuple[str, Task | None]]:
    """Decrypt and parse items (CPU bound, runs in the executor)."""
    result: list[tuple[str, Task | None]] = []
    for item in items:
        if item.deleted:
            result.append((item.uid, None))
            continue
        try:
            parsed = parse_task(item.uid, item.content)
        except (EtebaseError, InvalidTaskError) as err:
            LOGGER.warning("Skipping task %s that could not be read: %s", item.uid, err)
            result.append((item.uid, None))
        else:
            result.append((item.uid, Task(item, parsed)))
    return result


def _bumped_meta(item: Item, ical_uid: str | None) -> dict[str, Any]:
    meta = {**item.meta, "mtime": _now_ms()}
    if ical_uid and "name" not in meta:
        meta["name"] = ical_uid
    return meta


class EteSyncCoordinator(DataUpdateCoordinator[dict[str, TaskList]]):
    """Keeps the task lists of one account in sync."""

    config_entry: EteSyncConfigEntry

    def __init__(self, hass: HomeAssistant, entry: EteSyncConfigEntry, account: Account) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=f"{DOMAIN} {account.username}",
            update_interval=SCAN_INTERVAL,
        )
        self.account = account
        self._lists: dict[str, TaskList] = {}
        self._collections_stoken: str | None = None
        self._lock = asyncio.Lock()

    async def _call[T](self, func: Callable[[], Awaitable[T]]) -> T:
        """Run a request, refreshing an expired token once."""
        try:
            return await func()
        except UnauthorizedError:
            LOGGER.debug("Token of %s rejected, fetching a new one", self.account.username)
        try:
            await self.account.fetch_token()
        except UnauthorizedError as err:
            raise ConfigEntryAuthFailed(
                translation_domain=DOMAIN, translation_key="auth_failed"
            ) from err
        self.hass.config_entries.async_update_entry(
            self.config_entry,
            data={**self.config_entry.data, CONF_SESSION: self.account.save()},
        )
        return await func()

    async def _async_update_data(self) -> dict[str, TaskList]:
        """Fetch collections and items changed since the last sync."""
        async with self._lock:
            try:
                await self._sync()
            except EtebaseError as err:
                raise UpdateFailed(
                    translation_domain=DOMAIN,
                    translation_key="update_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
        return self._lists

    async def _sync(self) -> None:
        listing = await self._call(
            partial(
                self.account.list_collections, [TASKS_COLLECTION_TYPE], self._collections_stoken
            )
        )
        for uid in listing.removed_memberships:
            self._lists.pop(uid, None)
        for collection in listing.collections:
            if collection.deleted:
                self._lists.pop(collection.uid, None)
                continue
            task_list = self._lists.get(collection.uid)
            stoken = task_list.stoken if task_list else None
            items = await self._call(partial(self.account.list_items, collection, stoken))
            changes = await self.hass.async_add_executor_job(_decode_items, items.items)
            if task_list is None:
                task_list = self._lists[collection.uid] = TaskList(collection)
            task_list.collection = collection
            for uid, task in changes:
                if task is None:
                    task_list.tasks.pop(uid, None)
                else:
                    task_list.tasks[uid] = task
            task_list.stoken = items.stoken
        self._collections_stoken = listing.stoken

    @asynccontextmanager
    async def _write(self, list_uid: str) -> AsyncIterator[TaskList]:
        """Serialize a write to a list and translate errors."""
        async with self._lock:
            task_list = self._lists.get(list_uid)
            if task_list is None:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="list_not_found"
                )
            if task_list.read_only:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="read_only",
                    translation_placeholders={"list": task_list.name},
                )
            try:
                yield task_list
            except ConfigEntryAuthFailed as err:
                self.config_entry.async_start_reauth(self.hass)
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="auth_failed"
                ) from err
            except ConflictError as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="conflict"
                ) from err
            except (EtebaseError, InvalidTaskError) as err:
                raise HomeAssistantError(
                    translation_domain=DOMAIN,
                    translation_key="write_failed",
                    translation_placeholders={"error": str(err)},
                ) from err
            self.async_set_updated_data(self._lists)

    async def _upload(
        self,
        task_list: TaskList,
        items: list[Item],
        change: Callable[[Item], Item],
    ) -> list[Item]:
        """Upload changed copies of items, retrying once on a remote conflict.

        Items deleted remotely in the meantime are dropped from the list and
        are not part of the returned (uploaded) items.
        """
        for attempt in range(2):
            changed = [change(item) for item in items]
            try:
                if changed:
                    await self._call(
                        partial(self.account.transaction, task_list.collection, changed)
                    )
            except ConflictError:
                if attempt:
                    raise
                LOGGER.debug("Tasks changed remotely, retrying with the latest revision")
                latest = [
                    await self._call(
                        partial(self.account.fetch_item, task_list.collection, item.uid)
                    )
                    for item in items
                ]
                for item in latest:
                    if item.deleted:
                        task_list.tasks.pop(item.uid, None)
                items = [item for item in latest if not item.deleted]
            else:
                return changed
        raise AssertionError("unreachable")

    def _tasks(self, task_list: TaskList, uids: list[str]) -> list[Task]:
        missing = [uid for uid in uids if uid not in task_list.tasks]
        if missing:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="item_not_found",
                translation_placeholders={"uid": ", ".join(missing)},
            )
        return [task_list.tasks[uid] for uid in uids]

    async def async_create_task(self, list_uid: str, todo_item: TodoItem) -> None:
        """Add a task to a list."""
        async with self._write(list_uid) as task_list:
            ical_uid = str(uuid.uuid4())
            content = new_task(ical_uid, todo_item)
            item = task_list.collection.create_item({"name": ical_uid, "mtime": _now_ms()}, content)
            await self._call(partial(self.account.transaction, task_list.collection, [item]))
            task_list.tasks[item.uid] = Task(item, parse_task(item.uid, content))

    async def async_update_task(self, list_uid: str, todo_item: TodoItem) -> None:
        """Apply the changes of ``todo_item`` to its task."""
        async with self._write(list_uid) as task_list:
            (task,) = self._tasks(task_list, [todo_item.uid or ""])
            current = task.parsed.todo_item

            def change(item: Item) -> Item:
                updated = item.clone()
                updated.update(
                    _bumped_meta(item, task.parsed.ical_uid),
                    update_task(item.content, current, todo_item),
                )
                return updated

            uploaded = await self._upload(task_list, [task.item], change)
            if not uploaded:
                self.async_set_updated_data(self._lists)
                self._tasks(task_list, [task.item.uid])  # raises item_not_found
            item = uploaded[0]
            task_list.tasks[item.uid] = Task(item, parse_task(item.uid, item.content))

    async def async_delete_tasks(self, list_uid: str, uids: list[str]) -> None:
        """Delete tasks from a list."""
        async with self._write(list_uid) as task_list:
            tasks = self._tasks(task_list, uids)
            ical_uids = {task.item.uid: task.parsed.ical_uid for task in tasks}

            def change(item: Item) -> Item:
                deleted = item.clone()
                deleted.delete(_bumped_meta(item, ical_uids[item.uid]))
                return deleted

            for item in await self._upload(task_list, [task.item for task in tasks], change):
                task_list.tasks.pop(item.uid, None)
