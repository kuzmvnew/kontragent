from sqlalchemy import select

from app.database.postgres import get_session
from app.models.source import (
    DataSet,
    DataSource,
)


DATASET_CODE = "fns_tax_offence"

DATASET_NAME = (
    "ФНС — налоговые правонарушения "
    "и штрафы"
)

DATASET_DESCRIPTION = (
    "Официальный открытый набор ФНС "
    "со сведениями о налоговых "
    "правонарушениях и суммах штрафов."
)

SOURCE_URL = (
    "https://www.nalog.gov.ru/"
    "opendata/7707329152-taxoffence/"
)


def main():
    session = get_session()

    try:
        print()
        print(
            "=========================================="
        )
        print(
            "REGISTER FNS TAX OFFENCE DATASET"
        )
        print(
            "=========================================="
        )
        print()

        existing = (
            session.execute(
                select(DataSet)
                .where(
                    DataSet.code
                    == DATASET_CODE
                )
            )
            .scalar_one_or_none()
        )

        if existing is not None:
            print(
                "Dataset уже существует."
            )
            print()

            print(
                "id:",
                existing.id,
            )

            print(
                "code:",
                existing.code,
            )

            print(
                "name:",
                existing.name,
            )

            print(
                "domain:",
                existing.domain,
            )

            print(
                "update_mode:",
                existing.update_mode,
            )

            print(
                "data_format:",
                existing.data_format,
            )

            print(
                "refresh_schedule:",
                existing.refresh_schedule,
            )

            print(
                "priority:",
                existing.priority,
            )

            print(
                "enabled:",
                existing.enabled,
            )

            print()

            return

        source = (
            session.execute(
                select(DataSource)
                .where(
                    DataSource.code
                    == "fns"
                )
            )
            .scalar_one_or_none()
        )

        if source is None:
            raise RuntimeError(
                "Источник с code='fns' "
                "не найден в data_sources."
            )

        print(
            "Источник ФНС найден:"
        )

        print(
            "source_id:",
            source.id,
        )

        print(
            "source name:",
            source.name,
        )

        print()

        dataset = DataSet(
            source_id=source.id,
            code=DATASET_CODE,
            name=DATASET_NAME,
            domain="tax_offence",
            update_mode="bulk",
            data_format="xml",
            refresh_schedule="annual",
            priority=10,
            enabled=False,
            source_url=SOURCE_URL,
            description=(
                DATASET_DESCRIPTION
            ),
        )

        session.add(
            dataset
        )

        session.commit()

        session.refresh(
            dataset
        )

        print(
            "=========================================="
        )
        print(
            "DATASET СОЗДАН"
        )
        print(
            "=========================================="
        )
        print()

        print(
            "id:",
            dataset.id,
        )

        print(
            "source_id:",
            dataset.source_id,
        )

        print(
            "code:",
            dataset.code,
        )

        print(
            "name:",
            dataset.name,
        )

        print(
            "domain:",
            dataset.domain,
        )

        print(
            "update_mode:",
            dataset.update_mode,
        )

        print(
            "data_format:",
            dataset.data_format,
        )

        print(
            "refresh_schedule:",
            dataset.refresh_schedule,
        )

        print(
            "priority:",
            dataset.priority,
        )

        print(
            "enabled:",
            dataset.enabled,
        )

        print(
            "source_url:",
            dataset.source_url,
        )

        print()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()