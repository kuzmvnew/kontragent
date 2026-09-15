import csv
from datetime import date

import pytest

from app.ingestion.fns_disqualified import (
    EXPECTED_HEADER,
    iter_disqualified_records,
    normalize_organization_inn,
    parse_disqualified_row,
    parse_fns_date,
)


def test_parse_fns_date():
    assert (
        parse_fns_date("29.10.1979")
        == date(1979, 10, 29)
    )

    assert parse_fns_date("") is None
    assert parse_fns_date(None) is None
    assert parse_fns_date("wrong") is None


def test_normalize_organization_inn():
    assert (
        normalize_organization_inn(
            "7203435200"
        )
        == "7203435200"
    )

    assert (
        normalize_organization_inn("")
        is None
    )

    assert (
        normalize_organization_inn(
            "123"
        )
        is None
    )

    assert (
        normalize_organization_inn(
            "abcdefghij"
        )
        is None
    )


def test_parse_row_without_inn():
    row = [
        "247700068340",
        "ГЛУХОВ ДМИТРИЙ ВЛАДИМИРОВИЧ",
        "29.10.1979",
        "Г. НИЖНИЙ НОВГОРОД",
        'ООО "ПОРТАЛ"',
        "",
        "УЧРЕДИТЕЛЬ",
        "Ч.5 СТ. 14.25 КОАП РФ",
        "МИФНС РОССИИ № 46 ПО Г. МОСКВЕ",
        "КУРАХТАНОВ А В",
        "МИРОВОЙ СУДЬЯ",
        "2 г 0 м 0 д",
        "19.11.2024",
        "18.11.2026",
    ]

    record = parse_disqualified_row(
        row=row,
        data_date=date(
            2026,
            9,
            13,
        ),
    )

    assert record is not None
    assert (
        record["register_number"]
        == "247700068340"
    )
    assert (
        record["full_name"]
        == "ГЛУХОВ ДМИТРИЙ ВЛАДИМИРОВИЧ"
    )
    assert (
        record["birth_date"]
        == date(1979, 10, 29)
    )
    assert (
        record["organization_inn"]
        is None
    )
    assert (
        record["disqualification_term"]
        == "2 г 0 м 0 д"
    )
    assert (
        record["start_date"]
        == date(2024, 11, 19)
    )
    assert (
        record["end_date"]
        == date(2026, 11, 18)
    )


def test_parse_row_with_inn():
    row = [
        "247200065529",
        "КИРПИЧНИКОВ АЛЕКСЕЙ НИКОЛАЕВИЧ",
        "01.01.1980",
        "Г. ТЮМЕНЬ",
        'ООО "ТЮМЕНЬ-24"',
        "7203435200",
        "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР",
        "СТАТЬЯ",
        "ОРГАН",
        "СУДЬЯ",
        "МИРОВОЙ СУДЬЯ",
        "1 г 0 м 0 д",
        "01.01.2026",
        "31.12.2026",
    ]

    record = parse_disqualified_row(
        row=row,
        data_date=date(
            2026,
            9,
            13,
        ),
    )

    assert record is not None
    assert (
        record["organization_inn"]
        == "7203435200"
    )
    assert (
        record["position"]
        == "ГЕНЕРАЛЬНЫЙ ДИРЕКТОР"
    )


def test_iterator_respects_limit(
    tmp_path,
):
    path = tmp_path / "sample.csv"

    rows = [
        [
            "1",
            "ИВАНОВ ИВАН ИВАНОВИЧ",
            "01.01.1980",
            "",
            "ООО ТЕСТ",
            "7701000000",
            "ДИРЕКТОР",
            "СТАТЬЯ",
            "ОРГАН",
            "СУДЬЯ",
            "ДОЛЖНОСТЬ",
            "1 г 0 м 0 д",
            "01.01.2026",
            "31.12.2026",
        ],
        [
            "2",
            "ПЕТРОВ ПЕТР ПЕТРОВИЧ",
            "02.02.1980",
            "",
            "ООО ТЕСТ 2",
            "",
            "РУКОВОДИТЕЛЬ",
            "СТАТЬЯ",
            "ОРГАН",
            "СУДЬЯ",
            "ДОЛЖНОСТЬ",
            "2 г 0 м 0 д",
            "01.01.2025",
            "31.12.2026",
        ],
    ]

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        writer = csv.writer(file)
        writer.writerow(
            EXPECTED_HEADER
        )
        writer.writerows(rows)

    records = list(
        iter_disqualified_records(
            csv_path=path,
            data_date=date(
                2026,
                9,
                13,
            ),
            limit=1,
        )
    )

    assert len(records) == 1
    assert (
        records[0]["register_number"]
        == "1"
    )


def test_iterator_rejects_wrong_header(
    tmp_path,
):
    path = tmp_path / "wrong.csv"

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:

        writer = csv.writer(file)
        writer.writerow(
            ["WRONG", "HEADER"]
        )

    with pytest.raises(
        ValueError,
        match="Unexpected FNS",
    ):
        list(
            iter_disqualified_records(
                csv_path=path,
                data_date=date(
                    2026,
                    9,
                    13,
                ),
            )
        )


def test_import_csv_batches_and_totals(
    tmp_path,
    monkeypatch,
):
    from app.ingestion import (
        fns_disqualified,
    )

    path = (
        tmp_path
        / "sample.csv"
    )

    rows = [
        [
            "1",
            "ИВАНОВ ИВАН ИВАНОВИЧ",
            "01.01.1980",
            "",
            "ООО ТЕСТ",
            "7701000000",
            "ДИРЕКТОР",
            "СТАТЬЯ",
            "ОРГАН",
            "СУДЬЯ",
            "ДОЛЖНОСТЬ",
            "1 г 0 м 0 д",
            "01.01.2026",
            "31.12.2026",
        ],
        [
            "2",
            "ПЕТРОВ ПЕТР ПЕТРОВИЧ",
            "02.02.1980",
            "",
            "ООО ТЕСТ 2",
            "",
            "РУКОВОДИТЕЛЬ",
            "СТАТЬЯ",
            "ОРГАН",
            "СУДЬЯ",
            "ДОЛЖНОСТЬ",
            "2 г 0 м 0 д",
            "01.01.2025",
            "31.12.2026",
        ],
        [
            "3",
            "СИДОРОВ СИДОР СИДОРОВИЧ",
            "03.03.1980",
            "",
            "ООО ТЕСТ 3",
            "7703000000",
            "ДИРЕКТОР",
            "СТАТЬЯ",
            "ОРГАН",
            "СУДЬЯ",
            "ДОЛЖНОСТЬ",
            "1 г 0 м 0 д",
            "01.02.2026",
            "31.12.2026",
        ],
    ]

    with path.open(
        "w",
        encoding="utf-8-sig",
        newline="",
    ) as file:
        writer = csv.writer(
            file
        )
        writer.writerow(
            EXPECTED_HEADER
        )
        writer.writerows(
            rows
        )

    calls = []

    def fake_process(
        records,
        dataset_id,
    ):
        calls.append(
            (
                len(records),
                dataset_id,
            )
        )

        return {
            "processed": len(records),
            "inserted": len(records),
            "updated": 0,
            "skipped": 0,
        }

    monkeypatch.setattr(
        fns_disqualified,
        "process_disqualified_batch",
        fake_process,
    )

    totals = (
        fns_disqualified
        .import_fns_disqualified_csv(
            csv_path=path,
            data_date=date(
                2026,
                9,
                13,
            ),
            batch_size=2,
            dataset_id=38,
        )
    )

    assert calls == [
        (2, 38),
        (1, 38),
    ]

    assert (
        totals["processed"]
        == 3
    )

    assert (
        totals["inserted"]
        == 3
    )

    assert (
        totals["updated"]
        == 0
    )

    assert (
        totals["batches"]
        == 2
    )

    assert (
        totals["with_org_inn"]
        == 2
    )

    assert (
        totals["without_org_inn"]
        == 1
    )


def test_import_rejects_bad_batch_size(
    tmp_path,
):
    from app.ingestion.fns_disqualified import (
        import_fns_disqualified_csv,
    )

    path = (
        tmp_path
        / "sample.csv"
    )

    path.write_text(
        ",".join(
            EXPECTED_HEADER
        )
        + "\n",
        encoding="utf-8",
    )

    with pytest.raises(
        ValueError,
        match="batch_size",
    ):
        import_fns_disqualified_csv(
            csv_path=path,
            data_date=date(
                2026,
                9,
                13,
            ),
            batch_size=0,
            dataset_id=38,
        )
