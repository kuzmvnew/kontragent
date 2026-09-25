from datetime import datetime

from sqlalchemy import select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_regime import (
    CompanyTaxRegimeSnapshot,
)
from app.services.check_result import build_check_result
from app.services.data_readiness_service import clean_negative_blocker


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
    }


def get_tax_regime_check_for_company(
    company_id: int,
    *,
    now: datetime | None = None,
):
    """Return the applicable SNR/SNRIP member under family freshness."""

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
                source_data_date=None,
                member_dataset_code=None,
                **_empty_payload(),
            )

        datasets = {
            dataset.code: dataset
            for dataset in session.scalars(
                select(DataSet).where(
                    DataSet.code.in_((FAMILY_DATASET_CODE, member_code))
                )
            )
        }
        family = datasets.get(FAMILY_DATASET_CODE)
        member = datasets.get(member_code)
        if family is None or member is None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=None,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason="dataset_not_registered",
                source_data_date=None,
                member_dataset_code=member_code,
                **_empty_payload(),
            )

        blocker = clean_negative_blocker(family, now=now)
        if blocker is None:
            blocker = clean_negative_blocker(member, now=now)
        if blocker is not None:
            return build_check_result(
                checked=False,
                applicable=True,
                result="unavailable",
                data_date=member.last_data_date,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason=blocker,
                source_data_date=member.last_data_date,
                member_dataset_code=member_code,
                **_empty_payload(),
            )

        snapshot = session.scalar(
            select(CompanyTaxRegimeSnapshot)
            .where(
                CompanyTaxRegimeSnapshot.company_id == company_id,
                CompanyTaxRegimeSnapshot.dataset_id == member.id,
            )
            .order_by(
                CompanyTaxRegimeSnapshot.data_date.desc(),
                CompanyTaxRegimeSnapshot.id.desc(),
            )
            .limit(1)
        )
        if snapshot is None:
            return build_check_result(
                checked=True,
                applicable=True,
                result="not_found",
                data_date=member.last_data_date,
                dataset_code=FAMILY_DATASET_CODE,
                source=SOURCE_CODE,
                reason=None,
                source_data_date=member.last_data_date,
                member_dataset_code=member_code,
                limitation=(
                    "Отсутствие означает только, что ИНН не найден в текущем "
                    "официальном наборе применимых специальных режимов."
                ),
                **_empty_payload(),
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
    }
