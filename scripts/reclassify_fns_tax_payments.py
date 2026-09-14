from decimal import Decimal

from sqlalchemy import text

from app.database.postgres import (
    engine,
    get_session,
)
from app.models.source import DataSet


DATASET_CODE = "fns_tax_paid"

ZERO = Decimal("0.00")


def get_dataset_id():
    session = get_session()

    try:

        dataset = (
            session.query(
                DataSet
            )
            .filter(
                DataSet.code
                == DATASET_CODE
            )
            .one_or_none()
        )

        if dataset is None:

            raise RuntimeError(
                f"Dataset {DATASET_CODE} "
                "не найден"
            )

        return dataset.id

    finally:

        session.close()


def print_payment_types(
    connection,
    dataset_id,
):
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

            WHERE s.dataset_id = :dataset_id

            GROUP BY i.payment_type

            ORDER BY total_amount DESC
            """
        ),
        {
            "dataset_id": (
                dataset_id
            ),
        },
    ).all()

    for row in rows:

        print(
            row.payment_type,
            "| rows:",
            row.rows_count,
            "| amount:",
            row.total_amount,
        )


def print_other_names(
    connection,
    dataset_id,
):
    rows = connection.execute(
        text(
            """
            SELECT
                i.tax_name,
                COUNT(*) AS rows_count,
                SUM(i.amount) AS total_amount

            FROM company_tax_payment_items i

            JOIN company_tax_payment_snapshots s
              ON s.id = i.snapshot_id

            WHERE
                s.dataset_id = :dataset_id
                AND i.payment_type = 'other'

            GROUP BY i.tax_name

            ORDER BY total_amount DESC

            LIMIT 100
            """
        ),
        {
            "dataset_id": (
                dataset_id
            ),
        },
    ).all()

    if not rows:

        print(
            "Категория other пуста."
        )

        return

    for row in rows:

        print()
        print(
            "NAME:",
            row.tax_name,
        )

        print(
            "ROWS:",
            row.rows_count,
        )

        print(
            "SUM :",
            row.total_amount,
        )


def main():
    dataset_id = (
        get_dataset_id()
    )

    print()
    print(
        "=========================================="
    )
    print(
        "FNS PAYTAX RECLASSIFICATION"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "Dataset:",
        DATASET_CODE,
    )

    print(
        "Dataset ID:",
        dataset_id,
    )

    print()
    print(
        "СОСТОЯНИЕ ДО ИСПРАВЛЕНИЯ"
    )
    print()

    with engine.connect() as connection:

        print_payment_types(
            connection=connection,
            dataset_id=dataset_id,
        )

    print()
    print(
        "Запускаем транзакцию..."
    )

    with engine.begin() as connection:

        # -------------------------------------------------
        # 1. RECLASSIFY ITEMS
        # -------------------------------------------------

        result = connection.execute(
            text(
                """
                UPDATE company_tax_payment_items i

                SET
                    payment_type =
                        CASE

                            WHEN UPPER(
                                TRIM(i.tax_name)
                            ) = 'СУММЫ ПЕНЕЙ'
                                THEN 'penalty'

                            WHEN UPPER(
                                TRIM(i.tax_name)
                            ) LIKE '%НЕНАЛОГОВ%'
                                THEN 'non_tax'

                            WHEN
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%СТРАХОВ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%ВЗНОС%'
                                THEN 'insurance'

                            WHEN
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%НАЛОГ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%СБОР%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%АКЦИЗ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                )
                                LIKE
                                '%ГОСУДАРСТВЕННАЯ ПОШЛИНА%'
                                THEN 'tax'

                            ELSE 'other'

                        END,

                    updated_at = NOW()

                FROM
                    company_tax_payment_snapshots s

                WHERE
                    i.snapshot_id = s.id

                    AND s.dataset_id =
                        :dataset_id

                    AND i.payment_type
                        IS DISTINCT FROM
                        CASE

                            WHEN UPPER(
                                TRIM(i.tax_name)
                            ) = 'СУММЫ ПЕНЕЙ'
                                THEN 'penalty'

                            WHEN UPPER(
                                TRIM(i.tax_name)
                            ) LIKE '%НЕНАЛОГОВ%'
                                THEN 'non_tax'

                            WHEN
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%СТРАХОВ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%ВЗНОС%'
                                THEN 'insurance'

                            WHEN
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%НАЛОГ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%СБОР%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                ) LIKE '%АКЦИЗ%'
                                OR
                                UPPER(
                                    TRIM(i.tax_name)
                                )
                                LIKE
                                '%ГОСУДАРСТВЕННАЯ ПОШЛИНА%'
                                THEN 'tax'

                            ELSE 'other'

                        END
                """
            ),
            {
                "dataset_id": (
                    dataset_id
                ),
            },
        )

        print()
        print(
            "Переклассифицировано items:",
            result.rowcount,
        )

        # -------------------------------------------------
        # 2. TEMP AGGREGATION
        # -------------------------------------------------

        print()
        print(
            "Создаём временную таблицу "
            "для пересчёта snapshots..."
        )

        connection.execute(
            text(
                """
                CREATE TEMP TABLE
                tmp_fns_tax_payment_recalc
                ON COMMIT DROP
                AS

                SELECT
                    i.snapshot_id,

                    COALESCE(
                        SUM(i.amount),
                        0
                    ) AS total_amount,

                    COALESCE(
                        SUM(i.amount)
                        FILTER (
                            WHERE
                                i.payment_type
                                = 'tax'
                        ),
                        0
                    ) AS tax_amount,

                    COALESCE(
                        SUM(i.amount)
                        FILTER (
                            WHERE
                                i.payment_type
                                = 'insurance'
                        ),
                        0
                    ) AS insurance_amount,

                    COALESCE(
                        SUM(i.amount)
                        FILTER (
                            WHERE
                                i.payment_type
                                = 'penalty'
                        ),
                        0
                    ) AS penalty_amount,

                    COALESCE(
                        SUM(i.amount)
                        FILTER (
                            WHERE
                                i.payment_type
                                = 'non_tax'
                        ),
                        0
                    ) AS non_tax_amount,

                    COALESCE(
                        SUM(i.amount)
                        FILTER (
                            WHERE
                                i.payment_type
                                = 'other'
                        ),
                        0
                    ) AS other_amount

                FROM
                    company_tax_payment_items i

                JOIN
                    company_tax_payment_snapshots s
                  ON s.id = i.snapshot_id

                WHERE
                    s.dataset_id =
                        :dataset_id

                GROUP BY
                    i.snapshot_id
                """
            ),
            {
                "dataset_id": (
                    dataset_id
                ),
            },
        )

        connection.execute(
            text(
                """
                CREATE UNIQUE INDEX
                idx_tmp_fns_tax_payment_recalc_snapshot
                ON tmp_fns_tax_payment_recalc
                (
                    snapshot_id
                )
                """
            )
        )

        # -------------------------------------------------
        # 3. RESET ZERO SNAPSHOTS
        # -------------------------------------------------

        print(
            "Обнуляем агрегаты snapshots..."
        )

        connection.execute(
            text(
                """
                UPDATE
                    company_tax_payment_snapshots

                SET
                    total_amount = 0,
                    tax_amount = 0,
                    insurance_amount = 0,
                    penalty_amount = 0,
                    non_tax_amount = 0,
                    other_amount = 0,
                    updated_at = NOW()

                WHERE
                    dataset_id =
                        :dataset_id
                """
            ),
            {
                "dataset_id": (
                    dataset_id
                ),
            },
        )

        # -------------------------------------------------
        # 4. APPLY RECALCULATED TOTALS
        # -------------------------------------------------

        print(
            "Записываем пересчитанные суммы..."
        )

        result = connection.execute(
            text(
                """
                UPDATE
                    company_tax_payment_snapshots s

                SET
                    total_amount =
                        r.total_amount,

                    tax_amount =
                        r.tax_amount,

                    insurance_amount =
                        r.insurance_amount,

                    penalty_amount =
                        r.penalty_amount,

                    non_tax_amount =
                        r.non_tax_amount,

                    other_amount =
                        r.other_amount,

                    updated_at = NOW()

                FROM
                    tmp_fns_tax_payment_recalc r

                WHERE
                    s.id =
                        r.snapshot_id

                    AND s.dataset_id =
                        :dataset_id
                """
            ),
            {
                "dataset_id": (
                    dataset_id
                ),
            },
        )

        print(
            "Пересчитано snapshots:",
            result.rowcount,
        )

    print()
    print(
        "=========================================="
    )
    print(
        "ПРОВЕРКА ПОСЛЕ ИСПРАВЛЕНИЯ"
    )
    print(
        "=========================================="
    )
    print()

    with engine.connect() as connection:

        print_payment_types(
            connection=connection,
            dataset_id=dataset_id,
        )

        print()
        print(
            "ОСТАВШИЕСЯ OTHER"
        )

        print_other_names(
            connection=connection,
            dataset_id=dataset_id,
        )

        print()
        print(
            "ПРОВЕРКА ФОРМУЛЫ SNAPSHOT"
        )

        invalid_snapshots = (
            connection.execute(
                text(
                    """
                    SELECT COUNT(*)

                    FROM
                        company_tax_payment_snapshots

                    WHERE
                        dataset_id =
                            :dataset_id

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
                    "dataset_id": (
                        dataset_id
                    ),
                },
            )
            .scalar_one()
        )

        print(
            "Snapshots с нарушенной формулой:",
            invalid_snapshots,
        )

        print()
        print(
            "TOTALS ПО SNAPSHOTS"
        )

        row = connection.execute(
            text(
                """
                SELECT

                    SUM(total_amount),

                    SUM(tax_amount),

                    SUM(insurance_amount),

                    SUM(penalty_amount),

                    SUM(non_tax_amount),

                    SUM(other_amount)

                FROM
                    company_tax_payment_snapshots

                WHERE
                    dataset_id =
                        :dataset_id
                """
            ),
            {
                "dataset_id": (
                    dataset_id
                ),
            },
        ).one()

        print(
            "Всего:",
            row[0],
        )

        print(
            "Налоги:",
            row[1],
        )

        print(
            "Страховые взносы:",
            row[2],
        )

        print(
            "Пени:",
            row[3],
        )

        print(
            "Неналоговые платежи:",
            row[4],
        )

        print(
            "Прочее:",
            row[5],
        )

    print()
    print(
        "=========================================="
    )
    print(
        "ГОТОВО"
    )
    print(
        "=========================================="
    )
    print()


if __name__ == "__main__":
    main()