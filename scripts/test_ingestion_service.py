from app.database.postgres import get_session
from app.models.source import (
    DataSet,
    IngestionRun,
)
from app.services.ingestion_service import (
    finish_ingestion_success,
    get_ingestion_run,
    start_ingestion,
)


DATASET_CODE = "excel_companies"


def main():
    print()
    print(
        "======================================"
    )
    print(
        "INGESTION SERVICE SMOKE TEST"
    )
    print(
        "======================================"
    )
    print()

    # -----------------------------------------
    # Сохраняем исходное состояние dataset
    # -----------------------------------------

    session = get_session()

    try:
        dataset = (
            session.query(DataSet)
            .filter(
                DataSet.code
                == DATASET_CODE
            )
            .one()
        )

        old_last_success_at = (
            dataset.last_success_at
        )

        old_last_data_date = (
            dataset.last_data_date
        )

    finally:
        session.close()

    run_id = None

    try:
        # -------------------------------------
        # START
        # -------------------------------------

        run_id = start_ingestion(
            dataset_code=DATASET_CODE,
            source_file_name=(
                "smoke_test.xlsx"
            ),
            details={
                "smoke_test": True,
            },
        )

        print(
            f"Создан run_id: {run_id}"
        )

        running = (
            get_ingestion_run(
                run_id
            )
        )

        print(
            "Статус после START:",
            running["status"],
        )

        if (
            running["status"]
            != "running"
        ):
            raise RuntimeError(
                "Ожидался status=running"
            )

        # -------------------------------------
        # SUCCESS
        # -------------------------------------

        finished = (
            finish_ingestion_success(
                run_id=run_id,
                rows_read=100,
                rows_inserted=10,
                rows_updated=80,
                rows_skipped=10,
                errors_count=0,
                details={
                    "smoke_test_finished": True,
                },
            )
        )

        print(
            "Статус после FINISH:",
            finished["status"],
        )

        print(
            "rows_read:",
            finished["rows_read"],
        )

        print(
            "rows_inserted:",
            finished["rows_inserted"],
        )

        print(
            "rows_updated:",
            finished["rows_updated"],
        )

        print(
            "rows_skipped:",
            finished["rows_skipped"],
        )

        if (
            finished["status"]
            != "success"
        ):
            raise RuntimeError(
                "Ожидался status=success"
            )

        if (
            finished["rows_read"]
            != 100
        ):
            raise RuntimeError(
                "Некорректный rows_read"
            )

        print()
        print(
            "SMOKE TEST OK"
        )

    finally:

        # -------------------------------------
        # CLEANUP
        # -------------------------------------

        session = get_session()

        try:
            if run_id is not None:

                run = session.get(
                    IngestionRun,
                    run_id,
                )

                if run is not None:
                    session.delete(
                        run
                    )

            dataset = (
                session.query(DataSet)
                .filter(
                    DataSet.code
                    == DATASET_CODE
                )
                .one()
            )

            dataset.last_success_at = (
                old_last_success_at
            )

            dataset.last_data_date = (
                old_last_data_date
            )

            session.commit()

        except Exception:
            session.rollback()
            raise

        finally:
            session.close()

        print(
            "Тестовые данные удалены."
        )
        print()


if __name__ == "__main__":
    main()