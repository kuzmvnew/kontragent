"""Conservative C6 family and per-company frozen-release evidence rules.

Both the legacy check/Card and Company View use these pure decisions.  A
projected snapshot proves a positive; a negative needs a terminal company
coverage row from the currently accepted, two-member release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from app.contracts.company_view_v1 import DataState
from app.contracts.data_readiness import is_dataset_stale


FAMILY_CODE = "fns_tax_regime"
LEGAL_CODE = "fns_snr"
IP_CODE = "fns_snrip"
REQUIRED_CODES = (FAMILY_CODE, LEGAL_CODE, IP_CODE)


def _field(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            pass
    return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class FamilyReadiness:
    state: DataState
    reason: str | None
    source_data_date: date | None
    release_identity: str | None


def evaluate_family_readiness(
    datasets: Mapping[str, Any], *, now: datetime
) -> FamilyReadiness:
    """Require the family and both pinned members to be current together."""

    now = _datetime(now) or datetime.now(timezone.utc)
    family = datasets.get(FAMILY_CODE)
    family_date = _date(_field(family, "last_data_date")) if family else None
    family_coverage = dict(_field(family, "coverage") or {}) if family else {}
    release_identity = family_coverage.get("release_identity")
    failures: list[tuple[int, DataState, str]] = []
    for code in REQUIRED_CODES:
        dataset = datasets.get(code)
        if dataset is None:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_not_registered"))
            continue
        status = str(_field(dataset, "operational_status") or "").lower()
        error = str(_field(dataset, "last_error") or "").lower()
        if status == "error" and any(
            token in error for token in ("schema", "xsd", "xml", "pars")
        ):
            failures.append((4, DataState.PARSING_ERROR, f"{code}:parsing_error"))
        elif status in {"error", "unavailable", "source_blocked", "access_pending"}:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_{status}"))
        elif not _field(dataset, "last_success_at") or not _date(_field(dataset, "last_data_date")):
            failures.append((1, DataState.NOT_CHECKED, f"{code}:not_checked"))
        elif not _field(dataset, "enabled"):
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_disabled"))
        elif status == "stale" or (
            _date(_field(dataset, "official_actual_until")) is not None
            and now.date() > _date(_field(dataset, "official_actual_until"))
        ):
            failures.append((2, DataState.STALE_DATA, f"{code}:dataset_stale"))
        elif status != "current" or _date(_field(dataset, "official_actual_until")) is None:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:currentness_unavailable"))
        elif is_dataset_stale(
            now=now,
            freshness_policy=str(_field(dataset, "freshness_policy") or "irregular"),
            source_as_of=_datetime(_field(dataset, "source_as_of")),
            last_success_at=_datetime(_field(dataset, "last_success_at")),
            threshold_seconds=_field(dataset, "freshness_threshold_seconds"),
        ):
            failures.append((2, DataState.STALE_DATA, f"{code}:dataset_stale"))

    if failures:
        _priority, state, reason = max(failures, key=lambda failure: failure[0])
        return FamilyReadiness(state, reason, family_date, release_identity)

    dates = {_date(_field(datasets[code], "last_data_date")) for code in REQUIRED_CODES}
    members = family_coverage.get("members") or {}
    expected = {LEGAL_CODE: "legal", IP_CODE: "ip"}
    compatible = (
        len(dates) == 1
        and bool(release_identity)
        and all(
            (members.get(name) or {}).get("release_identity")
            and (members.get(name) or {}).get("release_identity")
            == (dict(_field(datasets[code], "coverage") or {})).get("release_identity")
            for code, name in expected.items()
        )
    )
    if not compatible:
        return FamilyReadiness(
            DataState.NOT_CHECKED, "family_release_unverified", family_date, release_identity
        )
    return FamilyReadiness(DataState.FOUND, None, family_date, release_identity)


@dataclass(frozen=True)
class CoverageDecision:
    state: DataState
    reason: str
    coverage: Any | None = None


def resolve_current_company_coverage(
    rows: Iterable[Any],
    publication: Any | None,
    *,
    company_id: int,
    source_data_date: date | None,
    release_identity: str | None,
) -> CoverageDecision:
    """Accept only coverage frozen to the current pointer, checksum and release."""

    validation = dict(_field(publication, "validation_metadata") or {}) if publication else {}
    accepted_release = (validation.get("validation") or {}).get("release_identity")
    pointer = _field(publication, "active_pointer") if publication else None
    checksum = validation.get("checksum")
    generation = _field(publication, "generation") if publication else None
    if not (pointer and checksum and generation and source_data_date and release_identity):
        return CoverageDecision(DataState.NOT_CHECKED, "current_coverage_unavailable")
    if accepted_release != release_identity:
        return CoverageDecision(DataState.NOT_CHECKED, "current_release_unverified")

    current = []
    for row in rows:
        snapshot = dict(_field(row, "source_snapshot") or {})
        if (
            _field(row, "company_id") == company_id
            and _field(row, "source_id") == FAMILY_CODE
            and _field(row, "worker_source_id") == FAMILY_CODE
            and _field(row, "mode") == "local_bulk_replay"
            and _field(row, "publication_generation") == generation
            and _field(row, "replay_pointer") == pointer
            and _field(row, "replay_checksum") == checksum
            and _date(_field(row, "source_data_date")) == source_data_date
            and snapshot.get("release_identity") == release_identity
        ):
            current.append(row)
    if not current:
        return CoverageDecision(DataState.NOT_CHECKED, "company_not_checked_on_current_release")

    current.sort(
        key=lambda row: (
            _datetime(_field(row, "updated_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(_field(row, "id") or ""),
        ),
        reverse=True,
    )
    row = current[0]
    status = str(_field(row, "status") or "")
    execution = str(_field(row, "execution_status") or "")
    if execution == "succeeded" and _field(row, "checked_at"):
        if status == "NOT_FOUND" and _field(row, "fact_count") == 0:
            return CoverageDecision(DataState.NOT_FOUND, "checked_current_release", row)
        if status == "FOUND":
            return CoverageDecision(DataState.FOUND, "checked_current_release", row)
        if status in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "STALE_DATA"}:
            return CoverageDecision(DataState(status), f"coverage_{status.lower()}", row)
    if status in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "STALE_DATA"}:
        return CoverageDecision(DataState(status), f"coverage_{status.lower()}", row)
    if status == "ACCESS_REQUIRED":
        return CoverageDecision(DataState.SOURCE_UNAVAILABLE, "coverage_access_required", row)
    return CoverageDecision(DataState.NOT_CHECKED, "company_check_pending", row)
