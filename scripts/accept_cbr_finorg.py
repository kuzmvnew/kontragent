"""W1-002 acceptance. Only --live writes two on-demand checks; never imports Master.

CI evidence is explicitly distinct from user-local/Mac acceptance. No mock
provider, fake company insertion or schema changes are used by this script.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import platform
import re
import socket
import subprocess
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx
from sqlalchemy import func, select, text

from app.database.postgres import get_session
from app.models.company import Company
from app.models.cbr_finorg import CbrFinorgCheck as Check
from app.providers.cbr_finorg_provider import CbrFinorgProvider, SERVICE_URL
from app.services.cbr_finorg_service import (
    get_cached_cbr_finorg_check_for_inn,
    refresh_cbr_finorg_check_for_inn,
)

ROOT = Path(__file__).resolve().parents[1]
FOUND_INN = '9706063520'
ABSENT_INN = '9102309919'


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


def normalized_text(value):
    return ' '.join(str(value or '').split())


def canonical_check(check):
    return {key: value for key, value in check.items() if key != 'cached'}


def validate_cached_case(check, expected):
    require(check.get('checked') is True, 'Source was not successfully checked')
    require(check.get('result') == expected, f'Expected {expected}; got {check.get("result")}')
    require(check.get('matching_method') == 'inn_exact', 'Exact-INN proof missing')
    require(check.get('is_participant') is (expected == 'found'), 'Inconsistent participant state')
    if expected == 'found':
        require(bool(check.get('licenses')), 'Real positive case must expose licenses/rights')
    else:
        require(not check.get('licenses'), 'Negative case contains licenses')
    require(bool(check.get('coverage_note')), 'Interpretation limitation missing')


def audit_case(inn, expected, request_date, refreshed=None):
    """A new DB session, then two independent service reads; never create companies."""
    with get_session() as session:
        company = session.scalar(select(Company).where(Company.inn == inn))
        require(company is not None, f'Company {inn} is missing from local Master Registry')
        rows = session.scalars(select(Check).where(
            Check.inn == inn, Check.request_date == request_date)).all()
        require(len(rows) == 1, f'Expected exactly one dated PostgreSQL check for {inn}')
        row = rows[0]
        require(row.result_status == 'success' and row.http_status == 200,
                f'{inn}: cached attempt is not an official successful response')
        require(row.is_participant is (expected == 'found'), 'PostgreSQL state mismatch')
        raw = row.raw_payload or {}
        require(raw.get('found') is (expected == 'found') and raw.get('http_status') == 200,
                'Stored provider evidence missing or inconsistent')
        if expected == 'found':
            require((raw.get('participant') or {}).get('inn') == inn,
                    'Official participant INN differs from requested INN')
            require((raw.get('participant') or {}).get('licenses') == row.licenses,
                    'Persisted licenses differ from provider response')
        evidence = {'inn': inn, 'company_name': company.name, 'row_id': row.id,
                    'request_date': str(row.request_date), 'checked_at': str(row.checked_at),
                    'official_http_status': row.http_status, 'result': expected,
                    'license_count': len(row.licenses or [])}
    first = get_cached_cbr_finorg_check_for_inn(inn, request_date=request_date)
    second = get_cached_cbr_finorg_check_for_inn(inn, request_date=request_date)
    validate_cached_case(first, expected)
    require(first.get('cached') is True and second.get('cached') is True and first == second,
            'Independent PostgreSQL reread/cache mismatch')
    if refreshed is not None:
        require(canonical_check(refreshed) == canonical_check(first), 'Live/cache mismatch')
    evidence.update({'cached': True, 'reread_pass': True,
                     'active_license_count': first['active_license_count']})
    return evidence, first


def audit_coverage(request_date):
    """Cache coverage, not an invented full-registry size or bulk import."""
    with get_session() as session:
        master = session.scalar(select(func.count()).select_from(Company))
        total = session.scalar(select(func.count()).select_from(Check))
        success = session.scalar(select(func.count()).select_from(Check).where(Check.result_status == 'success'))
        errors = session.scalar(select(func.count()).select_from(Check).where(Check.result_status == 'error'))
        unique = session.scalar(select(func.count(func.distinct(Check.inn))).where(Check.result_status == 'success'))
        dates = session.execute(select(func.min(Check.request_date), func.max(Check.request_date),
                                       func.min(Check.checked_at), func.max(Check.checked_at))).one()
        current = (Check.request_date == request_date, Check.result_status == 'success', Check.http_status == 200)
        current_unique = session.scalar(select(func.count(func.distinct(Check.inn))).where(*current))
        linked = session.scalar(select(func.count(func.distinct(Company.id))).select_from(Company)
                                .join(Check, Check.inn == Company.inn).where(*current))
        found = session.scalar(select(func.count()).select_from(Check).where(*current, Check.is_participant.is_(True)))
        absent = session.scalar(select(func.count()).select_from(Check).where(*current, Check.is_participant.is_(False)))
        require(master > 0 and linked >= 2 and found >= 1 and absent >= 1, 'Coverage evidence is incomplete')
        return {'mode': 'ON_DEMAND_WITH_DATED_CACHE', 'cache_rows': total,
                'successful_cache_rows': success, 'error_cache_rows': errors,
                'successful_unique_inn_all_dates': unique,
                'request_date_min': str(dates[0]), 'request_date_max': str(dates[1]),
                'checked_at_min': str(dates[2]), 'checked_at_max': str(dates[3]),
                'acceptance_request_date': str(request_date), 'current_successful_unique_inn': current_unique,
                'current_found_rows': found, 'current_not_found_rows': absent,
                'master_companies': master, 'current_checked_master_companies': linked,
                'current_unchecked_master_companies': master - linked,
                'current_checked_master_percent': round(100 * linked / master, 6),
                'full_cbr_registry_size': 'N/A: not supplied by these on-demand methods',
                'full_registry_import': 'N/A: no bulk crawl',
                'date_semantics': 'request_date is the check date, not an official full-registry snapshot date'}


class EvidenceClient:
    """Real HTTP only; bounded to three SOAP calls, two seconds apart."""
    def __init__(self, output):
        self.output = output
        self.calls = []
        self.previous = 0.0
        self.client = httpx.Client(timeout=45, follow_redirects=True)

    def post(self, url, content, headers):
        require(url == SERVICE_URL and len(self.calls) < 3, 'Unexpected source request or request budget exceeded')
        time.sleep(max(0, 2 - (time.monotonic() - self.previous)))
        self.previous = time.monotonic()
        response = self.client.post(url, content=content, headers=headers)
        require(urlparse(str(response.url)).hostname in {'www.cbr.ru', 'cbr.ru'}, 'Unexpected official redirect')
        name = f'official-response-{len(self.calls) + 1}.xml'
        (self.output / name).write_bytes(response.content)
        self.calls.append({'url': str(response.url), 'soap_action': headers.get('SOAPAction'),
                           'http_status': response.status_code, 'bytes': len(response.content),
                           'sha256': hashlib.sha256(response.content).hexdigest(), 'file': name,
                           'received_at': datetime.now(timezone.utc).isoformat()})
        return response


def browser_check(cases, output):
    from playwright.sync_api import sync_playwright
    with socket.socket() as sock:
        sock.bind(('127.0.0.1', 0))
        port = sock.getsockname()[1]
    origin = f'http://127.0.0.1:{port}'
    results = []
    with (output / 'server.log').open('w') as log:
        proc = subprocess.Popen([sys.executable, '-m', 'uvicorn', 'main:app', '--host', '127.0.0.1',
                                 '--port', str(port)], cwd=ROOT, stdout=log, stderr=log)
        try:
            for _ in range(150):
                require(proc.poll() is None, 'FastAPI failed to start; see server.log')
                try:
                    if httpx.get(origin + '/openapi.json', timeout=1).status_code == 200:
                        break
                except httpx.RequestError:
                    pass
                time.sleep(.2)
            else:
                raise RuntimeError('FastAPI readiness timeout')
            with sync_playwright() as pw:
                browser = pw.chromium.launch()
                try:
                    for evidence, check in cases:
                        page = browser.new_page(viewport={'width': 1440, 'height': 1000})
                        errors = []
                        page.on('pageerror', lambda error, bucket=errors: bucket.append(str(error)))
                        response = page.goto(f'{origin}/company/{evidence["inn"]}', wait_until='networkidle', timeout=90000)
                        block = page.get_by_test_id('cbr-finorg')
                        block.wait_for(state='visible', timeout=15000)
                        actual = block.get_attribute('data-result')
                        visible = normalized_text(block.inner_text())
                        require(response is not None and response.status == 200, 'Company card HTTP is not 200')
                        require(actual == evidence['result'] and not errors, 'Browser state or JavaScript error')
                        require('Банк России' in visible and 'точн' in visible, 'Source/matching explanation missing')
                        for note in ('interpretation_note', 'coverage_note'):
                            require(normalized_text(check[note]) in visible, 'Visible interpretation note missing')
                        if actual == 'found':
                            require('Сведения участника финансового рынка найдены' in visible, 'Positive text missing')
                            require(block.get_by_test_id('cbr-finorg-license').count() == len(check['licenses']),
                                    'Rendered license count differs from PostgreSQL')
                            require(block.get_by_test_id('cbr-finorg-active-count').inner_text().strip() == str(check['active_license_count']),
                                    'Rendered active count differs from service')
                            for item in check['licenses']:
                                for key in ('number', 'activity', 'name'):
                                    if item.get(key):
                                        require(normalized_text(item[key]) in visible, f'License {key} not visible')
                        else:
                            require('Сведения участника финансового рынка по ИНН не найдены' in visible, 'Negative text missing')
                            require(block.get_by_test_id('cbr-finorg-license').count() == 0, 'Negative card shows licenses')
                        screenshot = output / f'{actual}.png'
                        block.screenshot(path=str(screenshot))
                        results.append({'inn': evidence['inn'], 'result': actual, 'http_status': response.status,
                                        'browser': 'Chromium', 'browser_version': browser.version,
                                        'page_errors': list(errors), 'visible_text': visible,
                                        'screenshot': str(screenshot)})
                        page.close()
                finally:
                    browser.close()
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill(); proc.wait()
    return results


def main():
    parser = argparse.ArgumentParser(description='W1-002 real six-gate acceptance')
    parser.add_argument('--tests', action='store_true')
    parser.add_argument('--live', action='store_true', help='Refresh exactly two real INNs; at most three SOAP requests')
    parser.add_argument('--browser', action='store_true')
    parser.add_argument('--environment', choices=['user-local', 'ci-disposable'], default='user-local')
    parser.add_argument('--output-dir', type=Path)
    args = parser.parse_args()
    now = datetime.now(timezone.utc)
    output = args.output_dir or ROOT / 'data/acceptance/w1-002' / now.strftime('%Y%m%dT%H%M%S%fZ')
    output.mkdir(parents=True, exist_ok=True)
    gates = {str(n): 'NOT CONFIRMED' for n in range(1, 7)}
    report = {'source': 'W1-002', 'environment': args.environment, 'platform': platform.system(),
              'executed_at': now.isoformat(), 'gates': gates, 'auto_update': 'NOT_CONFIGURED',
              'accepted': False, 'user_mac_accepted': False}
    client = None
    try:
        with get_session() as session:
            require(session.bind.dialect.name == 'postgresql', 'Acceptance requires PostgreSQL')
            db = session.scalar(text('SELECT current_database()'))
            report['database'] = db
            if args.environment == 'user-local':
                require(not os.getenv('GITHUB_ACTIONS') and db == 'kontragent', 'Use local kontragent DB, not CI')
            else:
                require(os.getenv('GITHUB_ACTIONS') == 'true' and db == 'test', 'CI requires disposable test DB')
            report['alembic'] = session.scalars(text('SELECT version_num FROM alembic_version')).all()
            for inn in (FOUND_INN, ABSENT_INN):
                require(session.scalar(select(Company.id).where(Company.inn == inn)) is not None,
                        f'{inn} missing from Master Registry; no fabricated company will be inserted')
        report['git_head'] = subprocess.check_output(['git', 'rev-parse', 'HEAD'], cwd=ROOT, text=True).strip()
        report['git_status'] = subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip()
        if args.tests:
            print('W1-002: полный набор автоматических тестов', flush=True)
            test = subprocess.run([sys.executable, '-m', 'pytest', 'tests', '-q'], cwd=ROOT,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300)
            (output / 'tests.log').write_text(test.stdout)
            report['tests'] = {'returncode': test.returncode, 'summary': test.stdout.strip().splitlines()[-1:]}
            require(test.returncode == 0, 'Tests failed; no live checks started')
            require(' passed' in test.stdout, 'No passed-test evidence')
            gates['1'] = 'PASS'
        request_date = date.today()
        client = EvidenceClient(output) if args.live else None
        cases = []
        for inn, expected in ((FOUND_INN, 'found'), (ABSENT_INN, 'not_found')):
            refreshed = None
            if client:
                print(f'W1-002: официальный запрос {inn}, затем независимое чтение PostgreSQL', flush=True)
                refreshed = refresh_cbr_finorg_check_for_inn(inn, request_date=request_date,
                                    provider=CbrFinorgProvider(client=client))
                validate_cached_case(refreshed, expected)
            cases.append(audit_case(inn, expected, request_date, refreshed))
        report['cases'] = [evidence for evidence, _ in cases]
        gates['3'] = 'PASS'
        # Existing official successful cache is valid dated evidence; --live also
        # preserves real SOAP bodies and hashes for this run.
        gates['2'] = 'PASS'
        report['state_semantics'] = {
            'found': 'exact-INN participant found; not a universal license/compliance verdict',
            'not_found': 'no exact-INN participant in successful dated CBR response; not safe/unsafe',
            'not_applicable': 'N/A for valid company/IP INNs; business-specific license applicability not inferred',
            'unavailable': 'not checked or source failure; never proof of absence',
            'failure_checks': 'automated provider/cache regression, not induced against user DB'}
        gates['4'] = 'PASS' if gates['1'] == 'PASS' else 'NOT CONFIRMED'
        report['coverage'] = audit_coverage(request_date)
        gates['6'] = 'PASS'
        if args.browser:
            require(date.today() == request_date, 'Local date changed; cards use current-day cache')
            print('W1-002: реальные карточки found/not_found в Chromium', flush=True)
            report['browser'] = browser_check(cases, output)
            require(date.today() == request_date, 'Local date changed during browser acceptance')
            gates['5'] = 'PASS'
        report['accepted'] = all(value == 'PASS' for value in gates.values())
        report['user_mac_accepted'] = (report['accepted'] and args.environment == 'user-local'
                                       and platform.system() == 'Darwin')
    except Exception as error:
        report['error_type'] = type(error).__name__
        report['error'] = str(error)[:1500]
    finally:
        if client:
            report['official_http_calls'] = client.calls
            client.client.close()
        report['finished_at'] = datetime.now(timezone.utc).isoformat()
        path = output / 'report.json'
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2, default=str) + '\n')
        print(json.dumps(report, ensure_ascii=False, indent=2, default=str))
        print('Report:', path)
        label = 'W1-002 SIX GATES' if args.environment == 'user-local' else 'W1-002 CI SIX GATES (NOT USER MAC)'
        print(label + ':', 'PASS' if report['accepted'] else 'NOT ACCEPTED')
    return 0 if report['accepted'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
