from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader

from app.models.legal_event import LEGAL_EVENT_TYPES
from app.services.legal_event_service import interpret_event, serialize_legal_event


@pytest.mark.parametrize("event_type", LEGAL_EVENT_TYPES)
def test_every_legal_event_type_has_explicit_semantics(event_type):
    result = interpret_event(event_type)
    assert result["label"]
    assert result["interpretation_note"]
    assert isinstance(result["bankruptcy_procedure_confirmed"], bool)


def test_intent_and_application_do_not_confirm_bankruptcy_procedure():
    for event_type in (
        "bankruptcy_intent",
        "bankruptcy_application_filed",
        "bankruptcy_application_accepted",
    ):
        assert interpret_event(event_type)["bankruptcy_procedure_confirmed"] is False


def test_liquidation_and_exclusion_are_not_bankruptcy():
    liquidation = interpret_event("liquidation_in_process")
    exclusion = interpret_event("actual_exclusion")
    assert liquidation["liquidation_event"] is True
    assert liquidation["bankruptcy_procedure_confirmed"] is False
    assert exclusion["exclusion_event"] is True
    assert exclusion["bankruptcy_procedure_confirmed"] is False


def test_observation_is_a_confirmed_procedure_stage():
    result = interpret_event("bankruptcy_observation")
    assert result["bankruptcy_procedure_confirmed"] is True
    assert result["label"] == "Суд ввёл процедуру наблюдения"


def test_unknown_event_type_fails_closed():
    with pytest.raises(ValueError, match="Unsupported legal event type"):
        interpret_event("bankrupt")


def test_serialized_event_keeps_source_dates_and_evidence():
    now = datetime(2026, 9, 17, tzinfo=timezone.utc)
    row = SimpleNamespace(
        event_type="bankruptcy_intent",
        event_date=date(2026, 9, 1),
        publication_date=date(2026, 9, 2),
        status="published",
        source_code="official_source",
        source_identifier="message-1",
        source_url="https://example.test/message-1",
        raw_event_type="Намерение кредитора",
        evidence={"matching_method": "inn_exact", "inn": "7707083893"},
        checked_at=now,
        retrieved_at=now,
    )
    result = serialize_legal_event(row)
    assert result["source_identifier"] == "message-1"
    assert result["event_date"] == date(2026, 9, 1)
    assert result["evidence"]["matching_method"] == "inn_exact"
    assert result["bankruptcy_procedure_confirmed"] is False


def test_company_template_compiles_with_legal_event_block():
    template = Environment(loader=FileSystemLoader("templates")).get_template("company.html")
    assert template is not None
