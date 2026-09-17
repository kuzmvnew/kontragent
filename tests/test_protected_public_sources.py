import pytest

from app.providers.protected_public_source import parse_cbr_zsk_result_text, parse_fns_bankinform_result_text
from app.services import protected_source_session_service


def test_fns_challenge_is_never_not_found():
    result = parse_fns_bankinform_result_text("Вы превысили лимит запросов. Подтвердите, что Вы не робот.")
    assert result["result"] == "challenge_required"
    assert len(result["evidence_hash"]) == 64


def test_fns_explicit_absence_wins_over_suspension_words():
    result = parse_fns_bankinform_result_text("Действующие решения о приостановлении операций отсутствуют")
    assert result["result"] == "active_suspensions_not_found"


def test_cbr_challenge_and_timeout_cannot_be_negative():
    assert parse_cbr_zsk_result_text("SmartCaptcha: подтвердите, что вы не робот")["result"] == "challenge_required"
    assert parse_cbr_zsk_result_text("network timeout")["result"] == "unavailable"


def test_cbr_semantics_are_high_risk_presence_only():
    assert parse_cbr_zsk_result_text("Сведения об отнесении к группе высокой степени риска не найдены")["result"] == "high_risk_information_not_found"
    assert parse_cbr_zsk_result_text("Сведения об отнесении к группе высокой степени риска найдены")["result"] == "high_risk_information_found"


def test_protected_session_rejects_cross_source_result(monkeypatch):
    class Row:
        source_code = "cbr_zsk"

    class Session:
        def get(self, model, session_id): return Row()
        def rollback(self): pass
        def close(self): pass

    monkeypatch.setattr(protected_source_session_service, "get_session", lambda: Session())
    with pytest.raises(ValueError, match="Недопустимый result"):
        protected_source_session_service.update_protected_source_session(1, status="completed", result="active_suspensions_not_found")
