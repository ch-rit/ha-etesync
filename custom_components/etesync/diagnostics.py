"""Diagnostics for EteSync."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_USERNAME
from homeassistant.core import HomeAssistant

from .const import CONF_SESSION
from .coordinator import EteSyncConfigEntry

TO_REDACT = {CONF_SESSION, CONF_USERNAME, "title", "unique_id"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: EteSyncConfigEntry
) -> dict[str, Any]:
    """Return diagnostics without task content or credentials."""
    coordinator = entry.runtime_data
    return {
        "entry": async_redact_data(entry.as_dict(), TO_REDACT),
        "last_update_success": coordinator.last_update_success,
        "task_lists": [
            {
                "access_level": str(task_list.collection.access_level),
                "tasks": len(task_list.tasks),
                "open_tasks": sum(not task.parsed.sort_key[0] for task in task_list.tasks.values()),
            }
            for task_list in coordinator.data.values()
        ],
    }
