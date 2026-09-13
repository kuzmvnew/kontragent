from time import perf_counter

from app.services.company_service import (
    search_companies,
)


TEST_QUERIES = [
    "3701047965",
    "370104",
    "КРЫМАВТОДОР",
    'ООО ЧОО "АЛЬЯНС"',
    "Фортуна",
]


def run_test(
    query: str,
    repeats: int = 3,
):
    print()
    print(
        "=========================================="
    )
    print(
        "QUERY:",
        query,
    )
    print(
        "=========================================="
    )

    # Первый запрос прогревает PostgreSQL cache.
    start = perf_counter()

    first_result = search_companies(
        query,
        limit=20,
    )

    first_elapsed = (
        perf_counter()
        - start
    )

    print(
        "Первый запрос:",
        f"{first_elapsed:.4f}",
        "сек."
    )

    times = []

    for number in range(
        1,
        repeats + 1,
    ):
        start = perf_counter()

        results = search_companies(
            query,
            limit=20,
        )

        elapsed = (
            perf_counter()
            - start
        )

        times.append(
            elapsed
        )

        print(
            f"Повтор {number}:",
            f"{elapsed:.4f}",
            "сек.",
        )

    average = (
        sum(times)
        / len(times)
    )

    print(
        "Среднее после прогрева:",
        f"{average:.4f}",
        "сек.",
    )

    print(
        "Найдено:",
        len(first_result),
    )

    if first_result:
        print()
        print(
            "Первые результаты:"
        )

        for item in first_result[:3]:
            print(
                "-",
                item.get("inn"),
                "|",
                item.get("name"),
            )


def main():
    print()
    print(
        "=========================================="
    )
    print(
        "SEARCH BENCHMARK"
    )
    print(
        "=========================================="
    )

    for query in TEST_QUERIES:
        run_test(
            query=query,
            repeats=3,
        )

    print()
    print(
        "BENCHMARK FINISHED"
    )
    print()


if __name__ == "__main__":
    main()