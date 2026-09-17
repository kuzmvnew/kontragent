from __future__ import annotations

from collections import defaultdict
from datetime import date
import re

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.ingestion.roskomnadzor import save_pd_operator_check
from app.models.roskomnadzor import RoskomnadzorCompanyFact, RoskomnadzorPdOperatorCheck
from app.models.source import DataSet
from app.providers.roskomnadzor_provider import RoskomnadzorPdOperatorProvider, RoskomnadzorProviderError
from app.services.check_result import build_check_result
from app.services.roskomnadzor_registry_service import DATASETS, SOURCE_CODE, ensure_roskomnadzor_datasets

PUBLIC_CHANNELS = ("communications", "broadcast", "media", "information_distributors", "hosting")


def _valid_company_inn(inn):
    value = re.sub(r"\D", "", str(inn or ""))
    return value if len(value) == 10 else None


def get_roskomnadzor_bulk_check_for_inn(inn, channel):
    inn = _valid_company_inn(inn)
    if channel not in PUBLIC_CHANNELS:
        raise ValueError("Неизвестный канал Роскомнадзора")
    code = DATASETS[channel]
    if not inn:
        return build_check_result(checked=True, applicable=False, result="not_applicable", data_date=None, dataset_code=code, source=SOURCE_CODE, reason="company_layer_requires_legal_entity_inn10", matching_method=None, records=[], record_count=0)
    session = get_session()
    try:
        dataset = session.scalar(select(DataSet).where(DataSet.code == code))
        if dataset is None or dataset.last_data_date is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=code, source=SOURCE_CODE, reason="dataset_not_loaded", matching_method="inn_exact", records=[], record_count=None)
        total = session.scalar(select(func.count()).select_from(RoskomnadzorCompanyFact).where(RoskomnadzorCompanyFact.dataset_id == dataset.id))
        if not total:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=dataset.last_data_date, dataset_code=code, source=SOURCE_CODE, reason="dataset_snapshot_missing", matching_method="inn_exact", records=[], record_count=None)
        rows = session.scalars(select(RoskomnadzorCompanyFact).where(RoskomnadzorCompanyFact.dataset_id == dataset.id, RoskomnadzorCompanyFact.inn == inn).order_by(RoskomnadzorCompanyFact.external_number)).all()
        records = [{"external_number": row.external_number, "name": row.name, "ogrn": row.ogrn, "status": row.status, "issued_at": row.issued_at, "valid_until": row.valid_until, "details": dict(row.public_details or {})} for row in rows]
        notes = {
            "media": "Exact ИНН подтверждает, что компания опубликована как учредитель СМИ; это не доказывает текущее владение или контроль.",
            "communications": "Запись подтверждает опубликованную лицензию связи и её состояние на дату snapshot.",
            "broadcast": "Запись подтверждает опубликованную лицензию вещания и её состояние на дату snapshot.",
            "information_distributors": "Запись подтверждает включение в реестр организаторов распространения информации.",
            "hosting": "Запись подтверждает включение в реестр провайдеров хостинга.",
        }
        return build_check_result(checked=True, applicable=True, result="found" if rows else "not_found", data_date=dataset.last_data_date, dataset_code=code, source=SOURCE_CODE, reason=None, matching_method="inn_exact", records=records, record_count=len(records), interpretation_note=notes[channel], coverage_note="Физлица, ИП и записи без точного ИНН юрлица изолированы и не входят в публичный результат.")
    finally:
        session.close()


def _pd_result(row, cached=True):
    if row.result_status != "success":
        return build_check_result(checked=False, applicable=True, result="unavailable", data_date=row.request_date, dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE, reason=row.error_code or "source_error", message=row.error_message, http_status=row.http_status, cached=cached, matching_method="inn_exact", records=[], record_count=None)
    records = list(row.public_records or [])
    return build_check_result(checked=True, applicable=True, result="found" if row.is_found else "not_found", data_date=row.request_date, dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE, reason=None, http_status=row.http_status, cached=cached, matching_method="inn_exact", records=records, record_count=len(records), interpretation_note="Наличие означает публикацию уведомления оператора ПДн, а не подтверждение соответствия требованиям закона.", coverage_note="Отсутствие не доказывает нарушение или отсутствие обработки: возможны исключения и задержка публикации.")


def get_cached_pd_operator_check(inn, request_date=None):
    inn, request_date = _valid_company_inn(inn), request_date or date.today()
    if not inn:
        return build_check_result(checked=True, applicable=False, result="not_applicable", data_date=None, dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE, reason="company_layer_requires_legal_entity_inn10", matching_method=None, records=[], record_count=0)
    session = get_session()
    try:
        row = session.scalar(select(RoskomnadzorPdOperatorCheck).where(RoskomnadzorPdOperatorCheck.inn == inn, RoskomnadzorPdOperatorCheck.request_date == request_date))
        if row is None:
            return build_check_result(checked=False, applicable=True, result="unavailable", data_date=None, dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE, reason="not_checked", request_date=request_date, cached=False, matching_method="inn_exact", records=[], record_count=None)
        return _pd_result(row)
    finally:
        session.close()


def refresh_pd_operator_check(inn, request_date=None, provider=None, *, force_refresh=False):
    inn, request_date = _valid_company_inn(inn), request_date or date.today()
    if not inn:
        raise ValueError("Реестр операторов ПДн в company-продукте проверяется только для 10-значного ИНН")
    ensure_roskomnadzor_datasets()
    if provider is None and not force_refresh:
        cached = get_cached_pd_operator_check(inn, request_date)
        if cached["result"] in {"found", "not_found"}: return cached
    provider = provider or RoskomnadzorPdOperatorProvider()
    try:
        response = provider.check_inn(inn)
        row = save_pd_operator_check(inn=inn, request_date=request_date, result_status="success", is_found=response["found"], record_count=response["total"], public_records=response["records"], http_status=response["http_status"], error_code=None, error_message=None)
    except RoskomnadzorProviderError as error:
        row = save_pd_operator_check(inn=inn, request_date=request_date, result_status="error", is_found=None, record_count=None, public_records=None, http_status=error.http_status, error_code=error.kind, error_message=error.message)
    return _pd_result(row, cached=False)


def get_roskomnadzor_checks(inn):
    """Batch all persisted company channels in one read-only DB session."""

    clean_inn = _valid_company_inn(inn)
    if not clean_inn:
        return {
            **{
                channel: build_check_result(
                    checked=True, applicable=False, result="not_applicable",
                    data_date=None, dataset_code=DATASETS[channel], source=SOURCE_CODE,
                    reason="company_layer_requires_legal_entity_inn10",
                    matching_method=None, records=[], record_count=0,
                )
                for channel in PUBLIC_CHANNELS
            },
            "pd_operators": build_check_result(
                checked=True, applicable=False, result="not_applicable",
                data_date=None, dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE,
                reason="company_layer_requires_legal_entity_inn10",
                matching_method=None, records=[], record_count=0,
            ),
        }

    codes = [DATASETS[channel] for channel in PUBLIC_CHANNELS]
    session = get_session()
    try:
        datasets = session.scalars(select(DataSet).where(DataSet.code.in_(codes))).all()
        by_code = {item.code: item for item in datasets}
        ids = [item.id for item in datasets]
        snapshot_counts = dict(session.execute(
            select(RoskomnadzorCompanyFact.dataset_id, func.count())
            .where(RoskomnadzorCompanyFact.dataset_id.in_(ids))
            .group_by(RoskomnadzorCompanyFact.dataset_id)
        ).all()) if ids else {}
        matched = session.scalars(
            select(RoskomnadzorCompanyFact)
            .where(
                RoskomnadzorCompanyFact.dataset_id.in_(ids),
                RoskomnadzorCompanyFact.inn == clean_inn,
            )
            .order_by(RoskomnadzorCompanyFact.external_number)
        ).all() if ids else []
        matched_by_dataset = defaultdict(list)
        for row in matched:
            matched_by_dataset[row.dataset_id].append(row)

        notes = {
            "media": "Exact ИНН подтверждает публикацию компании как учредителя СМИ; это не доказывает текущее владение или контроль.",
            "communications": "Запись подтверждает опубликованную лицензию связи и её состояние на дату snapshot.",
            "broadcast": "Запись подтверждает опубликованную лицензию вещания и её состояние на дату snapshot.",
            "information_distributors": "Запись подтверждает включение в реестр организаторов распространения информации.",
            "hosting": "Запись подтверждает включение в реестр провайдеров хостинга.",
        }
        output = {}
        for channel in PUBLIC_CHANNELS:
            code = DATASETS[channel]
            dataset = by_code.get(code)
            if dataset is None or dataset.last_data_date is None:
                output[channel] = build_check_result(
                    checked=False, applicable=True, result="unavailable",
                    data_date=None, dataset_code=code, source=SOURCE_CODE,
                    reason="dataset_not_loaded", matching_method="inn_exact",
                    records=[], record_count=None,
                )
                continue
            if not snapshot_counts.get(dataset.id):
                output[channel] = build_check_result(
                    checked=False, applicable=True, result="unavailable",
                    data_date=dataset.last_data_date, dataset_code=code, source=SOURCE_CODE,
                    reason="dataset_snapshot_missing", matching_method="inn_exact",
                    records=[], record_count=None,
                )
                continue
            rows = matched_by_dataset.get(dataset.id, [])
            records = [{
                "external_number": row.external_number, "name": row.name,
                "ogrn": row.ogrn, "status": row.status, "issued_at": row.issued_at,
                "valid_until": row.valid_until, "details": dict(row.public_details or {}),
            } for row in rows]
            output[channel] = build_check_result(
                checked=True, applicable=True, result="found" if rows else "not_found",
                data_date=dataset.last_data_date, dataset_code=code, source=SOURCE_CODE,
                reason=None, matching_method="inn_exact", records=records,
                record_count=len(records), interpretation_note=notes[channel],
                coverage_note="Физлица, ИП и записи без точного ИНН юрлица изолированы и не входят в публичный результат.",
            )

        pd_row = session.scalar(select(RoskomnadzorPdOperatorCheck).where(
            RoskomnadzorPdOperatorCheck.inn == clean_inn,
            RoskomnadzorPdOperatorCheck.request_date == date.today(),
        ))
        output["pd_operators"] = _pd_result(pd_row) if pd_row else build_check_result(
            checked=False, applicable=True, result="unavailable", data_date=None,
            dataset_code=DATASETS["pd_operators"], source=SOURCE_CODE,
            reason="not_checked", request_date=date.today(), cached=False,
            matching_method="inn_exact", records=[], record_count=None,
        )
        return output
    finally:
        session.close()
