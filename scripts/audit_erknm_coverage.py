import argparse
from collections.abc import Iterable

from sqlalchemy import func, or_, select
from sqlalchemy.orm import aliased

from app.database.postgres import get_session
from app.models.company import Company
from app.models.erknm import ErknmInspection
from app.models.source import DataSet


DATASET_CODE = "erknm_inspections"


def percentage(part: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return round(part * 100.0 / total, 2)


def format_number(value: int) -> str:
    return f"{value:,}".replace(",", " ")


def _dataset_id(session) -> int:
    dataset_id = session.execute(
        select(DataSet.id).where(DataSet.code == DATASET_CODE)
    ).scalar_one_or_none()

    if dataset_id is None:
        raise RuntimeError(
            f"Dataset {DATASET_CODE!r} не найден. "
            "Сначала запусти scripts.init_sources."
        )

    return dataset_id


def _scalar_count(session, filters: Iterable) -> int:
    return session.execute(
        select(func.count())
        .select_from(ErknmInspection)
        .where(*filters)
    ).scalar_one()


def _top_values(session, column, filters: list, limit: int = 15):
    return session.execute(
        select(column, func.count())
        .select_from(ErknmInspection)
        .where(*filters)
        .group_by(column)
        .order_by(func.count().desc(), column)
        .limit(limit)
    ).all()


def collect_erknm_coverage(
    *,
    year: int,
    month: int,
) -> dict:
    session = get_session()

    try:
        dataset_id = _dataset_id(session)
        base = [
            ErknmInspection.dataset_id == dataset_id,
            ErknmInspection.period_year == year,
            ErknmInspection.period_month == month,
        ]

        total = _scalar_count(session, base)
        with_inn = _scalar_count(
            session,
            [*base, ErknmInspection.subject_inn.is_not(None)],
        )
        with_ogrn = _scalar_count(
            session,
            [*base, ErknmInspection.subject_ogrn.is_not(None)],
        )
        without_identifiers = _scalar_count(
            session,
            [
                *base,
                ErknmInspection.subject_inn.is_(None),
                ErknmInspection.subject_ogrn.is_(None),
            ],
        )

        unique_inn = session.execute(
            select(func.count(func.distinct(ErknmInspection.subject_inn)))
            .where(
                *base,
                ErknmInspection.subject_inn.is_not(None),
            )
        ).scalar_one()

        unique_ogrn = session.execute(
            select(func.count(func.distinct(ErknmInspection.subject_ogrn)))
            .where(
                *base,
                ErknmInspection.subject_ogrn.is_not(None),
            )
        ).scalar_one()

        matched_by_inn = session.execute(
            select(func.count())
            .select_from(ErknmInspection)
            .join(Company, Company.inn == ErknmInspection.subject_inn)
            .where(*base)
        ).scalar_one()

        matched_unique_inn = session.execute(
            select(func.count(func.distinct(ErknmInspection.subject_inn)))
            .select_from(ErknmInspection)
            .join(Company, Company.inn == ErknmInspection.subject_inn)
            .where(*base)
        ).scalar_one()

        matched_by_ogrn = session.execute(
            select(func.count())
            .select_from(ErknmInspection)
            .join(Company, Company.ogrn == ErknmInspection.subject_ogrn)
            .where(*base)
        ).scalar_one()

        matched_unique_ogrn = session.execute(
            select(func.count(func.distinct(ErknmInspection.subject_ogrn)))
            .select_from(ErknmInspection)
            .join(Company, Company.ogrn == ErknmInspection.subject_ogrn)
            .where(*base)
        ).scalar_one()

        match_condition = or_(
            Company.inn == ErknmInspection.subject_inn,
            Company.ogrn == ErknmInspection.subject_ogrn,
        )

        matched_any = session.execute(
            select(func.count(func.distinct(ErknmInspection.id)))
            .select_from(ErknmInspection)
            .join(Company, match_condition)
            .where(*base)
        ).scalar_one()

        matched_companies = session.execute(
            select(func.count(func.distinct(Company.id)))
            .select_from(ErknmInspection)
            .join(Company, match_condition)
            .where(*base)
        ).scalar_one()

        c_inn = aliased(Company)
        c_ogrn = aliased(Company)
        identifier_conflicts = session.execute(
            select(func.count())
            .select_from(ErknmInspection)
            .join(c_inn, c_inn.inn == ErknmInspection.subject_inn)
            .join(c_ogrn, c_ogrn.ogrn == ErknmInspection.subject_ogrn)
            .where(
                *base,
                c_inn.id != c_ogrn.id,
            )
        ).scalar_one()

        with_result = _scalar_count(
            session,
            [*base, ErknmInspection.result_text.is_not(None)],
        )
        with_warning = _scalar_count(
            session,
            [*base, ErknmInspection.warning_caption.is_not(None)],
        )
        with_risk = _scalar_count(
            session,
            [*base, ErknmInspection.risk_category.is_not(None)],
        )

        unmatched_with_identifiers = max(
            total - matched_any - without_identifiers,
            0,
        )

        unmatched_samples = session.execute(
            select(
                ErknmInspection.subject_inn,
                ErknmInspection.subject_ogrn,
                ErknmInspection.subject_name,
                ErknmInspection.erpid,
            )
            .select_from(ErknmInspection)
            .outerjoin(Company, match_condition)
            .where(
                *base,
                Company.id.is_(None),
                or_(
                    ErknmInspection.subject_inn.is_not(None),
                    ErknmInspection.subject_ogrn.is_not(None),
                ),
            )
            .order_by(ErknmInspection.id)
            .limit(20)
        ).all()

        return {
            "dataset_id": dataset_id,
            "year": year,
            "month": month,
            "total": total,
            "with_inn": with_inn,
            "with_ogrn": with_ogrn,
            "without_identifiers": without_identifiers,
            "unique_inn": unique_inn,
            "unique_ogrn": unique_ogrn,
            "matched_by_inn": matched_by_inn,
            "matched_unique_inn": matched_unique_inn,
            "matched_by_ogrn": matched_by_ogrn,
            "matched_unique_ogrn": matched_unique_ogrn,
            "matched_any": matched_any,
            "matched_companies": matched_companies,
            "unmatched_with_identifiers": unmatched_with_identifiers,
            "identifier_conflicts": identifier_conflicts,
            "with_result": with_result,
            "with_warning": with_warning,
            "with_risk": with_risk,
            "subject_types": _top_values(
                session, ErknmInspection.subject_type, base
            ),
            "statuses": _top_values(
                session, ErknmInspection.status, base
            ),
            "kinds": _top_values(
                session, ErknmInspection.kind_knm, base
            ),
            "risk_categories": _top_values(
                session, ErknmInspection.risk_category, base
            ),
            "unmatched_samples": unmatched_samples,
        }
    finally:
        session.close()


def _print_breakdown(title: str, rows, total: int) -> None:
    print()
    print(title)
    print("-" * len(title))
    for value, count in rows:
        label = value if value not in (None, "") else "<пусто>"
        print(
            f"{format_number(count):>10}  "
            f"{percentage(count, total):>6.2f}%  {label}"
        )


def print_report(report: dict) -> None:
    total = report["total"]

    print("=" * 78)
    print("ЕРКНМ — COVERAGE AUDIT")
    print("=" * 78)
    print(
        "Период:",
        f"{report['year']}-{report['month']:02d}",
    )
    print("Dataset ID:", report["dataset_id"])
    print()

    def metric(label: str, value: int, denominator: int = total):
        print(
            f"{label}: {format_number(value)} "
            f"({percentage(value, denominator):.2f}%)"
            if denominator
            else f"{label}: {format_number(value)}"
        )

    print("Всего мероприятий:", format_number(total))
    metric("С ИНН субъекта", report["with_inn"])
    metric("С ОГРН/ОГРНИП субъекта", report["with_ogrn"])
    metric("Без ИНН и ОГРН", report["without_identifiers"])
    print()
    print("Уникальных ИНН:", format_number(report["unique_inn"]))
    print("Уникальных ОГРН/ОГРНИП:", format_number(report["unique_ogrn"]))
    print()
    metric("Мероприятий, совпавших по ИНН", report["matched_by_inn"])
    print(
        "Уникальных ИНН, совпавших с Master Registry:",
        format_number(report["matched_unique_inn"]),
        f"({percentage(report['matched_unique_inn'], report['unique_inn']):.2f}%)",
    )
    metric("Мероприятий, совпавших по ОГРН", report["matched_by_ogrn"])
    print(
        "Уникальных ОГРН, совпавших с Master Registry:",
        format_number(report["matched_unique_ogrn"]),
        f"({percentage(report['matched_unique_ogrn'], report['unique_ogrn']):.2f}%)",
    )
    print()
    metric("Мероприятий, связанных с Master Registry", report["matched_any"])
    print(
        "Уникальных компаний Master Registry с мероприятиями:",
        format_number(report["matched_companies"]),
    )
    metric(
        "Есть ИНН/ОГРН, но компания не найдена",
        report["unmatched_with_identifiers"],
    )
    print(
        "Конфликтов ИНН и ОГРН между разными master-компаниями:",
        format_number(report["identifier_conflicts"]),
    )
    print()
    metric("Есть RESULT", report["with_result"])
    metric("Есть WARNING_INFO", report["with_warning"])
    metric("Есть категория риска", report["with_risk"])

    _print_breakdown("Типы субъектов", report["subject_types"], total)
    _print_breakdown("Статусы", report["statuses"], total)
    _print_breakdown("Виды КНМ", report["kinds"], total)
    _print_breakdown("Категории риска", report["risk_categories"], total)

    print()
    print("Примеры записей с идентификатором, но без master-match (до 20)")
    print("-" * 66)
    for inn, ogrn, name, erpid in report["unmatched_samples"]:
        print(
            f"ERPID={erpid} | ИНН={inn or '-'} | "
            f"ОГРН={ogrn or '-'} | {name or '-'}"
        )

    print()
    print("ВАЖНО:")
    print(
        "Факт контрольного/профилактического мероприятия сам по себе "
        "не является негативным сигналом."
    )
    print(
        "Автоматическое сопоставление выполняется только по ИНН/ОГРН; "
        "название субъекта не используется как ключ."
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Проверяет покрытие импортированных мероприятий ФГИС ЕРКНМ "
            "относительно Master Registry"
        )
    )
    parser.add_argument("--year", type=int, required=True)
    parser.add_argument("--month", type=int, required=True)
    args = parser.parse_args()

    if args.month < 1 or args.month > 12:
        raise ValueError("month должен быть от 1 до 12")

    report = collect_erknm_coverage(
        year=args.year,
        month=args.month,
    )
    print_report(report)


if __name__ == "__main__":
    main()
