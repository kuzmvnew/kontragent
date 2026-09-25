"""Read-only Firmoteka bridge projection for API/card consumers."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select

from app.contracts.data_readiness import is_dataset_stale
from app.database.postgres import get_session
from app.models.firmoteka import FirmotekaCompanySnapshot
from app.models.source import DataSet
from app.services.check_result import build_check_result


def get_cached_firmoteka_check(inn: str | None) -> dict:
    value = "".join(character for character in str(inn or "") if character.isdigit())
    if len(value) not in {10, 12}:
        return build_check_result(
            checked=True,
            applicable=False,
            result="not_applicable",
            data_date=None,
            dataset_code="firmoteka",
            source="firmoteka",
            reason="invalid_inn",
            records=[],
            record_count=0,
        )
    session = get_session()
    try:
        dataset = session.scalar(select(DataSet).where(DataSet.code == "firmoteka"))
        snapshot = session.scalar(
            select(FirmotekaCompanySnapshot)
            .where(
                FirmotekaCompanySnapshot.inn == value,
                FirmotekaCompanySnapshot.is_current.is_(True),
            )
            .order_by(FirmotekaCompanySnapshot.retrieved_at.desc())
            .limit(1)
        )
        if dataset is None or snapshot is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code="firmoteka",
                source="firmoteka",
                reason="not_checked",
                matching_method="inn_exact",
                records=[],
                record_count=None,
                source_url=f"https://firmoteka.ru/{value}",
            )
        stale = is_dataset_stale(
            now=datetime.now(timezone.utc),
            freshness_policy=dataset.freshness_policy,
            source_as_of=dataset.source_as_of,
            last_success_at=dataset.last_success_at,
        )
        if stale or dataset.operational_status in {"stale", "error", "unavailable"}:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=snapshot.source_as_of,
                dataset_code="firmoteka",
                source="firmoteka",
                reason="stale_bridge_snapshot",
                matching_method="inn_exact",
                records=[],
                record_count=None,
                source_url=snapshot.source_url,
                last_known_record=snapshot.projection,
                checked_at=snapshot.retrieved_at,
            )
        return build_check_result(
            checked=True,
            applicable=True,
            result="found",
            data_date=snapshot.source_as_of or snapshot.retrieved_at.date(),
            dataset_code="firmoteka",
            source="firmoteka",
            reason=None,
            matching_method="inn_exact",
            records=[snapshot.projection],
            record_count=1,
            source_url=snapshot.source_url,
            checked_at=snapshot.retrieved_at,
            source_class="authorized_bridge",
            raw_sha256=snapshot.raw_sha256,
            limitations=[
                "Secondary bridge evidence; not an official-registry assertion.",
                "Absence is never exposed as a clean official negative.",
            ],
        )
    finally:
        session.close()
