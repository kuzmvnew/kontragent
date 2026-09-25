"""Transport-neutral contracts for RAW, handlers, publication and recovery."""

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Protocol
from uuid import UUID


@dataclass(frozen=True)
class RawArtifactReference:
    """Reference to immutable RAW bytes owned by a future storage adapter."""

    artifact_reference: str
    checksum: str
    manifest: dict[str, Any]
    checksum_algorithm: str = "sha256"
    immutable: bool = True

    def __post_init__(self) -> None:
        if not self.artifact_reference.strip():
            raise ValueError("artifact_reference is required")
        if not self.checksum.strip():
            raise ValueError("checksum is required")
        if not self.immutable:
            raise ValueError("RAW artifacts must be immutable")


@dataclass(frozen=True)
class ValidationResult:
    accepted: bool
    errors: tuple[str, ...] = ()
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.accepted and self.errors:
            raise ValueError("accepted validation cannot contain errors")
        if not self.accepted and not self.errors:
            raise ValueError("rejected validation must explain why")


@dataclass(frozen=True)
class StagingResult:
    """Validated candidate pointer, not a domain-table publication."""

    staging_pointer: str
    validation: ValidationResult
    checksum: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.staging_pointer.strip():
            raise ValueError("staging_pointer is required")


@dataclass(frozen=True)
class ExecutionCounters:
    records_seen: int = 0
    records_written: int = 0
    records_rejected: int = 0
    records_duplicated: int = 0
    records_published: int = 0

    def __post_init__(self) -> None:
        values = (
            self.records_seen,
            self.records_written,
            self.records_rejected,
            self.records_duplicated,
            self.records_published,
        )
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in values
        ):
            raise ValueError("execution counters must be non-negative integers")

    def as_dict(self) -> dict[str, int]:
        return {
            "records_seen": self.records_seen,
            "records_written": self.records_written,
            "records_rejected": self.records_rejected,
            "records_duplicated": self.records_duplicated,
            "records_published": self.records_published,
        }


@dataclass(frozen=True)
class SourceChangeSummary:
    """Admin-safe publication delta shared by every source adapter.

    A metric may be unknown, but it may never be silently invented. Unknown
    values therefore require a non-empty reason keyed by the metric name.
    """

    matched_companies: int | None
    new_facts: int | None
    changed_facts: int | None
    removed_or_expired_facts: int | None
    unchanged_facts: int | None
    replayed_facts: int | None
    quarantined_records: int | None
    source_records: int | None
    source_data_date: date | None
    previous_source_data_date: date | None
    unavailable_reasons: dict[str, str] = field(default_factory=dict)

    _METRIC_NAMES = (
        "matched_companies",
        "new_facts",
        "changed_facts",
        "removed_or_expired_facts",
        "unchanged_facts",
        "replayed_facts",
        "quarantined_records",
        "source_records",
        "source_data_date",
        "previous_source_data_date",
    )

    def __post_init__(self) -> None:
        for name in self._METRIC_NAMES:
            value = getattr(self, name)
            if name.endswith("_date"):
                if value is not None and not isinstance(value, date):
                    raise ValueError(f"{name} must be a date or null")
            elif value is not None and (
                not isinstance(value, int) or isinstance(value, bool) or value < 0
            ):
                raise ValueError(f"{name} must be a non-negative integer or null")

            reason = str(self.unavailable_reasons.get(name) or "").strip()
            if value is None and not reason:
                raise ValueError(f"null change-summary metric requires reason: {name}")
            if value is not None and name in self.unavailable_reasons:
                raise ValueError(
                    f"available change-summary metric cannot have a reason: {name}"
                )

        unknown = set(self.unavailable_reasons) - set(self._METRIC_NAMES)
        if unknown:
            raise ValueError(
                "unknown change-summary reason fields: " + ", ".join(sorted(unknown))
            )

    def as_dict(self) -> dict[str, Any]:
        return {
            name: (
                getattr(self, name).isoformat()
                if isinstance(getattr(self, name), date)
                else getattr(self, name)
            )
            for name in self._METRIC_NAMES
        } | {"unavailable_reasons": dict(self.unavailable_reasons)}


@dataclass(frozen=True)
class HandlerResult:
    raw_artifacts: tuple[RawArtifactReference, ...] = ()
    staging_result: StagingResult | None = None
    checksum_metadata: dict[str, Any] = field(default_factory=dict)
    counters: ExecutionCounters | None = None
    change_summary: SourceChangeSummary | None = None


@dataclass(frozen=True)
class HandlerContext:
    job_id: UUID
    run_id: UUID
    source_id: str
    worker_id: str
    fencing_token: int
    deadline_at: datetime
    schedule_metadata: dict[str, Any]
    heartbeat: Callable[[], None]
    report_counters: Callable[[ExecutionCounters], None]
    shutdown_requested: Callable[[], bool]

    def ensure_active(self, *, now: datetime) -> None:
        if now >= self.deadline_at:
            from app.worker.errors import WorkerTimeoutError

            raise WorkerTimeoutError("worker execution deadline exceeded")


class WorkerHandler(Protocol):
    def __call__(self, context: HandlerContext) -> HandlerResult: ...


class WorkerPublisher(Protocol):
    """Publish a prepared handler result inside the worker success transaction."""

    def __call__(self, session: Any, claim: Any, result: HandlerResult) -> HandlerResult: ...


class RawStorage(Protocol):
    """Future adapter boundary; no production object store is implemented."""

    def put_immutable(
        self, *, content: bytes, manifest: dict[str, Any]
    ) -> RawArtifactReference: ...

    def verify(self, artifact: RawArtifactReference) -> bool: ...


class PublicationBackend(Protocol):
    """Future staging backend; SQL only moves validated pointer metadata."""

    def validate(self, staging_pointer: str) -> ValidationResult: ...

    def discard(self, staging_pointer: str) -> None: ...


@dataclass(frozen=True)
class RecoveryAcceptancePoint:
    name: str
    description: str
    required: bool = True


class BackupRecoveryBackend(Protocol):
    """Backup/recovery interface only; DEV-008 supplies no implementation."""

    def create_backup(self, *, label: str) -> str: ...

    def restore_to_staging(self, *, backup_reference: str) -> str: ...

    def verify_restore(
        self,
        *,
        staging_reference: str,
        acceptance_points: tuple[RecoveryAcceptancePoint, ...],
    ) -> ValidationResult: ...
