import argparse
from pathlib import Path
from zipfile import ZipFile

from sqlalchemy import func, select, text

from app.aggregators.company_product_aggregator import get_company_for_web
from app.database.postgres import get_session
from app.ingestion.fns_tax_debt import SOURCE_FILE_BASE_URL, SOURCE_PAGE_URL, iter_xml_records
from app.models.company import Company
from app.models.source import DataSet, IngestionRun
from app.models.tax_debt import CompanyTaxDebtItem, CompanyTaxDebtSnapshot
from app.services.ingestion_service import calculate_file_checksum
from app.services.tax_debt_service import get_tax_debt_check_for_company


DATASET_CODE = "fns_tax_debt"
DEFAULT_SOURCE_DIR = Path("data/fns/debtam")

CONSISTENCY_SQL = text(
    """
    WITH current AS (
        SELECT s.*
        FROM company_tax_debt_snapshots s
        JOIN data_sets d ON d.id = s.dataset_id
        WHERE d.code = :dataset_code
          AND s.data_date = d.last_data_date
    ),
    item_totals AS (
        SELECT
            i.snapshot_id,
            COUNT(*) AS item_count,
            SUM(i.arrears) AS arrears,
            SUM(i.penalties) AS penalties,
            SUM(i.fines) AS fines,
            SUM(i.total) AS total,
            COUNT(*) FILTER (
                WHERE i.total <> i.arrears + i.penalties + i.fines
            ) AS component_mismatches
        FROM company_tax_debt_items i
        JOIN current c ON c.id = i.snapshot_id
        GROUP BY i.snapshot_id
    )
    SELECT
        COUNT(*) AS snapshots,
        COUNT(*) FILTER (
            WHERE c.item_count <> t.item_count
               OR c.total_arrears <> t.arrears
               OR c.total_penalties <> t.penalties
               OR c.total_fines <> t.fines
               OR c.total_debt <> t.total
        ) AS snapshot_mismatches,
        COALESCE(SUM(t.component_mismatches), 0) AS component_mismatches,
        MIN(c.total_debt) AS min_total,
        MAX(c.total_debt) AS max_total,
        SUM(c.total_arrears) AS sum_arrears,
        SUM(c.total_penalties) AS sum_penalties,
        SUM(c.total_fines) AS sum_fines,
        SUM(c.total_debt) AS sum_total
    FROM current c
    JOIN item_totals t ON t.snapshot_id = c.id
    """
)


def parse_args():
    parser = argparse.ArgumentParser(
        description="Stage 1.5 A1 acceptance audit for FNS Tax Debt"
    )
    parser.add_argument(
        "--source-file",
        type=Path,
        help="Official FNS debtam ZIP; defaults to the latest imported filename",
    )
    return parser.parse_args()


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def product_check(inn, expected_result):
    company = get_company_for_web(inn)
    require(company is not None, f"Company product lookup failed for {inn}")
    check = company.get("tax_debt_check")
    require(isinstance(check, dict), f"Tax Debt product block missing for {inn}")
    require(
        check.get("result") == expected_result,
        f"Tax Debt product state for {inn}: {check.get('result')!r}",
    )
    if expected_result in {"found", "not_found"}:
        require(
            DATASET_CODE in (company.get("sources_used") or []),
            f"Tax Debt source missing from sources_used for {inn}",
        )
    return check


def main():
    args = parse_args()
    session = get_session()
    try:
        dataset = session.scalar(select(DataSet).where(DataSet.code == DATASET_CODE))
        require(dataset is not None, "Tax Debt dataset is not registered")
        require(dataset.last_data_date is not None, "Tax Debt dataset is not loaded")

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
            CompanyTaxDebtSnapshot.dataset_id == dataset.id,
            CompanyTaxDebtSnapshot.data_date == dataset.last_data_date,
        )
        snapshots = session.scalar(
            select(func.count()).select_from(CompanyTaxDebtSnapshot).where(*current_filter)
        )
        items = session.scalar(
            select(func.count())
            .select_from(CompanyTaxDebtItem)
            .join(CompanyTaxDebtSnapshot)
            .where(*current_filter)
        )
        master_count = session.scalar(select(func.count()).select_from(Company))
        legal_count = session.scalar(
            select(func.count()).select_from(Company).where(Company.entity_type == "legal")
        )
        history = session.execute(
            select(
                func.count(func.distinct(CompanyTaxDebtSnapshot.data_date)),
                func.min(CompanyTaxDebtSnapshot.data_date),
                func.max(CompanyTaxDebtSnapshot.data_date),
            ).where(CompanyTaxDebtSnapshot.dataset_id == dataset.id)
        ).one()

        found_company = session.execute(
            select(Company.id, Company.inn, Company.name)
            .join(CompanyTaxDebtSnapshot, CompanyTaxDebtSnapshot.company_id == Company.id)
            .where(*current_filter)
            .order_by(CompanyTaxDebtSnapshot.total_debt.desc())
            .limit(1)
        ).one()
        not_found_company = session.execute(
            select(Company.id, Company.inn, Company.name)
            .where(
                Company.entity_type == "legal",
                ~Company.id.in_(
                    select(CompanyTaxDebtSnapshot.company_id).where(*current_filter)
                ),
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

        consistency = session.execute(
            CONSISTENCY_SQL, {"dataset_code": DATASET_CODE}
        ).mappings().one()
    finally:
        session.close()

    found = get_tax_debt_check_for_company(found_company.id, include_items=True)
    not_found = get_tax_debt_check_for_company(not_found_company.id, include_items=False)
    not_applicable = get_tax_debt_check_for_company(ip_company.id, include_items=False)
    require(found["result"] == "found" and found["total_debt"] > 0, "Real found failed")
    require(not_found["result"] == "not_found", "Real dated not_found failed")
    require(not_found["data_date"] == dataset.last_data_date, "not_found has no coverage date")
    require(not_applicable["result"] == "not_applicable", "IP applicability failed")
    require(consistency["snapshots"] == snapshots, "Consistency audit missed snapshots")
    require(consistency["snapshot_mismatches"] == 0, "Snapshot/item totals differ")
    require(consistency["component_mismatches"] == 0, "Item components differ from totals")
    require(run.rows_inserted == snapshots, "Ingestion count differs from PostgreSQL")
    require((run.details or {}).get("items") == items, "Ingestion item count differs from PostgreSQL")

    product_check(found_company.inn, "found")
    product_check(not_found_company.inn, "not_found")
    product_check(ip_company.inn, "not_applicable")

    print("STAGE 1.5 A1 FNS TAX DEBT: DATABASE/PRODUCT PASS")
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
            "matched_snapshots": snapshots,
            "unmatched": (run.details or {}).get("unmatched"),
            "items": items,
            "master_count": master_count,
            "legal_count": legal_count,
            "master_coverage_percent": round(snapshots * 100 / master_count, 4),
            "legal_coverage_percent": round(snapshots * 100 / legal_count, 4),
            "history_dates": history[0],
            "history_min": history[1].isoformat(),
            "history_max": history[2].isoformat(),
            "found": {
                "inn": found_company.inn,
                "name": found_company.name,
                "total": str(found["total_debt"]),
                "arrears": str(found["total_arrears"]),
                "penalties": str(found["total_penalties"]),
                "fines": str(found["total_fines"]),
                "item_count": found["item_count"],
            },
            "not_found": {
                "inn": not_found_company.inn,
                "name": not_found_company.name,
                "data_date": not_found["data_date"].isoformat(),
            },
            "not_applicable": {"inn": ip_company.inn},
            "totals": {key: str(value) for key, value in consistency.items()},
        }
    )


if __name__ == "__main__":
    main()
