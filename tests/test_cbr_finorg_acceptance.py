from datetime import date
from types import SimpleNamespace

import httpx
import pytest
from jinja2 import Environment, FileSystemLoader

from app.providers.cbr_finorg_provider import CbrFinorgProvider, CbrFinorgProviderError
from app.services import cbr_finorg_service as service
from scripts.accept_cbr_finorg import canonical_check, normalized_text, validate_cached_case
from tests.test_cbr_finorg_source import (
    FakeClient, FakeResponse, SEARCH_NOT_FOUND_XML, make_saved_row,
)


def render(check):
    template = Environment(loader=FileSystemLoader('templates')).get_template('partials/cbr_finorg.html')
    return template.render(company={'inn': '7707083893', 'cbr_finorg_check': check})


def test_found_browser_contract_preserves_license_details():
    check = service._serialize_success(make_saved_row(), cached=True)
    html = render(check)
    assert 'data-testid="cbr-finorg"' in html
    assert 'data-result="found"' in html
    assert html.count('data-testid="cbr-finorg-license"') == 1
    assert '1481' in html and 'Универсальная лицензия' in html
    assert 'data-testid="cbr-finorg-active-count"' in html
    assert normalized_text(check['coverage_note']) in normalized_text(html)
    validate_cached_case(check, 'found')


def test_official_no_hit_browser_contract_is_deterministically_not_applicable():
    row = make_saved_row()
    row.is_participant = False
    row.licenses = []
    check = service._serialize_success(row, cached=True)
    html = render(check)
    assert 'data-result="not_applicable"' in html
    assert 'Сведения участника финансового рынка по ИНН не найдены' in html
    assert 'Отсутствие записи не означает нарушение' in html
    assert 'data-testid="cbr-finorg-license"' not in html
    validate_cached_case(check, 'not_applicable')


class ReadOnlySession:
    def close(self):
        pass


@pytest.mark.parametrize('row,reason', [
    (None, 'not_checked'),
    (SimpleNamespace(result_status='error', error_code='timeout', error_message='timeout',
                     checked_at=None, http_status=None), 'timeout'),
])
def test_cached_error_and_not_checked_are_never_not_found(monkeypatch, row, reason):
    monkeypatch.setattr(service, 'get_session', ReadOnlySession)
    monkeypatch.setattr(service, '_get_row', lambda *a, **kw: row)
    result = service.get_cached_cbr_finorg_check_for_inn('7707083893', date(2026, 9, 16))
    assert result['result'] == 'unavailable'
    assert result['checked'] is False
    assert result['reason'] == reason
    html = render(result)
    assert 'data-result="unavailable"' in html
    assert 'Сведения участника финансового рынка по ИНН не найдены' not in html
    with pytest.raises(RuntimeError):
        validate_cached_case(result, 'not_found')


@pytest.mark.parametrize('kind,status', [('timeout', None), ('network_error', None), ('service_unavailable', 503)])
def test_refresh_error_stores_unknown_not_negative(monkeypatch, kind, status):
    saved = []
    class ErrorProvider:
        def check_inn(self, inn):
            raise CbrFinorgProviderError(kind=kind, message='test failure', http_status=status)
    def save(**kwargs):
        saved.append(kwargs)
        return SimpleNamespace(checked_at=None)
    monkeypatch.setattr(service, 'ensure_cbr_finorg_dataset', lambda: None)
    monkeypatch.setattr(service, 'save_cbr_finorg_attempt', save)
    result = service.refresh_cbr_finorg_check_for_inn('7707083893', provider=ErrorProvider())
    assert result['result'] == 'unavailable' and result['checked'] is False
    assert saved[0]['is_participant'] is None
    assert saved[0]['result_status'] == 'error'


@pytest.mark.parametrize('response', [FakeResponse(b'<html>Error</html>'), FakeResponse(b'bad xml'),
                                    FakeResponse(b'error', status_code=503)])
def test_real_provider_failure_contract(response):
    with pytest.raises(CbrFinorgProviderError):
        CbrFinorgProvider(client=FakeClient([response])).check_inn('7707083893')


def test_negative_provider_needs_successful_official_response():
    client = FakeClient([FakeResponse(SEARCH_NOT_FOUND_XML)])
    result = CbrFinorgProvider(client=client).check_inn('9102309919')
    assert result['found'] is False and result['http_status'] == 200
    assert len(client.calls) == 1


def test_rejected_case_cannot_pass_from_expected_label_alone():
    for check in ({'result': 'found'}, {'checked': True, 'result': 'not_found'},
                  {'checked': True, 'result': 'found', 'matching_method': 'inn_exact',
                   'is_participant': True, 'licenses': [], 'coverage_note': 'test'}):
        with pytest.raises(RuntimeError):
            validate_cached_case(check, 'found')


def test_cache_compare_ignores_only_cache_transport_flag():
    assert canonical_check({'cached': True, 'result': 'found'}) == canonical_check({'cached': False, 'result': 'found'})
    assert canonical_check({'cached': True, 'result': 'unavailable'}) != canonical_check({'cached': False, 'result': 'not_found'})


def test_invalid_inn_is_unavailable_without_database_write():
    result = service.get_cached_cbr_finorg_check_for_inn('invalid')
    assert result['result'] == 'unavailable' and result['reason'] == 'invalid_inn'
