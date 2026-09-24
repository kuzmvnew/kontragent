"""In-process allow-list for version-pinned, non-live worker handlers."""

from dataclasses import dataclass

from app.worker.contracts import WorkerHandler, WorkerPublisher
from app.worker.errors import (
    HandlerNotRegisteredError,
    LiveHandlerProhibitedError,
)


@dataclass(frozen=True)
class RegisteredHandler:
    source_id: str
    version: str
    handler: WorkerHandler
    publisher: WorkerPublisher | None
    fixture: bool


class HandlerRegistry:
    def __init__(self) -> None:
        self._handlers: dict[tuple[str, str], RegisteredHandler] = {}

    def register(
        self,
        *,
        source_id: str,
        version: str,
        handler: WorkerHandler,
        approved: bool,
        publisher: WorkerPublisher | None = None,
        live: bool = False,
        fixture: bool = False,
    ) -> RegisteredHandler:
        if live:
            raise LiveHandlerProhibitedError(
                "DEV-008 prohibits live handlers"
            )
        if not approved:
            raise HandlerNotRegisteredError(
                "handler must be explicitly approved before registration"
            )
        if not source_id.strip() or not version.strip():
            raise ValueError("source_id and handler version are required")
        key = (source_id, version)
        if key in self._handlers:
            raise ValueError(f"handler is already registered: {source_id}@{version}")
        registration = RegisteredHandler(
            source_id=source_id,
            version=version,
            handler=handler,
            publisher=publisher,
            fixture=fixture,
        )
        self._handlers[key] = registration
        return registration

    def resolve(self, source_id: str, version: str) -> RegisteredHandler:
        try:
            return self._handlers[(source_id, version)]
        except KeyError as error:
            raise HandlerNotRegisteredError(
                f"approved handler is missing: {source_id}@{version}"
            ) from error

    def registered_versions(self, source_id: str) -> tuple[str, ...]:
        return tuple(
            sorted(version for candidate, version in self._handlers if candidate == source_id)
        )
