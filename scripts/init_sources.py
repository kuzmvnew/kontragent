from app.services.source_service import (
    list_datasets,
    list_sources,
    sync_default_registry,
)


def main():
    print()
    print(
        "Создаём Source Registry v2..."
    )
    print()

    sync_default_registry()

    sources = list_sources()
    datasets = list_datasets()

    print(
        "=============================================="
    )
    print(
        "DATA SOURCES"
    )
    print(
        "=============================================="
    )

    for source in sources:

        status = (
            "ON"
            if source["enabled"]
            else "OFF"
        )

        print(
            f'{source["priority"]:>3} | '
            f'{status:<3} | '
            f'{source["code"]:<15} | '
            f'{source["name"]}'
        )

    print()
    print(
        "=============================================="
    )
    print(
        "DATASETS"
    )
    print(
        "=============================================="
    )

    for dataset in datasets:

        status = (
            "ON"
            if dataset["enabled"]
            else "OFF"
        )

        print(
            f'{status:<3} | '
            f'{dataset["source_code"]:<12} | '
            f'{dataset["domain"]:<18} | '
            f'{dataset["update_mode"]:<7} | '
            f'{dataset["code"]}'
        )

    print()
    print(
        f"Источников: {len(sources)}"
    )
    print(
        f"Наборов данных: {len(datasets)}"
    )
    print()


if __name__ == "__main__":
    main()