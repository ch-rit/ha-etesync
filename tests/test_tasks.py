"""Tests for the VTODO <-> to-do item conversion."""

from __future__ import annotations

import datetime
from zoneinfo import ZoneInfo

from freezegun import freeze_time
from icalendar import Calendar
import pytest

from custom_components.etesync.tasks import (
    InvalidTaskError,
    new_task,
    parse_task,
    update_task,
)
from homeassistant.components.todo import TodoItem, TodoItemStatus
from homeassistant.core import HomeAssistant

from .fake_etebase import vtodo

VIENNA = ZoneInfo("Europe/Vienna")


@pytest.fixture(autouse=True)
async def set_time_zone(hass: HomeAssistant) -> None:
    """Use a fixed local time zone."""
    await hass.config.async_set_time_zone("Europe/Vienna")


def _todo(content: bytes) -> dict[str, str]:
    todo = Calendar.from_ical(content).walk("VTODO")[0]
    return {name: todo[name].to_ical().decode() for name in todo}


def test_parse_fields() -> None:
    """All supported fields are read; floating times are local."""
    parsed = parse_task(
        "item",
        vtodo(
            "ical",
            "Milk",
            "STATUS:IN-PROCESS\r\nDUE:20260105T183000\r\nDESCRIPTION:2 liters\r\n",
        ),
    )
    assert parsed.ical_uid == "ical"
    assert parsed.todo_item == TodoItem(
        uid="item",
        summary="Milk",
        status=TodoItemStatus.NEEDS_ACTION,
        due=datetime.datetime(2026, 1, 5, 18, 30, tzinfo=VIENNA),
        description="2 liters",
    )


@pytest.mark.parametrize(
    ("extra", "status"),
    [
        ("STATUS:COMPLETED\r\nCOMPLETED:20260101T120000Z\r\n", TodoItemStatus.COMPLETED),
        ("STATUS:CANCELLED\r\n", TodoItemStatus.COMPLETED),
        ("", TodoItemStatus.NEEDS_ACTION),
    ],
)
def test_parse_status(extra: str, status: TodoItemStatus) -> None:
    """Completed and cancelled tasks count as done, like in the EteSync apps."""
    assert parse_task("item", vtodo("u", "x", extra)).todo_item.status == status


def test_parse_dates() -> None:
    """Date-only due dates stay dates; UTC times are converted to local time."""
    item = parse_task("i", vtodo("u", "x", "DUE;VALUE=DATE:20260105\r\n")).todo_item
    assert item.due == datetime.date(2026, 1, 5)
    item = parse_task(
        "i", vtodo("u", "x", "STATUS:COMPLETED\r\nCOMPLETED:20260101T120000Z\r\n")
    ).todo_item
    assert item.completed == datetime.datetime(2026, 1, 1, 13, 0, tzinfo=VIENNA)


def test_parse_invalid() -> None:
    """Content without a VTODO is rejected."""
    with pytest.raises(InvalidTaskError):
        parse_task("i", b"BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n")
    with pytest.raises(InvalidTaskError):
        parse_task("i", b"garbage")


def test_sort_order() -> None:
    """Open first, then priority, due date, title and newest modification."""
    tasks = {
        "done": vtodo("1", "A", "STATUS:COMPLETED\r\nPRIORITY:1\r\n"),
        "low": vtodo("2", "A", "PRIORITY:9\r\n"),
        "high_late": vtodo("3", "Z", "PRIORITY:1\r\nDUE;VALUE=DATE:20260110\r\n"),
        "high_early": vtodo("4", "Z", "PRIORITY:1\r\nDUE:20260101T080000Z\r\n"),
        "none_b": vtodo("5", "b"),
        "none_a_old": vtodo("6", "a", "LAST-MODIFIED:20250101T000000Z\r\n"),
        "none_a_new": vtodo("7", "a", "LAST-MODIFIED:20260101T000000Z\r\n"),
    }
    ordered = sorted(
        (parse_task(uid, content) for uid, content in tasks.items()), key=lambda t: t.sort_key
    )
    assert [t.todo_item.uid for t in ordered] == [
        "high_early",
        "high_late",
        "low",
        "none_a_new",
        "none_a_old",
        "none_b",
        "done",
    ]


@freeze_time("2026-03-01 10:00:00+00:00")
def test_new_task() -> None:
    """New tasks are valid VTODOs with UTC due times."""
    content = new_task(
        "ical-uid",
        TodoItem(
            summary="Bread",
            status=TodoItemStatus.NEEDS_ACTION,
            due=datetime.datetime(2026, 3, 2, 9, 0, tzinfo=VIENNA),
            description="Whole grain",
        ),
    )
    props = _todo(content)
    assert props["UID"] == "ical-uid"
    assert props["SUMMARY"] == "Bread"
    assert props["STATUS"] == "NEEDS-ACTION"
    assert props["DUE"] == "20260302T080000Z"
    assert props["DESCRIPTION"] == "Whole grain"
    assert props["LAST-MODIFIED"] == props["DTSTAMP"] == props["CREATED"] == "20260301T100000Z"
    assert parse_task("i", content).todo_item.due == datetime.datetime(
        2026, 3, 2, 9, 0, tzinfo=VIENNA
    )


@freeze_time("2026-03-01 10:00:00+00:00")
def test_update_preserves_unknown_data() -> None:
    """Only changed fields are written; alarms and other properties survive."""
    content = vtodo(
        "u",
        "Milk",
        "DTSTART;VALUE=DATE:20260301\r\nCATEGORIES:Home\r\nX-OTHER-CLIENT:keep me\r\n"
        "PERCENT-COMPLETE:40\r\n"
        "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nDESCRIPTION:Milk\r\nEND:VALARM\r\n",
    )
    current = parse_task("i", content).todo_item
    updated = update_task(
        content,
        current,
        TodoItem(
            uid="i",
            summary="Oat milk",
            status=TodoItemStatus.COMPLETED,
            due=datetime.date(2026, 3, 5),
            description=None,
        ),
    )
    props = _todo(updated)
    assert props["SUMMARY"] == "Oat milk"
    assert props["STATUS"] == "COMPLETED"
    assert props["COMPLETED"] == "20260301T100000Z"
    assert props["PERCENT-COMPLETE"] == "100"
    assert props["DUE"] == "20260305"
    assert props["DTSTART"] == "20260301"
    assert props["CATEGORIES"] == "Home"
    assert props["X-OTHER-CLIENT"] == "keep me"
    assert props["LAST-MODIFIED"] == "20260301T100000Z"
    assert b"BEGIN:VALARM" in updated
    assert b"TRIGGER:-PT15M" in updated

    reopened = update_task(
        updated,
        parse_task("i", updated).todo_item,
        TodoItem(
            uid="i",
            summary="Oat milk",
            status=TodoItemStatus.NEEDS_ACTION,
            due=datetime.date(2026, 3, 5),
        ),
    )
    props = _todo(reopened)
    assert props["STATUS"] == "NEEDS-ACTION"
    assert "COMPLETED" not in props
    assert "PERCENT-COMPLETE" not in props


def test_update_keeps_unchanged_representation() -> None:
    """Unchanged fields keep their original encoding (time zone, status)."""
    content = vtodo(
        "u", "Milk", "STATUS:CANCELLED\r\nDUE;TZID=America/New_York:20260105T090000\r\n"
    )
    current = parse_task("i", content).todo_item
    updated = update_task(content, current, TodoItem(**{**current.__dict__, "summary": "Tea"}))
    props = _todo(updated)
    assert props["STATUS"] == "CANCELLED"
    assert props["DUE"] == "20260105T090000"
    assert b"DUE;TZID=America/New_York:20260105T090000" in updated


def test_update_due_drops_incompatible_start() -> None:
    """DTSTART is removed when it would be after (or typed unlike) the new DUE."""
    content = vtodo("u", "x", "DTSTART:20260310T080000Z\r\nDURATION:PT1H\r\n")
    current = parse_task("i", content).todo_item
    earlier = update_task(
        content,
        current,
        TodoItem(
            uid="i",
            summary="x",
            status=TodoItemStatus.NEEDS_ACTION,
            due=datetime.datetime(2026, 3, 9, 8, 0, tzinfo=datetime.UTC),
        ),
    )
    props = _todo(earlier)
    assert "DTSTART" not in props
    assert "DURATION" not in props
    assert props["DUE"] == "20260309T080000Z"

    later = update_task(
        content,
        current,
        TodoItem(
            uid="i",
            summary="x",
            status=TodoItemStatus.NEEDS_ACTION,
            due=datetime.datetime(2026, 3, 11, 8, 0, tzinfo=datetime.UTC),
        ),
    )
    assert _todo(later)["DTSTART"] == "20260310T080000Z"

    date_only = update_task(
        content,
        current,
        TodoItem(
            uid="i", summary="x", status=TodoItemStatus.NEEDS_ACTION, due=datetime.date(2026, 3, 11)
        ),
    )
    assert "DTSTART" not in _todo(date_only)
