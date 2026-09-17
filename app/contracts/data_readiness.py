"""Operational vocabulary for dataset freshness and update automation."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import StrEnum


class DatasetKind(StrEnum):
    BULK_SNAPSHOT = "bulk_snapshot"
    ON_DEMAND_API = "on_demand_api"
    PUBLIC_LIVE_USER_TRIGGERED = "public_live_user_triggered"
    HUMAN_ASSISTED = "human_assisted"
    SOURCE_BLOCKED = "source_blocked"
    ACCESS_CREDENTIAL_PENDING = "access_credential_pending"


class FreshnessPolicy(StrEnum):
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    IRREGULAR = "irregular"
    ON_DEMAND = "on_demand"
    USER_TRIGGERED = "user_triggered"
    MANUAL = "manual"
    ACCESS_PENDING = "access_pending"


class OperationalStatus(StrEnum):
    CURRENT = "current"
    STALE = "stale"
    UPDATING = "updating"
    ERROR = "error"
    UNAVAILABLE = "unavailable"
    SOURCE_BLOCKED = "source_blocked"
    ACCESS_PENDING = "access_pending"
    MANUAL = "manual"
    USER_TRIGGERED = "user_triggered"
    NOT_CONFIGURED = "not_configured"


class AutoUpdateStatus(StrEnum):
    NOT_CONFIGURED = "not_configured"
    MANUAL = "manual"
    USER_TRIGGERED = "user_triggered"
    HUMAN_ASSISTED = "human_assisted"
    CONFIGURED = "configured"
    SOURCE_BLOCKED = "source_blocked"
    ACCESS_PENDING = "access_pending"


class RunStatus(StrEnum):
    RUNNING = "running"
    SUCCESS = "success"
    FAILED = "failed"
    SKIPPED = "skipped"


class RetryDisposition(StrEnum):
    RETRY = "retry"
    SOURCE_BLOCKED = "source_blocked"
    ACCESS_PENDING = "access_pending"
    FAIL = "fail"


DEFAULT_FRESHNESS_THRESHOLDS = {
    FreshnessPolicy.DAILY: timedelta(hours=36),
    FreshnessPolicy.WEEKLY: timedelta(days=9),
    FreshnessPolicy.MONTHLY: timedelta(days=40),
}


def freshness_reference(
    *,
    source_as_of: datetime | None,
    last_success_at: datetime | None,
) -> datetime | None:
    """Prefer the publisher's data timestamp over our retrieval timestamp."""
    return source_as_of or last_success_at


def is_dataset_stale(
    *,
    now: datetime,
    freshness_policy: str,
    source_as_of: datetime | None,
    last_success_at: datetime | None,
    threshold_seconds: int | None = None,
) -> bool:
    """Return freshness only; failures are represented independently."""
    try:
        policy = FreshnessPolicy(freshness_policy)
    except ValueError:
        return False

    if policy not in DEFAULT_FRESHNESS_THRESHOLDS and threshold_seconds is None:
        return False

    reference = freshness_reference(
        source_as_of=source_as_of,
        last_success_at=last_success_at,
    )
    if reference is None:
        return True

    threshold = (
        timedelta(seconds=threshold_seconds)
        if threshold_seconds is not None
        else DEFAULT_FRESHNESS_THRESHOLDS[policy]
    )
    return now > reference + threshold


def retry_delay(
    retry_count: int,
    *,
    retry_after_seconds: int | None = None,
    base_seconds: int = 60,
    maximum_seconds: int = 6 * 60 * 60,
) -> timedelta:
    """Bounded exponential backoff, honouring a provider Retry-After hint."""
    exponential = min(maximum_seconds, base_seconds * (2 ** max(retry_count - 1, 0)))
    seconds = max(exponential, retry_after_seconds or 0)
    return timedelta(seconds=min(seconds, maximum_seconds))


def classify_network_failure(
    *,
    http_status: int | None = None,
    error_type: str | None = None,
) -> RetryDisposition:
    if http_status == 403 or error_type in {"protection", "source_protection", "partial_file"}:
        return RetryDisposition.SOURCE_BLOCKED
    if error_type in {"credentials_missing", "access_pending"}:
        return RetryDisposition.ACCESS_PENDING
    if http_status == 429 or error_type in {"timeout", "network_error", "download_failed"}:
        return RetryDisposition.RETRY
    return RetryDisposition.FAIL


@dataclass(frozen=True)
class Coverage:
    records: int | None = None
    unique_inns: int | None = None
    master_matches: int | None = None
    master_unmatched: int | None = None
    checked_inns: int | None = None
    cached_inns: int | None = None
    unchecked_master: int | None = None
    note: str | None = None

    def as_dict(self) -> dict:
        return {key: value for key, value in self.__dict__.items() if value is not None}
