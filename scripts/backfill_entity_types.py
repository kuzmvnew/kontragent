from sqlalchemy import update

from app.database.postgres import get_session
from app.models.company import Company


def main():
    session = get_session()

    try:
        legal_result = session.execute(
            update(Company)
            .where(
                Company.entity_type.is_(None),
                Company.inn.op("~")(r"^\d{10}$"),
            )
            .values(
                entity_type="legal"
            )
        )

        entrepreneur_result = session.execute(
            update(Company)
            .where(
                Company.entity_type.is_(None),
                Company.inn.op("~")(r"^\d{12}$"),
            )
            .values(
                entity_type=(
                    "individual_entrepreneur"
                )
            )
        )

        session.commit()

        print()
        print(
            "======================================"
        )
        print(
            "ENTITY TYPE BACKFILL"
        )
        print(
            "======================================"
        )

        print(
            "ЮЛ:",
            legal_result.rowcount,
        )

        print(
            "ИП:",
            entrepreneur_result.rowcount,
        )

        print()

    except Exception:
        session.rollback()
        raise

    finally:
        session.close()


if __name__ == "__main__":
    main()