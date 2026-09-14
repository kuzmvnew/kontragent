from pathlib import Path
import subprocess
import sys

from sqlalchemy import inspect, text

from app.database.postgres import engine


PROJECT_ROOT = Path(
    __file__
).resolve().parents[1]


EXPECTED_FILES = [
    "main.py",
    "templates/company.html",
    "templates/index.html",
    "static/css/style.css",
    "app/aggregators/company_aggregator.py",
    "app/services/company_service.py",
    "app/services/headcount_service.py",
    "app/services/tax_debt_service.py",
    "app/services/tax_offence_service.py",
    "app/services/tax_payment_service.py",
    "app/ingestion/fns_tax_debt.py",
    "app/ingestion/fns_tax_offence.py",
    "app/ingestion/fns_tax_payment.py",
    "app/models/tax_debt.py",
    "app/models/tax_offence.py",
    "app/models/tax_payment.py",
]


COMPILE_FILES = [
    "main.py",
    "app/aggregators/company_aggregator.py",
    "app/services/tax_debt_service.py",
    "app/services/tax_offence_service.py",
    "app/services/tax_payment_service.py",
    "app/ingestion/fns_tax_payment.py",
    "app/models/tax_payment.py",
]


DATASET_CODES = [
    "fns_msp",
    "fns_headcount",
    "fns_tax_debt",
    "fns_tax_offence",
    "fns_tax_paid",
    "fns_revenue_expenses",
]


COUNT_TABLES = [
    "companies",
    "company_msp_profiles",
    "company_headcounts",
    "company_tax_debt_snapshots",
    "company_tax_debt_items",
    "company_tax_offences",
    "company_tax_payment_snapshots",
    "company_tax_payment_items",
]


def title(value):
    print()
    print("=" * 70)
    print(value)
    print("=" * 70)
    print()


def ok(label):
    print(
        f"[OK]   {label}"
    )


def warning(label):
    print(
        f"[WARN] {label}"
    )


def fail(label):
    print(
        f"[FAIL] {label}"
    )


def read_text(relative_path):
    path = (
        PROJECT_ROOT
        / relative_path
    )

    if not path.exists():
        return ""

    return path.read_text(
        encoding="utf-8",
        errors="replace",
    )


def run_git_status():
    title(
        "GIT STATUS"
    )

    result = subprocess.run(
        [
            "git",
            "status",
            "--short",
        ],
        cwd=PROJECT_ROOT,
        capture_output=True,
        text=True,
    )

    if result.returncode != 0:

        fail(
            "git status завершился ошибкой"
        )

        print(
            result.stderr
        )

        return

    output = (
        result.stdout.strip()
    )

    if not output:

        ok(
            "working tree clean"
        )

    else:

        warning(
            "есть незакоммиченные изменения"
        )

        print(
            output
        )


def check_files():
    title(
        "PROJECT FILES"
    )

    for relative_path in (
        EXPECTED_FILES
    ):

        path = (
            PROJECT_ROOT
            / relative_path
        )

        if path.exists():

            ok(
                relative_path
            )

        else:

            fail(
                relative_path
            )


def compile_python():
    title(
        "PYTHON COMPILE"
    )

    for relative_path in (
        COMPILE_FILES
    ):

        path = (
            PROJECT_ROOT
            / relative_path
        )

        if not path.exists():

            fail(
                f"{relative_path}: файл отсутствует"
            )

            continue

        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "py_compile",
                str(path),
            ],
            cwd=PROJECT_ROOT,
            capture_output=True,
            text=True,
        )

        if result.returncode == 0:

            ok(
                relative_path
            )

        else:

            fail(
                relative_path
            )

            print(
                result.stderr
            )


def check_code_links():
    title(
        "CODE INTEGRATION"
    )

    aggregator = read_text(
        "app/aggregators/company_aggregator.py"
    )

    template = read_text(
        "templates/company.html"
    )

    main_py = read_text(
        "main.py"
    )

    aggregator_checks = {
        "Aggregator imports tax_payment_service":
            (
                "tax_payment_service"
                in aggregator
            ),

        "Aggregator exposes tax_payment":
            (
                '"tax_payment"'
                in aggregator
                or "'tax_payment'"
                in aggregator
            ),

        "Aggregator exposes tax_payment_history":
            (
                "tax_payment_history"
                in aggregator
            ),

        "Aggregator adds fns_tax_paid":
            (
                "fns_tax_paid"
                in aggregator
            ),
    }

    for label, passed in (
        aggregator_checks.items()
    ):

        if passed:
            ok(label)
        else:
            fail(label)

    print()

    template_checks = {
        "Template renders tax_debt":
            (
                "tax_debt"
                in template
            ),

        "Template renders tax_offence":
            (
                "tax_offence"
                in template
            ),

        "Template renders tax_payment":
            (
                "tax_payment"
                in template
            ),

        "Template maps fns_tax_paid label":
            (
                "fns_tax_paid"
                in template
            ),

        "Template maps fns_tax_offence label":
            (
                "fns_tax_offence"
                in template
            ),
    }

    for label, passed in (
        template_checks.items()
    ):

        if passed:
            ok(label)
        else:
            fail(label)

    print()

    if (
        "fns_tax_paid"
        in aggregator
        and "tax_payment"
        not in template
    ):

        fail(
            "КРИТИЧНО: backend PAYTAX подключён, "
            "но company.html его не отображает"
        )

    if (
        "fns_tax_paid"
        in aggregator
        and "fns_tax_paid"
        not in template
    ):

        fail(
            "КРИТИЧНО: source fns_tax_paid "
            "не имеет человекочитаемого label в template"
        )

    if (
        "get_company_for_web"
        in main_py
    ):

        ok(
            "main.py использует get_company_for_web"
        )

    else:

        warning(
            "В main.py не найден get_company_for_web"
        )

    if (
        "company.html"
        in main_py
    ):

        ok(
            "main.py содержит company.html"
        )

    else:

        warning(
            "В main.py не найден company.html"
        )


def database_audit():
    title(
        "DATABASE"
    )

    inspector = inspect(
        engine
    )

    table_names = set(
        inspector.get_table_names()
    )

    for table_name in (
        COUNT_TABLES
    ):

        if (
            table_name
            not in table_names
        ):

            fail(
                f"{table_name}: таблица отсутствует"
            )

            continue

        with engine.connect() as connection:

            count = (
                connection.execute(
                    text(
                        f"""
                        SELECT COUNT(*)
                        FROM {table_name}
                        """
                    )
                )
                .scalar_one()
            )

        ok(
            f"{table_name}: {count:,}"
        )

    print()

    if (
        "alembic_version"
        in table_names
    ):

        with engine.connect() as connection:

            version = (
                connection.execute(
                    text(
                        """
                        SELECT version_num
                        FROM alembic_version
                        """
                    )
                )
                .scalar_one_or_none()
            )

        print(
            "Alembic head:",
            version,
        )


def dataset_audit():
    title(
        "DATASETS"
    )

    inspector = inspect(
        engine
    )

    if (
        "data_sets"
        not in inspector.get_table_names()
    ):

        fail(
            "data_sets отсутствует"
        )

        return

    with engine.connect() as connection:

        for code in DATASET_CODES:

            row = (
                connection.execute(
                    text(
                        """
                        SELECT
                            code,
                            name,
                            domain,
                            update_mode,
                            data_format,
                            refresh_schedule,
                            priority,
                            enabled,
                            source_url,
                            last_data_date,
                            last_success_at

                        FROM data_sets

                        WHERE code = :code
                        """
                    ),
                    {
                        "code": code,
                    },
                )
                .mappings()
                .one_or_none()
            )

            if row is None:

                warning(
                    f"{code}: dataset не найден"
                )

                continue

            print(
                f"{code}:"
            )

            print(
                "  name:",
                row["name"],
            )

            print(
                "  domain:",
                row["domain"],
            )

            print(
                "  enabled:",
                row["enabled"],
            )

            print(
                "  last_data_date:",
                row["last_data_date"],
            )

            print(
                "  last_success_at:",
                row["last_success_at"],
            )

            print(
                "  source_url:",
                row["source_url"],
            )

            if (
                row["last_data_date"]
                is not None
                and not row["enabled"]
            ):

                warning(
                    f"{code}: данные загружены, "
                    "но dataset enabled=False"
                )

            print()


def paytax_quality():
    title(
        "PAYTAX QUALITY"
    )

    inspector = inspect(
        engine
    )

    required = {
        "company_tax_payment_snapshots",
        "company_tax_payment_items",
        "data_sets",
    }

    if not required.issubset(
        set(
            inspector.get_table_names()
        )
    ):

        fail(
            "PAYTAX tables отсутствуют"
        )

        return

    with engine.connect() as connection:

        dataset_id = (
            connection.execute(
                text(
                    """
                    SELECT id
                    FROM data_sets
                    WHERE code = 'fns_tax_paid'
                    """
                )
            )
            .scalar_one_or_none()
        )

        if dataset_id is None:

            fail(
                "fns_tax_paid dataset отсутствует"
            )

            return

        snapshots = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)
                    FROM company_tax_payment_snapshots
                    WHERE dataset_id = :dataset_id
                    """
                ),
                {
                    "dataset_id": dataset_id,
                },
            )
            .scalar_one()
        )

        items = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)

                    FROM company_tax_payment_items i

                    JOIN company_tax_payment_snapshots s
                      ON s.id = i.snapshot_id

                    WHERE s.dataset_id = :dataset_id
                    """
                ),
                {
                    "dataset_id": dataset_id,
                },
            )
            .scalar_one()
        )

        formula_errors = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)

                    FROM company_tax_payment_snapshots

                    WHERE
                        dataset_id = :dataset_id

                        AND total_amount
                            <>
                            (
                                tax_amount
                                + insurance_amount
                                + penalty_amount
                                + non_tax_amount
                                + other_amount
                            )
                    """
                ),
                {
                    "dataset_id": dataset_id,
                },
            )
            .scalar_one()
        )

        print(
            "Snapshots:",
            snapshots,
        )

        print(
            "Items:",
            items,
        )

        print(
            "Formula errors:",
            formula_errors,
        )

        if formula_errors == 0:
            ok(
                "PAYTAX snapshot formula"
            )
        else:
            fail(
                "PAYTAX snapshot formula"
            )

        print()
        print(
            "PAYMENT TYPES:"
        )

        rows = connection.execute(
            text(
                """
                SELECT
                    i.payment_type,
                    COUNT(*) AS rows_count,
                    SUM(i.amount) AS total_amount

                FROM company_tax_payment_items i

                JOIN company_tax_payment_snapshots s
                  ON s.id = i.snapshot_id

                WHERE
                    s.dataset_id = :dataset_id

                GROUP BY
                    i.payment_type

                ORDER BY
                    total_amount DESC
                """
            ),
            {
                "dataset_id": dataset_id,
            },
        ).all()

        for row in rows:

            print(
                row
            )

        print()
        print(
            "OTHER:"
        )

        other_rows = connection.execute(
            text(
                """
                SELECT
                    i.tax_name,
                    COUNT(*),
                    SUM(i.amount)

                FROM company_tax_payment_items i

                JOIN company_tax_payment_snapshots s
                  ON s.id = i.snapshot_id

                WHERE
                    s.dataset_id = :dataset_id
                    AND i.payment_type = 'other'

                GROUP BY
                    i.tax_name

                ORDER BY
                    SUM(i.amount) DESC
                """
            ),
            {
                "dataset_id": dataset_id,
            },
        ).all()

        for row in other_rows:

            print(
                row
            )


def latest_ingestion_runs():
    title(
        "LATEST INGESTION RUNS"
    )

    inspector = inspect(
        engine
    )

    if (
        "ingestion_runs"
        not in inspector.get_table_names()
    ):

        warning(
            "ingestion_runs отсутствует"
        )

        return

    try:

        with engine.connect() as connection:

            rows = connection.execute(
                text(
                    """
                    SELECT
                        r.id,
                        d.code,
                        r.status,
                        r.started_at,
                        r.finished_at,
                        r.data_date,
                        r.rows_read,
                        r.rows_inserted,
                        r.rows_updated,
                        r.rows_skipped,
                        r.errors_count

                    FROM ingestion_runs r

                    JOIN data_sets d
                      ON d.id = r.dataset_id

                    ORDER BY
                        r.id DESC

                    LIMIT 12
                    """
                )
            ).all()

        for row in rows:

            print(
                row
            )

    except Exception as error:

        warning(
            "Не удалось вывести ingestion_runs"
        )

        print(
            error
        )


def main():
    title(
        "KONTRAGENT PROJECT AUDIT"
    )

    print(
        "Project:",
        PROJECT_ROOT,
    )

    run_git_status()
    check_files()
    compile_python()
    check_code_links()
    database_audit()
    dataset_audit()
    paytax_quality()
    latest_ingestion_runs()

    title(
        "AUDIT FINISHED"
    )


if __name__ == "__main__":
    main()