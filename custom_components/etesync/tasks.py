"""Conversion between iCalendar VTODO data and Home Assistant to-do items."""

from __future__ import annotations

from dataclasses import dataclass
import datetime
from typing import Any

from icalendar import Calendar, Todo

from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.util import dt as dt_util

PRODID = "-//Home Assistant//EteSync integration//EN"

_FINISHED_STATUSES = {"COMPLETED", "CANCELLED"}
_NO_PRIORITY = 10
_FAR_FUTURE = datetime.datetime.max.replace(tzinfo=datetime.UTC)
_EPOCH = datetime.datetime.min.replace(tzinfo=datetime.UTC)


class InvalidTaskError(ValueError):
    """The item does not contain a usable VTODO."""


@dataclass(frozen=True, slots=True)
class ParsedTask:
    """A VTODO decoded from an Etebase item."""

    todo_item: TodoItem
    sort_key: tuple[Any, ...]
    ical_uid: str | None


def _local_datetime(value: datetime.datetime) -> datetime.datetime:
    """Interpret floating times in the local time zone and convert to local time."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_util.get_default_time_zone())
    return dt_util.as_local(value)


def _date_value(todo: Todo, name: str) -> datetime.date | datetime.datetime | None:
    prop = todo.get(name)
    value = getattr(prop, "dt", None)
    if isinstance(value, datetime.datetime):
        return _local_datetime(value)
    if isinstance(value, datetime.date):
        return value
    return None


def _text(todo: Todo, name: str) -> str | None:
    value = todo.get(name)
    if value is None:
        return None
    if isinstance(value, list):  # repeated property, use the first one
        value = value[0]
    return str(value)


def _priority(todo: Todo) -> int:
    try:
        priority = int(todo.get("PRIORITY", 0))
    except TypeError, ValueError:
        return _NO_PRIORITY
    return priority if 1 <= priority <= 9 else _NO_PRIORITY


def _as_sortable(value: datetime.date | datetime.datetime | None) -> datetime.datetime:
    if value is None:
        return _FAR_FUTURE
    if isinstance(value, datetime.datetime):
        return value
    return dt_util.start_of_local_day(value)


def first_vtodo(calendar: Calendar) -> Todo:
    """Return the first VTODO of a calendar."""
    todos = calendar.walk("VTODO")
    if not todos:
        raise InvalidTaskError("No VTODO component")
    return todos[0]


def parse_calendar(content: bytes) -> Calendar:
    """Parse iCalendar data."""
    try:
        calendar = Calendar.from_ical(content)
    except ValueError as err:
        raise InvalidTaskError(str(err)) from err
    if not isinstance(calendar, Calendar):
        raise InvalidTaskError("Not a VCALENDAR")
    return calendar


def parse_task(item_uid: str, content: bytes) -> ParsedTask:
    """Decode the VTODO of an item into a to-do item and its sort key.

    Items are sorted like the "smart" order of the EteSync web app: open tasks
    first, then by priority, due date, title and most recently modified.
    """
    todo = first_vtodo(parse_calendar(content))
    status = (_text(todo, "STATUS") or "NEEDS-ACTION").upper()
    finished = status in _FINISHED_STATUSES
    summary = _text(todo, "SUMMARY") or ""
    due = _date_value(todo, "DUE")
    completed = _date_value(todo, "COMPLETED")
    last_modified = _date_value(todo, "LAST-MODIFIED")
    todo_item = TodoItem(
        uid=item_uid,
        summary=summary,
        status=TodoItemStatus.COMPLETED if finished else TodoItemStatus.NEEDS_ACTION,
        due=due,
        description=_text(todo, "DESCRIPTION") or None,
        completed=completed if isinstance(completed, datetime.datetime) else None,
    )
    modified = last_modified if isinstance(last_modified, datetime.datetime) else _EPOCH
    sort_key = (
        finished,
        _priority(todo),
        _as_sortable(due),
        summary.casefold(),
        -modified.timestamp(),
    )
    return ParsedTask(todo_item, sort_key, _text(todo, "UID"))


def _ical_datetime(value: datetime.date | datetime.datetime) -> datetime.date | datetime.datetime:
    if isinstance(value, datetime.datetime):
        return dt_util.as_utc(value)
    return value


def _set_due(todo: Todo, due: datetime.date | datetime.datetime | None) -> None:
    todo.pop("DUE", None)
    if due is None:
        return
    todo.pop("DURATION", None)
    todo.add("DUE", _ical_datetime(due))
    # RFC 5545: DTSTART must be of the same value type as DUE and not after it.
    start = getattr(todo.get("DTSTART"), "dt", None)
    if start is None:
        return
    start_is_datetime = isinstance(start, datetime.datetime)
    if start_is_datetime != isinstance(due, datetime.datetime) or _as_sortable(
        _local_datetime(start) if start_is_datetime else start
    ) > _as_sortable(due):
        todo.pop("DTSTART", None)


def _set_text(todo: Todo, name: str, value: str | None) -> None:
    todo.pop(name, None)
    if value:
        todo.add(name, value)


def _set_status(todo: Todo, status: TodoItemStatus | None, now: datetime.datetime) -> None:
    todo.pop("STATUS", None)
    todo.pop("COMPLETED", None)
    if status == TodoItemStatus.COMPLETED:
        todo.add("STATUS", "COMPLETED")
        todo.add("COMPLETED", now)
        if "PERCENT-COMPLETE" in todo:
            todo.pop("PERCENT-COMPLETE")
            todo.add("PERCENT-COMPLETE", 100)
    else:
        todo.add("STATUS", "NEEDS-ACTION")
        if str(todo.get("PERCENT-COMPLETE", "")) == "100":
            todo.pop("PERCENT-COMPLETE")


def _touch(todo: Todo, now: datetime.datetime) -> None:
    for name in ("DTSTAMP", "LAST-MODIFIED"):
        todo.pop(name, None)
        todo.add(name, now)


def new_task(ical_uid: str, item: TodoItem) -> bytes:
    """Serialize a new to-do item as a VCALENDAR."""
    now = dt_util.utcnow().replace(microsecond=0)
    calendar = Calendar()
    calendar.add("PRODID", PRODID)
    calendar.add("VERSION", "2.0")
    todo = Todo()
    todo.add("UID", ical_uid)
    todo.add("CREATED", now)
    _touch(todo, now)
    _set_text(todo, "SUMMARY", item.summary)
    _set_status(todo, item.status, now)
    _set_due(todo, item.due)
    _set_text(todo, "DESCRIPTION", item.description)
    calendar.add_component(todo)
    return calendar.to_ical()


def update_task(content: bytes, current: TodoItem, updated: TodoItem) -> bytes:
    """Apply the fields that differ between ``current`` and ``updated`` to the VTODO.

    Everything else in the calendar (alarms, categories, time zones, properties
    of other clients) is preserved.
    """
    calendar = parse_calendar(content)
    todo = first_vtodo(calendar)
    now = dt_util.utcnow().replace(microsecond=0)
    if updated.summary != current.summary:
        _set_text(todo, "SUMMARY", updated.summary)
    if updated.status != current.status:
        _set_status(todo, updated.status, now)
    if updated.due != current.due:
        _set_due(todo, updated.due)
    if updated.description != current.description:
        _set_text(todo, "DESCRIPTION", updated.description)
    _touch(todo, now)
    return calendar.to_ical()
