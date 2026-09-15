from sqlalchemy.dialects import postgresql

from scripts.extend_master_registry_from_excel import (
    build_company_payload,
    build_insert_statement,
    classify_entity_type,
    clean_optional_identifier,
)


def test_classify_entity_type():
    assert classify_entity_type("7701234567") == "legal"
    assert (
        classify_entity_type("770123456789")
        == "individual_entrepreneur"
    )
    assert classify_entity_type("123") is None
    assert classify_entity_type(None) is None


def test_clean_optional_identifier():
    assert (
        clean_optional_identifier(
            "77 0101 001",
            max_length=9,
        )
        == "770101001"
    )

    assert (
        clean_optional_identifier(
            "41745829450001",
            max_length=12,
        )
        is None
    )

    assert (
        clean_optional_identifier(
            None,
            max_length=12,
        )
        is None
    )


def test_build_company_payload():
    row = {
        "Название": "ООО ТЕСТ",
        "ИНН": "77 0123 4567",
        "КПП": "770101001",
        "ОГРН": "1027700000000",
        "ОКПО": "12345678",
        "Адрес": "Москва",
        "Вид деятельности": "Разработка ПО",
        "Сайт": "example.ru",
    }

    payload = build_company_payload(row)

    assert payload["inn"] == "7701234567"
    assert payload["entity_type"] == "legal"
    assert payload["name"] == "ООО ТЕСТ"
    assert payload["source"] == "excel_import"
    assert payload["ogrn"] == "1027700000000"
    assert payload["okpo"] == "12345678"


def test_build_company_payload_drops_overlong_optional_identifier():
    row = {
        "Название": "ООО ТЕСТ",
        "ИНН": "7701234567",
        "КПП": "770101001",
        "ОГРН": "1027700000000",
        "ОКПО": "41745829450001",
    }

    payload = build_company_payload(row)

    assert payload["inn"] == "7701234567"
    assert payload["okpo"] is None


def test_insert_statement_is_do_nothing():
    statement = build_insert_statement(
        [
            {
                "inn": "7701234567",
                "name": "ООО ТЕСТ",
                "entity_type": "legal",
                "source": "excel_import",
            }
        ]
    )

    sql = str(
        statement.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={
                "literal_binds": False,
            },
        )
    ).upper()

    assert "ON CONFLICT DO NOTHING" in sql
    assert "DO UPDATE" not in sql
