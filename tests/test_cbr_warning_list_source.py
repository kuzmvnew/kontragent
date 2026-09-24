from datetime import date
from types import SimpleNamespace

from jinja2 import Environment, FileSystemLoader

from app.aggregators import company_product_aggregator
from app.ingestion.cbr_warning_list import (
    normalize_warning_record,
    parse_cbr_warning_payload,
)
from app.providers.cbr_warning_list_provider import (
    CbrWarningListProvider,
    FULL_LIST_JSON_URL,
)
from app.services import cbr_warning_list_service
from app.services.cbr_warning_list_service import (
    _serialize_entry,
)
from app.services.cbr_warning_registry_service import (
    build_cbr_warning_dataset_spec,
    build_cbr_warning_source_spec,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b'{"test": true}'

    def json(self):
        return self._payload


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.response


class FakeDbResult:
    def __init__(self, *, scalar=None, rows=None):
        self.scalar = scalar
        self.rows = rows or []

    def scalar_one_or_none(self):
        return self.scalar

    def scalar_one(self):
        return self.scalar

    def scalars(self):
        return self

    def all(self):
        return self.rows


class FakeSession:
    def __init__(self, results):
        self.results = list(results)

    def execute(self, _statement):
        return self.results.pop(0)

    def close(self):
        pass


def sample_row():
    return {
        "id": 12345,
        "dt": "16.09.2026",
        "nameOrg": "ООО ТЕСТ ФИНАНС",
        "inn": "7701234567",
        "addr": "Москва",
        "site": "test-finance.example; second.example",
        "dateUpdate": "15.09.2026",
        "isLikvid": "Нет",
        "comment": "Тестовая запись",
        "OrgType": "Юридическое лицо",
        "Signs": [
            {
                "id": 1,
                "signRus": "Признаки финансовой пирамиды",
            }
        ],
        "Regions": [
            {
                "okato": 45,
                "name": "Москва",
            }
        ],
    }


def test_provider_fetches_official_automation_json():
    client = FakeClient(
        FakeResponse(
            [sample_row()]
        )
    )

    provider = CbrWarningListProvider(
        client=client
    )

    result = provider.fetch_full_list()

    assert result["http_status"] == 200
    assert result["payload"] == [sample_row()]
    assert result["raw_content"] == b'{"test": true}'
    assert client.calls == [FULL_LIST_JSON_URL]


def test_parser_normalizes_documented_cbr_fields():
    record = normalize_warning_record(
        sample_row(),
        data_date=date(2026, 9, 16),
    )

    assert record["cbr_id"] == "12345"
    assert record["inn"] == "7701234567"
    assert record["name"] == "ООО ТЕСТ ФИНАНС"
    assert record["entry_date"] == date(2026, 9, 16)
    assert record["update_date"] == date(2026, 9, 15)
    assert record["sites"] == [
        "test-finance.example",
        "second.example",
    ]
    assert record["signs"] == [
        {
            "id": "1",
            "name": "Признаки финансовой пирамиды",
        }
    ]


def test_parser_preserves_individual_entrepreneur_inn():
    row = sample_row()
    row["inn"] = "770123456789"
    row["OrgType"] = "Индивидуальный предприниматель"

    record = normalize_warning_record(
        row,
        data_date=date(2026, 9, 16),
    )

    assert record["inn"] == "770123456789"
    assert record["org_type"] == "Индивидуальный предприниматель"


def test_parser_accepts_data_wrapper_and_counts_duplicates():
    row = sample_row()

    result = parse_cbr_warning_payload(
        {
            "Data": [row, dict(row)],
        },
        data_date=date(2026, 9, 16),
    )

    assert result["source_records"] == 2
    assert result["imported_records"] == 1
    assert result["duplicate_records"] == 1
    assert result["with_inn"] == 1
    assert result["without_inn"] == 0


def test_ip_is_checked_against_current_snapshot(monkeypatch):
    snapshot_date = date(2026, 9, 16)
    dataset = SimpleNamespace(
        id=10,
        last_data_date=snapshot_date,
        operational_status="current",
        official_actual_until=date.today(),
    )

    session = FakeSession(
        [
            FakeDbResult(scalar=dataset),
            FakeDbResult(scalar=1),
            FakeDbResult(scalar=0),
            FakeDbResult(rows=[]),
        ]
    )

    monkeypatch.setattr(
        cbr_warning_list_service,
        "get_session",
        lambda: session,
    )

    result = (
        cbr_warning_list_service
        .get_cbr_warning_list_check_for_inn(
            "770123456789"
        )
    )

    assert result["result"] == "not_found"
    assert result["checked"] is True
    assert result["applicable"] is True
    assert result["data_date"] == snapshot_date


def test_entry_serialization_preserves_official_detail_link():
    row = SimpleNamespace(
        cbr_id="12345",
        name="ООО ТЕСТ ФИНАНС",
        inn="7701234567",
        entry_date=date(2026, 9, 16),
        update_date=date(2026, 9, 15),
        address="Москва",
        sites=["test-finance.example"],
        signs=[
            {
                "id": "1",
                "name": "Признаки финансовой пирамиды",
            }
        ],
        regions=[{"id": "45", "name": "Москва"}],
        additional_info=None,
        liquidation_status="Нет",
        comment=None,
        org_type="Юридическое лицо",
    )

    result = _serialize_entry(row)

    assert result["signs"] == [
        "Признаки финансовой пирамиды"
    ]
    assert result["detail_url"].endswith("?id=12345")


def test_product_aggregator_adds_cbr_warning_source(monkeypatch):
    company = {
        "inn": "7701234567",
        "name": "ООО ТЕСТ",
        "sources_used": ["fns"],
    }

    check = {
        "checked": True,
        "applicable": True,
        "result": "found",
        "data_date": date(2026, 9, 16),
        "dataset_code": "cbr_warning_list",
        "source": "cbr_warning_list",
        "reason": None,
        "record_count": 1,
        "records": [],
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_cbr_warning_list_check_for_inn",
        lambda inn: check,
    )

    result = (
        company_product_aggregator
        .enrich_company_with_cbr_warning_list(company)
    )

    assert result["cbr_warning_list_check"] == check
    assert "cbr_warning_list" in result["sources_used"]
    assert company["sources_used"] == ["fns"]


def test_unavailable_cbr_check_is_not_marked_as_used(monkeypatch):
    company = {
        "inn": "7701234567",
        "sources_used": [],
    }

    monkeypatch.setattr(
        company_product_aggregator,
        "get_cbr_warning_list_check_for_inn",
        lambda inn: {"result": "unavailable"},
    )

    result = (
        company_product_aggregator
        .enrich_company_with_cbr_warning_list(company)
    )

    assert "cbr_warning_list" not in result["sources_used"]


def test_registry_specs_are_official_and_machine_readable():
    source = build_cbr_warning_source_spec()
    dataset = build_cbr_warning_dataset_spec(
        source_id=10
    )

    assert source["source_type"] == "official"
    assert source["code"] == "cbr"
    assert dataset["code"] == "cbr_warning_list"
    assert dataset["update_mode"] == "bulk"
    assert dataset["data_format"] == "json"
    assert dataset["refresh_schedule"] == "daily"


def test_cbr_warning_partial_compiles():
    env = Environment(
        loader=FileSystemLoader("templates"),
    )

    template = env.get_template(
        "partials/cbr_warning_list.html"
    )

    assert template is not None
