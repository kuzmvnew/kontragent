"""Narrow, fail-closed synchronization of persisted dataset applicability."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Literal

from sqlalchemy import BigInteger, bindparam, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Session

from app.models.source import DataSet
from app.services.dataset_applicability_policy import (
    CANONICAL_DATASET_POLICIES,
    canonical_dataset_applicability,
)
from app.services.source_applicability_service import parse_source_applicability


SyncAction = Literal["NO_CHANGE", "SET", "INVALID_CURRENT", "UNMAPPED"]


@dataclass(frozen=True)
class DatasetApplicabilityPlanItem:
    dataset_id: int
    code: str
    current_applicability: Any
    canonical_applicability: dict[str, list[str]] | None
    action: SyncAction

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class DatasetApplicabilitySyncPlan:
    items: tuple[DatasetApplicabilityPlanItem, ...]
    totals: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "items": [item.as_dict() for item in self.items],
            "totals": dict(self.totals),
        }


@dataclass(frozen=True)
class DatasetApplicabilityApplyResult:
    mutations: int
    plan_before: DatasetApplicabilitySyncPlan
    plan_after: DatasetApplicabilitySyncPlan

    def as_dict(self) -> dict[str, Any]:
        return {
            "mutations": self.mutations,
            "plan_before": self.plan_before.as_dict(),
            "plan_after": self.plan_after.as_dict(),
        }


def _build_plan(rows: tuple[DataSet, ...]) -> DatasetApplicabilitySyncPlan:
    items: list[DatasetApplicabilityPlanItem] = []
    valid_before = missing_before = invalid_before = will_set = will_change = 0
    unmapped = 0
    for dataset in rows:
        current = dataset.applicability
        current_valid = parse_source_applicability(current) is not None
        valid_before += int(current_valid)
        missing_before += int(current is None)
        invalid_before += int(current is not None and not current_valid)
        if dataset.code not in CANONICAL_DATASET_POLICIES:
            action: SyncAction = "UNMAPPED"
            canonical = None
            unmapped += 1
        else:
            canonical = canonical_dataset_applicability(dataset.code)
            if current == canonical:
                action = "NO_CHANGE"
            elif current is not None and not current_valid:
                action = "INVALID_CURRENT"
                will_set += 1
                will_change += 1
            else:
                action = "SET"
                will_set += 1
                will_change += 1
        items.append(
            DatasetApplicabilityPlanItem(
                dataset_id=dataset.id,
                code=dataset.code,
                current_applicability=current,
                canonical_applicability=canonical,
                action=action,
            )
        )
    total = len(items)
    return DatasetApplicabilitySyncPlan(
        items=tuple(items),
        totals={
            "total": total,
            "valid_before": valid_before,
            "missing_before": missing_before,
            "invalid_before": invalid_before,
            "will_set": will_set,
            "will_change": will_change,
            "unmapped": unmapped,
            "valid_after_expected": total - unmapped,
        },
    )


def plan_dataset_applicability_sync(
    session: Session, *, lock: bool = False, skip_locked: bool = False
) -> DatasetApplicabilitySyncPlan:
    """Inspect every persisted dataset; without ``lock`` this is read-only."""

    statement = select(DataSet).order_by(DataSet.code, DataSet.id)
    if lock:
        statement = statement.with_for_update(skip_locked=skip_locked)
    return _build_plan(tuple(session.scalars(statement)))


def apply_dataset_applicability_sync(
    session: Session,
) -> DatasetApplicabilityApplyResult:
    """Update only ``DataSet.applicability`` in the caller's transaction."""

    before = plan_dataset_applicability_sync(session, lock=True)
    if before.totals["unmapped"]:
        codes = ", ".join(
            item.code for item in before.items if item.action == "UNMAPPED"
        )
        raise RuntimeError(f"applicability apply blocked by unmapped datasets: {codes}")
    mutations = tuple(item for item in before.items if item.action != "NO_CHANGE")
    if mutations:
        # Deliberately bypass the model's generic ``updated_at`` on-update
        # hook: the approved recovery mutation is exactly one column.
        statement = text(
            "UPDATE data_sets "
            "SET applicability = :applicability "
            "WHERE id = :dataset_id"
        ).bindparams(
            bindparam("applicability", type_=JSONB),
            bindparam("dataset_id", type_=BigInteger),
        )
        session.execute(
            statement,
            [
                {
                    "dataset_id": item.dataset_id,
                    "applicability": dict(item.canonical_applicability or {}),
                }
                for item in mutations
            ],
        )
        session.expire_all()
    after = plan_dataset_applicability_sync(session)
    if after.totals["unmapped"] or after.totals["valid_before"] != len(after.items):
        raise RuntimeError("applicability synchronization postcondition failed")
    return DatasetApplicabilityApplyResult(
        mutations=len(mutations),
        plan_before=before,
        plan_after=after,
    )
