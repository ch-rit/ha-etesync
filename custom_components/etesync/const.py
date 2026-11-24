"""Constants for the EteSync integration."""

from __future__ import annotations

from datetime import timedelta
import logging
from typing import Final

DOMAIN: Final = "etesync"
LOGGER = logging.getLogger(__package__)

CONF_SESSION: Final = "session"

TASKS_COLLECTION_TYPE: Final = "etebase.vtodo"
SCAN_INTERVAL: Final = timedelta(minutes=1)
