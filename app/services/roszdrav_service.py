from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.ingestion.roszdrav import save_medical_device_check, save_unified_license_check
from app.models.roszdrav import (
    RoszdravClinicalOrganizationEntry,
    RoszdravLicenseEntry,
    RoszdravMedicalDeviceCheck,
    RoszdravUnifiedLicenseCheck,
)
from app.models.source import DataSet
from app.providers.roszdrav_provider import (
    RoszdravMedicalDeviceProvider,
    RoszdravProviderError,
    RoszdravUnifiedLicenseProvider,
    normalize_inn,
)
from app.services.check_result import build_check_result
from app.services.roszdrav_registry_service import (
    CLINICAL_ORG_DATASET,
    LICENSE_DATASETS,
    MEDICAL_DEVICE_DATASET,
    SOURCE_CODE,
    UNIFIED_LICENSE_DATASET,
    ensure_roszdrav_datasets,
)


def _invalid(dataset_code, reason="invalid_inn"):
    return build_check_result(
        checked=False, applicable=True, result="unavailable", data_date=None,
        dataset_code=dataset_code, source=SOURCE_CODE, reason=reason,
        matching_method="inn_exact",
    )


def _license_row(row):
    return {
        "category": row.category, "license_number": row.license_number,
        "licensee_name": row.licensee_name, "ogrn": row.ogrn,
        "authority_name": row.authority_name, "activity_type": row.activity_type,
        "address": row.address, "work_places": list(row.work_places or []),
        "decision_date": row.decision_date, "start_date": row.start_date,
        "end_date": row.end_date, "termination_info": row.termination_info,
        "termination_date": row.termination_date,
        "suspension_info": row.suspension_info,
        "cancellation_info": row.cancellation_info,
    }


def get_roszdrav_bulk_license_check_for_inn(inn: str) -> dict:
    inn = normalize_inn(inn)
    if not inn:
        return _invalid("roszdrav_bulk_licenses")
    session = get_session()
    try:
        datasets = session.execute(
            select(DataSet).where(DataSet.code.in_(list(LICENSE_DATASETS.values())))
        ).scalars().all()
        by_code = {item.code: item for item in datasets}
        missing = [code for code in LICENSE_DATASETS.values() if code not in by_code or by_code[code].last_data_date is None]
        if missing:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code="roszdrav_bulk_licenses", source=SOURCE_CODE,
                reason="dataset_not_loaded", missing_datasets=missing, records=[],
                record_count=None, dataset_dates={}, matching_method="inn_exact",
                interpretation_note="Лицензия подтверждает только опубликованный вид деятельности и статус на дату snapshot.",
                coverage_note="Частично загруженные категории не трактуются как отсутствие лицензии.",
            )
        dataset_dates = {code: by_code[code].last_data_date for code in LICENSE_DATASETS.values()}
        if len(set(dataset_dates.values())) != 1:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code="roszdrav_bulk_licenses", source=SOURCE_CODE,
                reason="dataset_date_mismatch", records=[], record_count=None,
                dataset_dates=dataset_dates, matching_method="inn_exact",
                interpretation_note="Три категории лицензий должны относиться к одной дате snapshot.",
                coverage_note="Смешанные даты не трактуются как полный срез и не дают not_found.",
            )
        for code, dataset in by_code.items():
            count = session.execute(select(func.count()).select_from(RoszdravLicenseEntry).where(RoszdravLicenseEntry.dataset_id == dataset.id)).scalar_one()
            if count == 0:
                return build_check_result(
                    checked=False, applicable=True, result="unavailable", data_date=dataset.last_data_date,
                    dataset_code="roszdrav_bulk_licenses", source=SOURCE_CODE,
                    reason="dataset_snapshot_missing", missing_datasets=[code], records=[],
                    record_count=None, dataset_dates=dataset_dates, matching_method="inn_exact",
                    interpretation_note="Пустой snapshot не считается успешной проверкой.",
                    coverage_note="Ошибка или неполный snapshot не равны not_found.",
                )
        rows = session.execute(
            select(RoszdravLicenseEntry).where(
                RoszdravLicenseEntry.dataset_id.in_([item.id for item in datasets]),
                RoszdravLicenseEntry.inn == inn,
            ).order_by(RoszdravLicenseEntry.category, RoszdravLicenseEntry.license_number)
        ).scalars().all()
        return build_check_result(
            checked=True, applicable=True, result="found" if rows else "not_found",
            data_date=min(dataset_dates.values()), dataset_code="roszdrav_bulk_licenses",
            source=SOURCE_CODE, reason=None, records=[_license_row(row) for row in rows],
            record_count=len(rows), dataset_dates=dataset_dates, matching_method="inn_exact",
            interpretation_note="Найдены только лицензии трёх открытых категорий Росздравнадзора; наличие записи не является общей оценкой контрагента.",
            coverage_note="Отсутствие означает только отсутствие exact-INN в трёх успешно загруженных snapshot и не охватывает все виды лицензий.",
        )
    finally:
        session.close()


def _unified_row(row, cached=True):
    if row.result_status != "success":
        return build_check_result(
            checked=False, applicable=True, result="unavailable", data_date=row.request_date,
            dataset_code=UNIFIED_LICENSE_DATASET, source=SOURCE_CODE,
            reason=row.error_code or "source_error", message=row.error_message,
            records=[], record_count=None, cached=cached, checked_at=row.checked_at,
            http_status=row.http_status, matching_method="inn_exact",
        )
    records = list(row.records or [])
    return build_check_result(
        checked=True, applicable=True, result="found" if row.is_found else "not_found",
        data_date=row.request_date, dataset_code=UNIFIED_LICENSE_DATASET,
        source=SOURCE_CODE, reason=None, records=records, record_count=len(records),
        cached=cached, checked_at=row.checked_at, http_status=row.http_status,
        matching_method="inn_exact",
        interpretation_note="Результат относится к точечному официальному поиску Единого реестра лицензий.",
        coverage_note="Отсутствие записи не определяет, требуется ли компании лицензия. Это датированный cache, а не полная локальная копия реестра.",
    )


def get_cached_roszdrav_unified_license_check(inn: str, request_date=None):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()
    if not inn:
        return _invalid(UNIFIED_LICENSE_DATASET)
    session = get_session()
    try:
        row = session.execute(select(RoszdravUnifiedLicenseCheck).where(
            RoszdravUnifiedLicenseCheck.inn == inn,
            RoszdravUnifiedLicenseCheck.request_date == request_date,
        )).scalar_one_or_none()
        if row is None:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code=UNIFIED_LICENSE_DATASET, source=SOURCE_CODE,
                reason="not_checked", records=[], record_count=None, cached=False,
                request_date=request_date,
                matching_method="inn_exact",
            )
        return _unified_row(row)
    finally:
        session.close()


def refresh_roszdrav_unified_license_check(
    inn: str, request_date=None, provider=None, *, force_refresh=False,
):
    inn = normalize_inn(inn)
    request_date = request_date or date.today()
    if not inn:
        return _invalid(UNIFIED_LICENSE_DATASET)
    ensure_roszdrav_datasets()
    if provider is None and not force_refresh:
        cached = get_cached_roszdrav_unified_license_check(inn, request_date)
        if cached.get("result") in {"found", "not_found"}:
            return cached
    provider = provider or RoszdravUnifiedLicenseProvider()
    try:
        response = provider.check_inn(inn)
        row = save_unified_license_check(
            inn=inn, request_date=request_date, result_status="success",
            is_found=response["found"], records=response["records"],
            http_status=response["http_status"], error_code=None, error_message=None,
            raw_payload=response["raw_payload"],
        )
    except RoszdravProviderError as error:
        row = save_unified_license_check(
            inn=inn, request_date=request_date, result_status="error", is_found=None,
            records=None, http_status=error.http_status, error_code=error.kind,
            error_message=error.message, raw_payload=None,
        )
    return _unified_row(row, cached=False)


def get_roszdrav_clinical_org_check_for_inn(inn: str) -> dict:
    inn = normalize_inn(inn)
    if not inn:
        return _invalid(CLINICAL_ORG_DATASET)
    session = get_session()
    try:
        dataset = session.execute(select(DataSet).where(DataSet.code == CLINICAL_ORG_DATASET)).scalar_one_or_none()
        if dataset is None or dataset.last_data_date is None:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code=CLINICAL_ORG_DATASET, source=SOURCE_CODE,
                reason="dataset_not_loaded", records=[], record_count=None,
                matching_method="inn_exact",
            )
        total = session.execute(select(func.count()).select_from(RoszdravClinicalOrganizationEntry).where(RoszdravClinicalOrganizationEntry.dataset_id == dataset.id)).scalar_one()
        if total == 0:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=dataset.last_data_date,
                dataset_code=CLINICAL_ORG_DATASET, source=SOURCE_CODE,
                reason="dataset_snapshot_missing", records=[], record_count=None,
                matching_method="inn_exact",
            )
        rows = session.execute(select(RoszdravClinicalOrganizationEntry).where(
            RoszdravClinicalOrganizationEntry.dataset_id == dataset.id,
            RoszdravClinicalOrganizationEntry.inn == inn,
        )).scalars().all()
        records = [{"name": row.name, "included_at": row.included_at, "address": row.address, "phone": row.phone, "email": row.email} for row in rows]
        return build_check_result(
            checked=True, applicable=True, result="found" if rows else "not_found",
            data_date=dataset.last_data_date, dataset_code=CLINICAL_ORG_DATASET,
            source=SOURCE_CODE, reason=None, records=records, record_count=len(records),
            matching_method="inn_exact",
            interpretation_note="Наличие записи подтверждает включение в официальный перечень организаций для клинических исследований медицинских изделий.",
            coverage_note="Отсутствие относится только к этому перечню и его дате.",
        )
    finally:
        session.close()


def get_roszdrav_medical_device_company_check() -> dict:
    return build_check_result(
        checked=True, applicable=False, result="not_applicable", data_date=None,
        dataset_code=MEDICAL_DEVICE_DATASET, source=SOURCE_CODE,
        reason="official_registry_has_no_company_exact_identifier",
        matching_method=None,
        interpretation_note="Публичный реестр медизделий поддерживает lookup по номеру РУ, но не отдаёт ИНН/ОГРН производителя.",
        coverage_note="Автоматическая связь с Company по названию или адресу запрещена.",
    )


def _medical_device_row(row, *, cached=True):
    if row.result_status != "success":
        return build_check_result(
            checked=False, applicable=True, result="unavailable",
            data_date=row.request_date, dataset_code=MEDICAL_DEVICE_DATASET,
            source=SOURCE_CODE, reason=row.error_code, message=row.error_message,
            records=[], record_count=None, checked_at=row.checked_at,
            http_status=row.http_status, matching_method="registration_number_exact",
            cached=cached,
        )
    records = list(row.records or [])
    return build_check_result(
        checked=True, applicable=True, result="found" if row.is_found else "not_found",
        data_date=row.request_date, dataset_code=MEDICAL_DEVICE_DATASET,
        source=SOURCE_CODE, reason=None, records=records, record_count=len(records),
        checked_at=row.checked_at, http_status=row.http_status,
        matching_method="registration_number_exact", cached=cached,
    )


def get_cached_roszdrav_medical_device_check(registration_number: str, request_date=None):
    registration_number = str(registration_number or "").strip()
    request_date = request_date or date.today()
    if not registration_number:
        raise ValueError("Номер регистрационного удостоверения обязателен")
    session = get_session()
    try:
        row = session.execute(select(RoszdravMedicalDeviceCheck).where(
            RoszdravMedicalDeviceCheck.registration_number == registration_number,
            RoszdravMedicalDeviceCheck.request_date == request_date,
        )).scalar_one_or_none()
        if row is None:
            return build_check_result(
                checked=False, applicable=True, result="unavailable", data_date=None,
                dataset_code=MEDICAL_DEVICE_DATASET, source=SOURCE_CODE,
                reason="not_checked", records=[], record_count=None, cached=False,
                request_date=request_date, matching_method="registration_number_exact",
            )
        return _medical_device_row(row)
    finally:
        session.close()


def refresh_roszdrav_medical_device_check(
    registration_number: str, request_date=None, provider=None, *, force_refresh=False,
):
    registration_number = str(registration_number or "").strip()
    if not registration_number:
        raise ValueError("Номер регистрационного удостоверения обязателен")
    if len(registration_number) > 160:
        raise ValueError("Номер регистрационного удостоверения слишком длинный")
    request_date = request_date or date.today()
    ensure_roszdrav_datasets()
    if provider is None and not force_refresh:
        cached = get_cached_roszdrav_medical_device_check(
            registration_number, request_date
        )
        if cached.get("result") in {"found", "not_found"}:
            return cached
    provider = provider or RoszdravMedicalDeviceProvider()
    try:
        response = provider.check_registration_number(registration_number)
        row = save_medical_device_check(
            registration_number=registration_number, request_date=request_date,
            result_status="success", is_found=response["found"],
            total_results=response["total"], records=response["records"],
            http_status=response["http_status"], error_code=None, error_message=None,
            raw_payload=response["raw_payload"],
        )
    except RoszdravProviderError as error:
        row = save_medical_device_check(
            registration_number=registration_number, request_date=request_date,
            result_status="error", is_found=None, total_results=None, records=None,
            http_status=error.http_status, error_code=error.kind,
            error_message=error.message, raw_payload=None,
        )
    return _medical_device_row(row, cached=False)
