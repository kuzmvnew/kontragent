from scripts.extend_master_registry_from_excel import (
    build_company_payload,
    build_insert_statement,
    classify_entity_type,
)


def test_classify_entity_type():
    assert classify_entity_type("7701234567") == "legal"
    assert (
        classify_entity_type("770123456789")
        == "individual_entrepreneur"
    )
    assert classify_entity_type("123") is None
    assert classify_entity_type(None) is None


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
            dialect=statement.dialect,
            compile_kwargs={
                "literal_binds": False,
            },
        )
    ).upper()

    assert "ON CONFLICT DO NOTHING" in sql
    assert "DO UPDATE" not in sql
