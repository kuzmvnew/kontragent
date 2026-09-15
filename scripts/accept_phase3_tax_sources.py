from sqlalchemy import func, select

from app.aggregators.company_product_aggregator import get_company_for_web
from app.database.postgres import get_session
from app.models.company import Company
from app.models.source import DataSet
from app.models.tax_debt import CompanyTaxDebtItem, CompanyTaxDebtSnapshot
from app.models.tax_offence import CompanyTaxOffence
from app.models.tax_payment import CompanyTaxPaymentSnapshot
from app.models.revenue_expense import CompanyRevenueExpenseSnapshot
from app.services.tax_debt_service import get_tax_debt_check_for_company
from app.services.tax_offence_service import get_tax_offence_check_for_company


def dataset(session, code):
    row = session.execute(select(DataSet).where(DataSet.code == code)).scalar_one_or_none()
    if row is None or row.last_data_date is None:
        raise RuntimeError(f"{code}: dataset not loaded")
    return row


def count_current(session, model, ds):
    return session.execute(
        select(func.count()).select_from(model).where(
            model.dataset_id == ds.id,
            model.data_date == ds.last_data_date,
        )
    ).scalar_one()


def found_company(session, model, ds, order_column):
    row = session.execute(
        select(Company.id, Company.inn, Company.name)
        .join(model, model.company_id == Company.id)
        .where(model.dataset_id == ds.id, model.data_date == ds.last_data_date)
        .order_by(order_column.desc())
        .limit(1)
    ).first()
    if row is None:
        raise RuntimeError(f"{ds.code}: no real matched company")
    return row


def assert_product(inn, field, source_code):
    company = get_company_for_web(inn)
    check = company.get(field) if company else None
    if not isinstance(check, dict) or check.get("result") != "found":
        raise RuntimeError(f"{source_code}: aggregator smoke failed for {inn}")
    if source_code not in (company.get("sources_used") or []):
        raise RuntimeError(f"{source_code}: missing from sources_used")


def main():
    session = get_session()
    try:
        debt = dataset(session, "fns_tax_debt")
        offence = dataset(session, "fns_tax_offence")
        paid = dataset(session, "fns_tax_paid")
        revexp = dataset(session, "fns_revenue_expenses")

        debt_count = count_current(session, CompanyTaxDebtSnapshot, debt)
        offence_count = count_current(session, CompanyTaxOffence, offence)
        paid_count = count_current(session, CompanyTaxPaymentSnapshot, paid)
        revexp_count = count_current(session, CompanyRevenueExpenseSnapshot, revexp)

        debt_items = session.execute(
            select(func.count()).select_from(CompanyTaxDebtItem)
            .join(CompanyTaxDebtSnapshot, CompanyTaxDebtItem.snapshot_id == CompanyTaxDebtSnapshot.id)
            .where(
                CompanyTaxDebtSnapshot.dataset_id == debt.id,
                CompanyTaxDebtSnapshot.data_date == debt.last_data_date,
            )
        ).scalar_one()

        debt_company = found_company(session, CompanyTaxDebtSnapshot, debt, CompanyTaxDebtSnapshot.total_debt)
        offence_company = found_company(session, CompanyTaxOffence, offence, CompanyTaxOffence.fine_amount)
        ip = session.execute(
            select(Company.id, Company.inn, Company.name)
            .where(Company.entity_type == "individual_entrepreneur")
            .order_by(Company.id)
            .limit(1)
        ).first()
        if ip is None:
            raise RuntimeError("Master Registry: IP not found")
    finally:
        session.close()

    if min(debt_count, offence_count, paid_count, revexp_count) <= 0:
        raise RuntimeError("One of Phase 3 tax datasets has an empty current slice")

    debt_check = get_tax_debt_check_for_company(debt_company.id, include_items=True)
    offence_check = get_tax_offence_check_for_company(offence_company.id)
    ip_debt = get_tax_debt_check_for_company(ip.id, include_items=False)
    ip_offence = get_tax_offence_check_for_company(ip.id)

    if debt_check.get("result") != "found":
        raise RuntimeError("Tax debt real smoke did not return found")
    if offence_check.get("result") != "found":
        raise RuntimeError("Tax offence real smoke did not return found")
    if ip_debt.get("result") != "not_applicable" or ip_offence.get("result") != "not_applicable":
        raise RuntimeError("IP applicability smoke failed")

    assert_product(debt_company.inn, "tax_debt_check", "fns_tax_debt")
    assert_product(offence_company.inn, "tax_offence_check", "fns_tax_offence")

    print("===== PHASE 3 TAX DATASETS =====")
    print(f"DEBT: {debt.last_data_date} | snapshots={debt_count:,} | items={debt_items:,}")
    print(f"OFFENCE: {offence.last_data_date} | documents={offence_count:,}")
    print(f"PAYTAX: {paid.last_data_date} | snapshots={paid_count:,}")
    print(f"REVEXP: {revexp.last_data_date} | snapshots={revexp_count:,}")
    print()
    print("===== REAL PRODUCT SMOKE =====")
    print(f"DEBT PASS | INN={debt_company.inn} | company={debt_company.name} | total={debt_check['total_debt']}")
    print(f"OFFENCE PASS | INN={offence_company.inn} | company={offence_company.name} | docs={offence_check['document_count']} | fine={offence_check['fine_amount']}")
    print(f"IP PASS | INN={ip.inn} | debt={ip_debt['result']} | offence={ip_offence['result']}")
    print()
    print("PHASE 3 TAX ACCEPTANCE: PASS")


if __name__ == "__main__":
    main()
