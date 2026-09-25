"""Create truthful, source-neutral change summaries for worker runs."""

from datetime import date, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.admin import SourceChangeSummary
from app.models.source import DataSet
from app.worker.contracts import HandlerResult


WORKER_SOURCE_DATASET_CODES = {"S02": "fns_tax_debt"}


def _known_count(*values: Any) -> int | None:
    for value in values:
        if isinstance(value, bool):
            continue
        if isinstance(value, int) and value >= 0:
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    return None


def _source_date(dataset: DataSet | None) -> date | None:
    if dataset is None:
        return None
    if dataset.last_data_date is not None:
        return dataset.last_data_date
    if dataset.source_as_of is not None:
        return dataset.source_as_of.date()
    return None


def persist_source_change_summary(
    session: Session,
    *,
    source_id: str,
    run_id,
    result: HandlerResult,
    created_at: datetime,
) -> SourceChangeSummary:
    """Persist known counters and leave unknowable values NULL.

    A NULL is intentional: the admin UI renders it as ``N/A — legacy run`` or
    ``N/A — not reported``.  In particular, a full snapshot's published count
    is not silently relabelled as "new facts".
    """

    existing = session.scalar(
        select(SourceChangeSummary).where(SourceChangeSummary.run_id == run_id)
    )
    if existing is not None:
        return existing

    dataset_code = WORKER_SOURCE_DATASET_CODES.get(source_id, source_id)
    dataset = session.scalar(select(DataSet).where(DataSet.code == dataset_code))
    coverage = dict(dataset.coverage or {}) if dataset is not None else {}
    checksum = dict(result.checksum_metadata or {})
    validation = (
        dict(result.staging_result.validation.metadata or {})
        if result.staging_result is not None
        else {}
    )
    metadata = {**coverage, **validation, **checksum}
    latest = session.scalar(
        select(SourceChangeSummary)
        .where(SourceChangeSummary.source_id == source_id)
        .order_by(SourceChangeSummary.created_at.desc(), SourceChangeSummary.id.desc())
        .limit(1)
    )

    new_facts = _known_count(metadata.get("new_facts"))
    replayed_facts = _known_count(metadata.get("replayed_facts"))
    if replayed_facts is None and metadata.get("replayed") is True:
        replayed_facts = new_facts

    summary = SourceChangeSummary(
        source_id=source_id,
        run_id=run_id,
        matched_companies=_known_count(
            metadata.get("matched_companies"),
            metadata.get("risk_summary_candidate_companies"),
        ),
        new_facts=new_facts,
        changed_facts=_known_count(metadata.get("changed_facts")),
        removed_or_expired_facts=_known_count(
            metadata.get("removed_or_expired_facts"),
            metadata.get("removed_facts"),
            metadata.get("expired_facts"),
        ),
        unchanged_facts=_known_count(metadata.get("unchanged_facts")),
        replayed_facts=replayed_facts,
        quarantined_records=_known_count(metadata.get("quarantined_records")),
        source_records=_known_count(
            metadata.get("source_records"),
            result.counters.records_seen if result.counters else None,
        ),
        source_data_date=_source_date(dataset),
        previous_source_data_date=latest.source_data_date if latest else None,
        created_at=created_at,
    )
    session.add(summary)
    return summary
