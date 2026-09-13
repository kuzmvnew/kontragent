from time import perf_counter

from sqlalchemy import text

from app.database.postgres import engine


INDEXES = [
    {
        "name": "ix_companies_name_trgm",
        "sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_companies_name_trgm
            ON companies
            USING gin
            (name gin_trgm_ops)
        """,
    },
    {
        "name": "ix_companies_inn_pattern",
        "sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_companies_inn_pattern
            ON companies
            (inn varchar_pattern_ops)
        """,
    },
    {
        "name": "ix_companies_ogrn_pattern",
        "sql": """
            CREATE INDEX CONCURRENTLY IF NOT EXISTS
            ix_companies_ogrn_pattern
            ON companies
            (ogrn varchar_pattern_ops)
        """,
    },
]


def create_pg_trgm(
    connection,
):
    print()
    print(
        "Проверяем pg_trgm..."
    )

    connection.execute(
        text(
            """
            CREATE EXTENSION
            IF NOT EXISTS pg_trgm
            """
        )
    )

    print(
        "pg_trgm: OK"
    )


def create_indexes(
    connection,
):
    for item in INDEXES:
        print()
        print(
            "=========================================="
        )
        print(
            "INDEX:",
            item["name"],
        )
        print(
            "=========================================="
        )

        start = perf_counter()

        connection.execute(
            text(
                item["sql"]
            )
        )

        elapsed = (
            perf_counter()
            - start
        )

        print(
            "Готово за:",
            f"{elapsed:.2f}",
            "сек."
        )


def analyze_companies(
    connection,
):
    print()
    print(
        "=========================================="
    )
    print(
        "ANALYZE COMPANIES"
    )
    print(
        "=========================================="
    )

    start = perf_counter()

    connection.execute(
        text(
            """
            ANALYZE companies
            """
        )
    )

    elapsed = (
        perf_counter()
        - start
    )

    print(
        "ANALYZE завершён за:",
        f"{elapsed:.2f}",
        "сек."
    )


def show_indexes(
    connection,
):
    print()
    print(
        "=========================================="
    )
    print(
        "SEARCH INDEXES"
    )
    print(
        "=========================================="
    )

    rows = connection.execute(
        text(
            """
            SELECT
                indexname,
                pg_size_pretty(
                    pg_relation_size(
                        quote_ident(indexname)
                    )
                ) AS size
            FROM pg_indexes
            WHERE tablename = 'companies'
              AND indexname IN
              (
                  'ix_companies_name_trgm',
                  'ix_companies_inn_pattern',
                  'ix_companies_ogrn_pattern'
              )
            ORDER BY indexname
            """
        )
    ).all()

    for row in rows:
        print(
            row.indexname,
            "|",
            row.size,
        )


def main():
    print()
    print(
        "=========================================="
    )
    print(
        "FAST SEARCH SETUP"
    )
    print(
        "=========================================="
    )

    # CREATE INDEX CONCURRENTLY нельзя
    # выполнять внутри обычной транзакции.
    #
    # Поэтому используем AUTOCOMMIT.
    with engine.connect().execution_options(
        isolation_level="AUTOCOMMIT"
    ) as connection:

        create_pg_trgm(
            connection
        )

        create_indexes(
            connection
        )

        analyze_companies(
            connection
        )

        show_indexes(
            connection
        )

    print()
    print(
        "=========================================="
    )
    print(
        "FAST SEARCH SETUP COMPLETE"
    )
    print(
        "=========================================="
    )
    print()


if __name__ == "__main__":
    main()