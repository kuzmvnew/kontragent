from __future__ import annotations

from sqlalchemy import delete

from app.database.postgres import get_session
from app.models.company import Company
from app.services.company_service import normalize_company_name, search_companies


ROWS = (
    {"inn": "9900000101", "ogrn": "1099000000101", "name": "ООО «Альфа Квантлогистикс 9901»", "source": "stage16_test"},
    {"inn": "9900000102", "ogrn": "1099000000102", "name": "ПАО Альфа Квантбанк 9902", "source": "stage16_test"},
    {"inn": "9900000103", "ogrn": "1099000000103", "name": "ООО Бета Транс", "source": "stage16_test"},
)


def _seed():
    with get_session() as session:
        session.execute(delete(Company).where(Company.inn.in_([row["inn"] for row in ROWS])))
        session.add_all(Company(**row) for row in ROWS)
        session.commit()


def _cleanup():
    with get_session() as session:
        session.execute(delete(Company).where(Company.inn.in_([row["inn"] for row in ROWS])))
        session.commit()


def test_search_exact_prefix_ogrn_normalized_and_typo():
    _seed()
    try:
        assert search_companies("9900000101")[0]["inn"] == "9900000101"
        assert {row["inn"] for row in search_companies("99000001")} >= {row["inn"] for row in ROWS}
        assert search_companies("1099000000103")[0]["inn"] == "9900000103"
        assert search_companies("ооо «альфа квантлогистикс 9901»")[0]["inn"] == "9900000101"
        assert search_companies("АЛЬФА КВАНТЛОГИСТИКС 9901")[0]["inn"] == "9900000101"
        assert search_companies("альфа квантлогестикс 9901")[0]["inn"] == "9900000101"
        assert search_companies("квантлог", limit=2)
    finally:
        _cleanup()


def test_company_name_normalization_removes_quotes_and_legal_form():
    assert normalize_company_name("ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ «Ёжик»") == "ежик"
