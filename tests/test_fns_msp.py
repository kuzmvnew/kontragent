from datetime import date
from types import SimpleNamespace
from xml.etree import ElementTree as ET

from jinja2 import (
    Environment,
    FileSystemLoader,
)

from app.aggregators import company_aggregator
from app.ingestion.fns_msp import (
    parse_date,
    parse_msp_document,
    safe_int,
)
from app.services import msp_service


class FakeResult:
    def __init__(
        self,
        row,
    ):
        self.row = row

    def first(
        self,
    ):
        return self.row


class FakeSession:
    def __init__(
        self,
        row,
    ):
        self.row = row
        self.closed = False

    def execute(
        self,
        statement,
    ):
        return FakeResult(
            self.row
        )

    def close(
        self,
    ):
        self.closed = True


def test_parse_date_supported_formats():
    assert parse_date(
        "10.09.2026"
    ) == date(
        2026,
        9,
        10,
    )

    assert parse_date(
        "2026-09-10"
    ) == date(
        2026,
        9,
        10,
    )


def test_parse_date_invalid_values():
    assert parse_date(
        None
    ) is None

    assert parse_date(
        ""
    ) is None

    assert parse_date(
        "wrong-date"
    ) is None


def test_safe_int():
    assert safe_int(
        "25"
    ) == 25

    assert safe_int(
        0
    ) == 0

    assert safe_int(
        None
    ) is None

    assert safe_int(
        "abc"
    ) is None


def test_parse_msp_legal_entity():
    document = ET.fromstring(
        """
        <Документ
            ИдДок="MSP-LEGAL-1"
            ДатаСост="10.09.2026"
            ДатаВклМСП="01.08.2024"
            ВидСубМСП="1"
            КатСубМСП="2"
            ПризНовМСП="2"
            СведСоцПред="2"
            ССЧР="15"
        >
            <ОргВклМСП
                ИННЮЛ="7707083893"
                ОГРН="1027700132195"
                НаимОрг="ОБЩЕСТВО ТЕСТ"
                НаимОргСокр="ООО ТЕСТ"
            />

            <СведМН
                КодРегион="77"
            />

            <СвОКВЭД>
                <СвОКВЭДОсн
                    КодОКВЭД="62.01"
                    НаимОКВЭД="Разработка ПО"
                />
            </СвОКВЭД>
        </Документ>
        """
    )

    result = parse_msp_document(
        document
    )

    assert result is not None

    assert result[
        "inn"
    ] == "7707083893"

    assert result[
        "entity_type"
    ] == "legal"

    assert result[
        "name"
    ] == "ООО ТЕСТ"

    assert result[
        "category_code"
    ] == "2"

    assert result[
        "employee_count"
    ] == 15

    assert result[
        "data_date"
    ] == date(
        2026,
        9,
        10,
    )


def test_parse_msp_individual_entrepreneur():
    document = ET.fromstring(
        """
        <Документ
            ИдДок="MSP-IP-1"
            ДатаСост="10.09.2026"
            ДатаВклМСП="01.08.2025"
            ВидСубМСП="2"
            КатСубМСП="1"
        >
            <ИПВклМСП
                ИННФЛ="667100000001"
                ОГРНИП="326665800000001"
            >
                <ФИОИП
                    Фамилия="ИВАНОВ"
                    Имя="ИВАН"
                    Отчество="ИВАНОВИЧ"
                />
            </ИПВклМСП>

            <СведМН
                КодРегион="66"
            />
        </Документ>
        """
    )

    result = parse_msp_document(
        document
    )

    assert result is not None

    assert result[
        "inn"
    ] == "667100000001"

    assert result[
        "entity_type"
    ] == (
        "individual_entrepreneur"
    )

    assert result[
        "name"
    ] == (
        "ИП ИВАНОВ ИВАН ИВАНОВИЧ"
    )


def test_parse_msp_rejects_invalid_inn():
    document = ET.fromstring(
        """
        <Документ
            ДатаСост="10.09.2026"
        >
            <ОргВклМСП
                ИННЮЛ="123"
                НаимОрг="ТЕСТ"
            />
        </Документ>
        """
    )

    assert parse_msp_document(
        document
    ) is None


def test_msp_category_names():
    assert (
        msp_service.get_msp_category_name(
            "1"
        )
        == "Микропредприятие"
    )

    assert (
        msp_service.get_msp_category_name(
            "2"
        )
        == "Малое предприятие"
    )

    assert (
        msp_service.get_msp_category_name(
            "3"
        )
        == "Среднее предприятие"
    )

    assert (
        msp_service.get_msp_category_name(
            "9"
        )
        == "Код 9"
    )


def test_msp_service_returns_profile(
    monkeypatch,
):
    profile = SimpleNamespace(
        data_date=date(
            2026,
            9,
            10,
        ),
        inclusion_date=date(
            2024,
            8,
            1,
        ),
        subject_type_code="1",
        category_code="2",
        is_new_code="2",
        social_enterprise_code="1",
        employee_count=21,
        source_document_id="MSP-DOC-1",
    )

    dataset = SimpleNamespace(
        id=8,
        code="fns_msp",
        priority=10,
    )

    session = FakeSession(
        (
            profile,
            dataset,
        )
    )

    monkeypatch.setattr(
        msp_service,
        "get_session",
        lambda: session,
    )

    result = (
        msp_service
        .get_msp_profile_for_company(
            company_id=100,
        )
    )

    assert result[
        "dataset_code"
    ] == "fns_msp"

    assert result[
        "category_name"
    ] == "Малое предприятие"

    assert result[
        "employee_count"
    ] == 21

    assert result[
        "inclusion_date"
    ] == date(
        2024,
        8,
        1,
    )

    assert session.closed is True


def test_msp_service_returns_none_when_missing(
    monkeypatch,
):
    session = FakeSession(
        None
    )

    monkeypatch.setattr(
        msp_service,
        "get_session",
        lambda: session,
    )

    result = (
        msp_service
        .get_msp_profile_for_company(
            company_id=999,
        )
    )

    assert result is None
    assert session.closed is True


def test_structured_domain_data_contains_msp(
    monkeypatch,
):
    msp_profile = {
        "dataset_code": "fns_msp",
        "category_name": (
            "Микропредприятие"
        ),
    }

    monkeypatch.setattr(
        company_aggregator,
        "get_msp_profile_for_company",
        lambda company_id: (
            msp_profile
        ),
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_regime_profile_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_revenue_expense_check_for_company",
        lambda company_id: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_debt_check_for_company",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_latest_tax_debt_for_company",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_debt_history",
        lambda **kwargs: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_offence_check_for_company",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_offence_history",
        lambda **kwargs: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_latest_tax_payment_for_company",
        lambda **kwargs: None,
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_tax_payment_history",
        lambda **kwargs: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_legal_events_for_company",
        lambda **kwargs: [],
    )

    result = (
        company_aggregator
        .load_structured_domain_data(
            company_id=10,
        )
    )

    assert (
        result[
            "msp_profile"
        ]
        == msp_profile
    )


def test_aggregate_company_adds_msp_source(
    monkeypatch,
):
    monkeypatch.setattr(
        company_aggregator,
        "get_source_registry",
        lambda: {},
    )

    monkeypatch.setattr(
        company_aggregator,
        "get_company_from_database",
        lambda inn: {
            "id": 10,
            "inn": inn,
            "source": "excel_import",
        },
    )

    monkeypatch.setattr(
        company_aggregator,
        "normalize_base_company",
        lambda company: {
            "inn": company[
                "inn"
            ],
        },
    )

    monkeypatch.setattr(
        company_aggregator,
        "load_cached_source_candidates",
        lambda company_id, registry: [],
    )

    monkeypatch.setattr(
        company_aggregator,
        "load_domain_candidates",
        lambda company_id: [],
    )

    msp_profile = {
        "dataset_code": "fns_msp",
        "category_name": (
            "Микропредприятие"
        ),
    }

    monkeypatch.setattr(
        company_aggregator,
        "load_structured_domain_data",
        lambda company_id: {
            "msp_profile": (
                msp_profile
            ),
            "tax_regime_profile": None,
            "revenue_expense_check": None,
            "tax_debt": None,
            "tax_debt_check": None,
            "tax_debt_history": [],
            "tax_offence": None,
            "tax_offence_check": None,
            "tax_offence_history": [],
            "tax_payment": None,
            "tax_payment_check": None,
            "tax_payment_history": [],
        },
    )

    result = (
        company_aggregator
        .aggregate_company(
            inn="7707083893",
        )
    )

    assert (
        result[
            "msp_profile"
        ]
        == msp_profile
    )

    assert (
        "fns_msp"
        in result[
            "sources_used"
        ]
    )


def test_msp_templates_compile():
    environment = Environment(
        loader=FileSystemLoader(
            "templates"
        )
    )

    environment.get_template(
        "company.html"
    )

    environment.get_template(
        "partials/msp.html"
    )
