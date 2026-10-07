from datetime import datetime, timezone

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.company_enrichment import CompanySourceCoverage
from app.models.source import DataSet
from app.models.tax_regime import (
    CompanyTaxRegimeSnapshot,
)
from app.services.check_result import build_check_result
from app.models.worker import WorkerPublicationState
from app.contracts.company_view_v1 import DataState
from app.services.fns_tax_regime_readiness import (
    REQUIRED_CODES,
    evaluate_family_readiness,
    resolve_current_company_coverage,
)


FAMILY_DATASET_CODE = "fns_tax_regime"
LEGAL_DATASET_CODE = "fns_snr"
IP_DATASET_CODE = "fns_snrip"
SOURCE_CODE = FAMILY_DATASET_CODE


REGIME_NAMES = {
    "usn": "Упрощённая система налогообложения (УСН)",
    "ausn": (
        "Автоматизированная упрощённая "
        "система налогообложения (АУСН)"
    ),
    "eshn": (
        "Единый сельскохозяйственный налог (ЕСХН)"
    ),
    "psn": "Патентная система налогообложения (ПСН)",
    "npd": "Налог на профессиональный доход (НПД)",
    "srp": (
        "Система налогообложения при выполнении "
        "соглашения о разделе продукции (СРП)"
    ),
}


def get_regime_name(
    regime_code,
):
    if regime_code is None:
        return None

    return REGIME_NAMES.get(
        str(regime_code).strip()
    )


def _empty_payload():
    return {
        "entity_type": None,
        "regime_codes": [],
        "regimes": [],
        "source_document_id": None,
        "source_document_date": None,
        "provenance": None,
    }


def _provenance_payload(family, member):
    coverage = dict(member.coverage or {})
    return {
        "source": "fns",
        "source_id": FAMILY_DATASET_CODE,
        "member_dataset_code": member.code,
        "official_source_url": member.source_url,
        "source_data_date": member.last_data_date,
        "retrieved_at": member.retrieved_at,
        "published_at": member.published_at,
        "family_release_identity": dict(family.coverage or {}).get(
            "release_identity"
        ),
        "member_release_identity": coverage.get("release_identity"),
        "artifact_sha256": coverage.get("artifact_sha256"),
        "xsd_sha256": coverage.get("xsd_sha256"),
    }


def get_tax_regime_check_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
):
    """Return the applicable member only when the complete family is trusted."""

    now = now or datetime.now(timezone.utc)
    session = get_session()
    try:
        company = session.execute(
            select(Company.id, Company.inn, Company.entity_type).where(
                Company.id == company_id
            )
        ).mappings().one_or_none()
        if company is None:
            return build_check_result(
                checked=False,
                applicable=None,
                result="unavailable",
                data_date=None,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason="company_not_found",
                semantic_state=DataState.SOURCE_UNAVAILABLE.value,
                source_data_date=None,
                member_dataset_code=None,
                **_empty_payload(),
            )

        inn = str(company["inn"] or "").strip()
        entity_type = company["entity_type"]
        is_legal = (
            entity_type == "legal" and len(inn) == 10
        ) or (
            entity_type is None and len(inn) == 10
        )
        is_ip = (
            entity_type == "individual_entrepreneur" and len(inn) == 12
        ) or (
            entity_type is None and len(inn) == 12
        )
        if not inn.isdigit():
            is_legal = False
            is_ip = False

        if is_legal:
            member_code = LEGAL_DATASET_CODE
        elif is_ip:
            member_code = IP_DATASET_CODE
        else:
            return build_check_result(
                checked=True,
                applicable=False,
                result="not_applicable",
                data_date=None,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason="legal_or_individual_entrepreneur_only",
                semantic_state=DataState.NOT_APPLICABLE.value,
                source_data_date=None,
                member_dataset_code=None,
                **_empty_payload(),
            )

        datasets = {
            dataset.code: dataset
            for dataset in session.scalars(
                select(DataSet).where(DataSet.code.in_(REQUIRED_CODES))
            )
        }
        family = datasets.get(FAMILY_DATASET_CODE)
        member = datasets.get(member_code)
        readiness = evaluate_family_readiness(datasets, now=now)
        if readiness.state != DataState.FOUND:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=member.last_data_date if member else None,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason=readiness.reason,
                semantic_state=readiness.state.value,
                source_data_date=readiness.source_data_date,
                member_dataset_code=member_code,
                **_empty_payload(),
            )

        provenance = _provenance_payload(family, member)

        snapshot = session.scalar(
            select(CompanyTaxRegimeSnapshot)
            .where(
                CompanyTaxRegimeSnapshot.company_id == company_id,
                CompanyTaxRegimeSnapshot.dataset_id == member.id,
                CompanyTaxRegimeSnapshot.data_date == member.last_data_date,
            )
            .order_by(
                CompanyTaxRegimeSnapshot.data_date.desc(),
                CompanyTaxRegimeSnapshot.id.desc(),
            )
            .limit(1)
        )
        if snapshot is None:
            coverage = resolve_current_company_coverage(
                session.scalars(
                    select(CompanySourceCoverage).where(
                        CompanySourceCoverage.company_id == company_id,
                        CompanySourceCoverage.source_id == FAMILY_DATASET_CODE,
                    )
                ),
                session.get(WorkerPublicationState, FAMILY_DATASET_CODE),
                company_id=company_id,
                source_data_date=readiness.source_data_date,
                release_identity=readiness.release_identity,
            )
            proven_negative = coverage.state == DataState.NOT_FOUND
            return build_check_result(
                checked=proven_negative,
                applicable=True,
                result="not_found" if proven_negative else "unavailable",
                data_date=member.last_data_date,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason=coverage.reason,
                semantic_state=(
                    DataState.NOT_FOUND.value
                    if proven_negative
                    else coverage.state.value
                    if coverage.state != DataState.FOUND
                    else DataState.NOT_CHECKED.value
                ),
                source_data_date=member.last_data_date,
                member_dataset_code=member_code,
                limitation=(
                    "Текущий принятый выпуск ФНС проверен для этого ИНН; "
                    "специальный режим не найден."
                    if proven_negative
                    else "Проверка ИНН по текущему принятому выпуску ФНС не завершена."
                ),
                **{
                    **_empty_payload(),
                    "provenance": provenance,
                },
            )

        regime_codes = list(snapshot.regime_codes or [])
        return build_check_result(
            checked=True,
            applicable=True,
            result="found",
            data_date=snapshot.data_date,
            dataset_code=FAMILY_DATASET_CODE,
            source=SOURCE_CODE,
            reason=None,
            semantic_state=DataState.FOUND.value,
            source_data_date=member.last_data_date,
            member_dataset_code=member_code,
            company_id=snapshot.company_id,
            entity_type=snapshot.entity_type,
            regime_codes=regime_codes,
            regimes=[
                {"code": code, "name": get_regime_name(code) or code}
                for code in regime_codes
            ],
            dataset_id=snapshot.dataset_id,
            source_document_id=snapshot.source_document_id,
            source_document_date=snapshot.source_document_date,
            provenance=provenance,
        )
    finally:
        session.close()


def get_tax_regime_profile_for_company(
    company_id,
    *,
    now: datetime | None = None,
):
    """
    Возвращает последний доступный snapshot
    специальных налоговых режимов компании.
    """

    check = get_tax_regime_check_for_company(company_id, now=now)
    if check["result"] != "found":
        return None
    return {
        "company_id": check["company_id"],
        "entity_type": check["entity_type"],
        "data_date": check["data_date"],
        "regime_codes": check["regime_codes"],
        "regimes": check["regimes"],
        "dataset_id": check["dataset_id"],
        "dataset_code": check["member_dataset_code"],
        "source_document_id": check["source_document_id"],
        "source_document_date": check["source_document_date"],
        "provenance": check["provenance"],
    }
