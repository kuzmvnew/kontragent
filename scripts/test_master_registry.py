from app.database.postgres import get_session
from app.models.company import Company
from app.services.master_registry_service import (
    upsert_master_company,
)


TEST_INN = "3701047965"


def main():
    session = get_session()

    try:
        company = (
            session.query(
                Company
            )
            .filter(
                Company.inn
                == TEST_INN
            )
            .one()
        )

        original = {
            "name": company.name,
            "entity_type": (
                company.entity_type
            ),
            "master_dataset_id": (
                company.master_dataset_id
            ),
            "master_data_date": (
                company.master_data_date
            ),
            "source": (
                company.source
            ),
            "source_updated_at": (
                company.source_updated_at
            ),
        }

    finally:
        session.close()

    print()
    print(
        "До теста:"
    )
    print(
        original
    )

    result = (
        upsert_master_company(
            dataset_code=(
                "excel_companies"
            ),
            inn=TEST_INN,
            name=original[
                "name"
            ],
            entity_type=(
                "legal"
            ),
        )
    )

    print()
    print(
        "Результат upsert:"
    )
    print(
        result
    )

    # ---------------------------------------------
    # RESTORE
    # ---------------------------------------------

    session = get_session()

    try:
        company = (
            session.query(
                Company
            )
            .filter(
                Company.inn
                == TEST_INN
            )
            .one()
        )

        company.name = (
            original["name"]
        )

        company.entity_type = (
            original[
                "entity_type"
            ]
        )

        company.master_dataset_id = (
            original[
                "master_dataset_id"
            ]
        )

        company.master_data_date = (
            original[
                "master_data_date"
            ]
        )

        company.source = (
            original["source"]
        )

        company.source_updated_at = (
            original[
                "source_updated_at"
            ]
        )

        session.commit()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()

    print()
    print(
        "Тест завершён."
    )
    print(
        "Исходное состояние восстановлено."
    )
    print()


if __name__ == "__main__":
    main()