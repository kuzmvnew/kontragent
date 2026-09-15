import argparse
from datetime import date

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company
from app.models.disqualified_person import DisqualifiedPersonSnapshot


def parse_data_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Дата должна быть в формате YYYY-MM-DD"
        ) from error


def percentage(part: int, whole: int) -> float:
    if whole == 0:
        return 0.0
    return round(part * 100 / whole, 2)


def resolve_data_date(session, requested_date: date | None) -> date:
    if requested_date is not None:
        return requested_date

    latest = session.execute(
        select(func.max(DisqualifiedPersonSnapshot.data_date))
    ).scalar_one_or_none()

    if latest is None:
        raise RuntimeError(
            "В disqualified_person_snapshots пока нет данных"
        )

    return latest


def build_coverage_report(session, data_date: date) -> dict:
    snapshot_filter = (
        DisqualifiedPersonSnapshot.data_date == data_date
    )
    has_inn_filter = (
        DisqualifiedPersonSnapshot.organization_inn.is_not(None)
    )

    total_records = session.execute(
        select(func.count())
        .select_from(DisqualifiedPersonSnapshot)
        .where(snapshot_filter)
    ).scalar_one()

    records_with_inn = session.execute(
        select(func.count())
        .select_from(DisqualifiedPersonSnapshot)
        .where(snapshot_filter, has_inn_filter)
    ).scalar_one()

    unique_inns = session.execute(
        select(
            func.count(
                func.distinct(
                    DisqualifiedPersonSnapshot.organization_inn
                )
            )
        )
        .select_from(DisqualifiedPersonSnapshot)
        .where(snapshot_filter, has_inn_filter)
    ).scalar_one()

    matched_rows = session.execute(
        select(func.count())
        .select_from(DisqualifiedPersonSnapshot)
        .join(
            Company,
            Company.inn
            == DisqualifiedPersonSnapshot.organization_inn,
        )
        .where(snapshot_filter, has_inn_filter)
    ).scalar_one()

    matched_unique_inns = session.execute(
        select(
            func.count(
                func.distinct(
                    DisqualifiedPersonSnapshot.organization_inn
                )
            )
        )
        .select_from(DisqualifiedPersonSnapshot)
        .join(
            Company,
            Company.inn
            == DisqualifiedPersonSnapshot.organization_inn,
        )
        .where(snapshot_filter, has_inn_filter)
    ).scalar_one()

    active_filter = (
        (DisqualifiedPersonSnapshot.start_date.is_(None))
        | (DisqualifiedPersonSnapshot.start_date <= data_date)
    ) & (
        (DisqualifiedPersonSnapshot.end_date.is_(None))
        | (DisqualifiedPersonSnapshot.end_date >= data_date)
    )

    active_records = session.execute(
        select(func.count())
        .select_from(DisqualifiedPersonSnapshot)
        .where(snapshot_filter, active_filter)
    ).scalar_one()

    active_matched_rows = session.execute(
        select(func.count())
        .select_from(DisqualifiedPersonSnapshot)
        .join(
            Company,
            Company.inn
            == DisqualifiedPersonSnapshot.organization_inn,
        )
        .where(
            snapshot_filter,
            has_inn_filter,
            active_filter,
        )
    ).scalar_one()

    unmatched_inns = (
        session.execute(
            select(DisqualifiedPersonSnapshot.organization_inn)
            .distinct()
            .outerjoin(
                Company,
                Company.inn
                == DisqualifiedPersonSnapshot.organization_inn,
            )
            .where(
                snapshot_filter,
                has_inn_filter,
                Company.id.is_(None),
            )
            .order_by(DisqualifiedPersonSnapshot.organization_inn)
            .limit(20)
        )
        .scalars()
        .all()
    )

    return {
        "data_date": data_date,
        "total_records": total_records,
        "records_with_inn": records_with_inn,
        "records_without_inn": total_records - records_with_inn,
        "unique_inns": unique_inns,
        "matched_rows": matched_rows,
        "matched_unique_inns": matched_unique_inns,
        "unmatched_unique_inns": unique_inns - matched_unique_inns,
        "active_records": active_records,
        "active_matched_rows": active_matched_rows,
        "sample_unmatched_inns": unmatched_inns,
    }


def print_report(report: dict) -> None:
    total_records = report["total_records"]
    records_with_inn = report["records_with_inn"]
    unique_inns = report["unique_inns"]
    matched_unique_inns = report["matched_unique_inns"]

    print("=" * 72)
    print("ФНС — ПОКРЫТИЕ РЕЕСТРА ДИСКВАЛИФИЦИРОВАННЫХ ЛИЦ")
    print("=" * 72)
    print("Дата snapshot:", report["data_date"])
    print()
    print("Всего записей:", f"{total_records:,}")
    print(
        "С ИНН организации:",
        f"{records_with_inn:,}",
        f"({percentage(records_with_inn, total_records)}%)",
    )
    print(
        "Без ИНН организации:",
        f"{report['records_without_inn']:,}",
        f"({percentage(report['records_without_inn'], total_records)}%)",
    )
    print()
    print("Уникальных ИНН организаций:", f"{unique_inns:,}")
    print(
        "Совпали с Master Registry:",
        f"{matched_unique_inns:,}",
        f"({percentage(matched_unique_inns, unique_inns)}%)",
    )
    print(
        "Не совпали с Master Registry:",
        f"{report['unmatched_unique_inns']:,}",
        f"({percentage(report['unmatched_unique_inns'], unique_inns)}%)",
    )
    print()
    print(
        "Строк с ИНН, связанных с Master Registry:",
        f"{report['matched_rows']:,}",
    )
    print(
        "Активных на дату snapshot записей:",
        f"{report['active_records']:,}",
    )
    print(
        "Активных записей, связанных с Master Registry:",
        f"{report['active_matched_rows']:,}",
    )

    if report["sample_unmatched_inns"]:
        print()
        print("Пример ИНН без совпадения (до 20):")
        for inn in report["sample_unmatched_inns"]:
            print(" -", inn)

    print()
    print("ВАЖНО:")
    print(
        "organization_inn — организация, связанная с правонарушением в записи ФНС."
    )
    print(
        "Совпадение по ИНН не означает, что дисквалифицированное лицо является "
        "текущим руководителем компании."
    )


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Проверка покрытия Реестра дисквалифицированных лиц ФНС "
            "относительно Master Registry"
        )
    )
    parser.add_argument(
        "--data-date",
        type=parse_data_date,
        default=None,
        help=(
            "Дата snapshot YYYY-MM-DD. Если не указана, "
            "используется максимальная дата в БД."
        ),
    )
    return parser.parse_args()


def main():
    args = parse_arguments()
    session = get_session()

    try:
        data_date = resolve_data_date(session, args.data_date)
        report = build_coverage_report(session, data_date)
        print_report(report)
    finally:
        session.close()


if __name__ == "__main__":
    main()
