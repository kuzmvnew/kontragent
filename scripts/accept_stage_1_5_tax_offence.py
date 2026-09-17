import argparse
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import func, select

from app.aggregators.company_product_aggregator import get_company_for_web
from app.database.postgres import get_session
from app.ingestion.fns_tax_offence import (
    SOURCE_FILE_BASE_URL,
    SOURCE_PAGE_URL,
    iter_xml_records,
)
from app.models.company import Company
from app.models.source import DataSet, IngestionRun
from app.models.tax_offence import CompanyTaxOffence
from app.services.ingestion_service import calculate_file_checksum
from app.services.tax_offence_service import get_tax_offence_check_for_company


DATASET_CODE = "fns_tax_offence"
DEFAULT_SOURCE_DIR = Path("data/fns/taxoffence")


def parse_args():
    parser = argparse.ArgumentParser(
        description="Stage 1.5 A2 acceptance audit for FNS Tax Offences"
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="Official FNS taxoffence ZIP; defaults to latest imported filename",
    )
    return parser.parse_args()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def product_check(inn, expected_result):
    company = get_company_for_web(inn)
    require(company is not None, f"Company product lookup failed for {inn}")
    check = company.get("tax_offence_check")
    require(isinstance(check, dict), f"Tax Offences product block missing for {inn}")
    require(
        check.get("result") == expected_result,
        f"Tax Offences product state for {inn}: {check.get('result')!r}",
    )
    if expected_result in {"found", "not_found"}:
        require(
            DATASET_CODE in (company.get("sources_used") or []),
            f"Tax Offences source missing from sources_used for {inn}",
        )
    return check


def main():
    args = parse_args()
    session = get_session()
    try:
        dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
        require(dataset is not None, "Tax Offences dataset is not registered")
        require(dataset.last_data_date is not None, "Tax Offences dataset is not loaded")

        run = session.scalars(
            select(IngestionRun)
            .where(IngestionRun.dataset_id == dataset.id)
            .order_by(IngestionRun.id.desc())
            .limit(1)
        ).first()
        require(run is not None and run.status == "success", "Latest ingestion is not successful")
        require(run.data_date == dataset.last_data_date, "Ingestion and dataset dates differ")
        require(run.errors_count == 0, "Latest ingestion recorded errors")

        source_file = args.source_file or DEFAULT_SOURCE_DIR / str(run.source_file_name)
        require(source_file.is_file(), f"Official source file is missing: {source_file}")
        checksum = calculate_file_checksum(source_file)
        require(checksum == run.file_checksum, "Source file checksum differs from ingestion run")

        with ZipFile(source_file) as archive:
            require(archive.testzip() is None, "Official source ZIP failed CRC validation")
            xml_files = [
                info for info in archive.infolist()
                if not info.is_dir() and info.filename.lower().endswith(".xml")
            ]
            require(xml_files, "Official source ZIP contains no XML files")
            with archive.open(xml_files[0]) as xml_file:
                first_record = next(record for record in iter_xml_records(xml_file) if record)
        require(first_record["data_date"] == dataset.last_data_date, "XML and dataset dates differ")

        current_filter = (
            CompanyTaxOffence.dataset_id == dataset.id,
            CompanyTaxOffence.data_date == dataset.last_data_date,
        )
        documents = session.scalar(
            select(func.count()).select_from(CompanyTaxOffence).where(*current_filter)
        )
        companies = session.scalar(
            select(func.count(func.distinct(CompanyTaxOffence.company_id))).where(
                *current_filter
            )
        )
        stored_fine_total = session.scalar(
            select(func.sum(CompanyTaxOffence.fine_amount)).where(*current_filter)
        )
        master_count = session.scalar(select(func.count()).select_from(Company))
        legal_count = session.scalar(
            select(func.count()).select_from(Company).where(Company.entity_type == "legal")
        )
        history = session.execute(
            select(
                func.count(func.distinct(CompanyTaxOffence.data_date)),
                func.min(CompanyTaxOffence.data_date),
                func.max(CompanyTaxOffence.data_date),
            ).where(CompanyTaxOffence.dataset_id == dataset.id)
        ).one()

        found_company = session.execute(
            select(
                Company.id,
                Company.inn,
                Company.name,
                func.count(CompanyTaxOffence.id).label("document_count"),
                func.sum(CompanyTaxOffence.fine_amount).label("fine_amount"),
            )
            .join(CompanyTaxOffence, CompanyTaxOffence.company_id == Company.id)
            .where(*current_filter)
            .group_by(Company.id)
            .order_by(func.sum(CompanyTaxOffence.fine_amount).desc())
            .limit(1)
        ).one()
        not_found_company = session.execute(
            select(Company.id, Company.inn, Company.name)
            .where(
                Company.entity_type == "legal",
                ~Company.id.in_(select(CompanyTaxOffence.company_id).where(*current_filter)),
            )
            .order_by(Company.id)
            .limit(1)
        ).one()
        ip_company = session.execute(
            select(Company.id, Company.inn, Company.name)
            .where(Company.entity_type == "individual_entrepreneur")
            .order_by(Company.id)
            .limit(1)
        ).one()
    finally:
        session.close()

    found = get_tax_offence_check_for_company(found_company.id)
    not_found = get_tax_offence_check_for_company(not_found_company.id)
    not_applicable = get_tax_offence_check_for_company(ip_company.id)
    require(found["result"] == "found" and found["fine_amount"] > 0, "Real found failed")
    require(found["fine_amount"] == found_company.fine_amount, "Found fine total differs")
    require(found["document_count"] == found_company.document_count, "Document count differs")
    require(not_found["result"] == "not_found", "Real dated not_found failed")
    require(not_found["data_date"] == dataset.last_data_date, "not_found has no coverage date")
    require(not_applicable["result"] == "not_applicable", "IP applicability failed")
    require(run.rows_inserted == documents, "Ingestion count differs from PostgreSQL")
    require((run.details or {}).get("saved") == documents, "Saved count differs")
    require(documents == companies, "Accepted snapshot unexpectedly has duplicate companies")

    product_check(found_company.inn, "found")
    product_check(not_found_company.inn, "not_found")
    product_check(ip_company.inn, "not_applicable")

    print("STAGE 1.5 A2 FNS TAX OFFENCES: DATABASE/PRODUCT PASS")
    print(
        {
            "source_file": source_file.name,
            "source_page_url": SOURCE_PAGE_URL,
            "source_file_url": SOURCE_FILE_BASE_URL + source_file.name,
            "sha256": checksum,
            "xml_files": len(xml_files),
            "data_date": dataset.last_data_date.isoformat(),
            "document_date": first_record["document_date"].isoformat(),
            "rows_read": run.rows_read,
            "valid": (run.details or {}).get("valid"),
            "invalid": (run.details or {}).get("invalid"),
            "matched_documents": documents,
            "matched_companies": companies,
            "unmatched": (run.details or {}).get("unmatched"),
            "source_fine_total": (run.details or {}).get("fine_total"),
            "stored_fine_total": str(stored_fine_total),
            "master_count": master_count,
            "legal_count": legal_count,
            "master_coverage_percent": round(companies * 100 / master_count, 4),
            "legal_coverage_percent": round(companies * 100 / legal_count, 4),
            "history_dates": history[0],
            "history_min": history[1].isoformat(),
            "history_max": history[2].isoformat(),
            "found": {
                "inn": found_company.inn,
                "name": found_company.name,
                "documents": found["document_count"],
                "fine_amount": str(found["fine_amount"]),
                "document_date": found["document_date"].isoformat(),
            },
            "not_found": {
                "inn": not_found_company.inn,
                "name": not_found_company.name,
                "data_date": not_found["data_date"].isoformat(),
            },
            "not_applicable": {"inn": ip_company.inn},
        }
    )


if __name__ == "__main__":
    main()
