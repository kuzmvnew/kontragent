"""V1 worker foundation.

The package has no import from ``main.py`` and no default handlers, so adding
it cannot enable live ingestion or background scheduling.
"""

from app.worker.contracts import (
    ExecutionCounters,
    HandlerContext,
    HandlerResult,
    RawArtifactReference,
    StagingResult,
    ValidationResult,
)
from app.worker.execution import RetryPolicy, WorkerExecutor
from app.worker.registry import HandlerRegistry

__all__ = [
    "HandlerContext",
    "HandlerRegistry",
    "HandlerResult",
    "ExecutionCounters",
    "RawArtifactReference",
    "RetryPolicy",
    "StagingResult",
    "ValidationResult",
    "WorkerExecutor",
]
