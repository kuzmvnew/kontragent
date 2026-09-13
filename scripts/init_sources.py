from app.services.source_service import (
    list_sources,
    sync_default_sources,
)


def main():
    print()
    print("Создаём реестр источников...")
    print()

    sync_default_sources()

    sources = list_sources()

    print(
        "======================================"
    )

    print(
        "SOURCE REGISTRY"
    )

    print(
        "======================================"
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


if __name__ == "__main__":
    main()