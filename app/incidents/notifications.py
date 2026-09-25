"""Notification boundary; structured logging is the safe default adapter."""

from __future__ import annotations

import logging
from typing import Protocol

from app.incidents.safe import safe_value


logger = logging.getLogger("nextcompany.incidents.notifications")


class NotificationAdapter(Protocol):
    name: str
    configured: bool

    def notify(self, event: str, payload: dict) -> None: ...


class StructuredLogNotificationAdapter:
    name = "structured_log"
    configured = False

    def notify(self, event: str, payload: dict) -> None:
        logger.warning("incident_notification", extra={"event": event, "payload": safe_value(payload)})


DEFAULT_NOTIFIER = StructuredLogNotificationAdapter()

