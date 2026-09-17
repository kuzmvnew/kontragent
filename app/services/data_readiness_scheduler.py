"""Small restart-safe scheduler for a solo/MVP deployment.

The database is the schedule and run-history source of truth. No dataset is marked
configured merely because this loop exists: a runnable handler must be registered.
"""

from collections.abc import Callable, Iterable
import logging
import time

from app.services.data_readiness_service import due_dataset_codes


logger = logging.getLogger("kontragent.data_readiness.scheduler")
UpdateHandler = Callable[[], object]
HANDLERS: dict[str, UpdateHandler] = {}


def register_handler(dataset_code: str, handler: UpdateHandler) -> None:
    HANDLERS[dataset_code] = handler


def run_due_updates(*, due_codes: Iterable[str] | None = None) -> dict[str, str]:
    results: dict[str, str] = {}
    for dataset_code in due_codes if due_codes is not None else due_dataset_codes():
        handler = HANDLERS.get(dataset_code)
        if handler is None:
            results[dataset_code] = "not_configured"
            logger.error("scheduled_dataset_has_no_handler", extra={"dataset_code": dataset_code})
            continue
        try:
            handler()
        except Exception:
            results[dataset_code] = "failed"
            logger.exception("scheduled_dataset_failed", extra={"dataset_code": dataset_code})
        else:
            results[dataset_code] = "success"
    return results


def worker_loop(*, poll_seconds: int = 60) -> None:
    if poll_seconds < 5:
        raise ValueError("poll_seconds должен быть не меньше 5")
    while True:
        run_due_updates()
        time.sleep(poll_seconds)
