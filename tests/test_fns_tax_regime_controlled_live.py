from datetime import date, datetime, timezone
from types import SimpleNamespace

import pytest

from app.ingestion import fns_bulk_worker as bulk
from app.ingestion import fns_tax_regime
from app.models.worker import WorkerHandlerRegistration, WorkerPublicationState
from scripts import run_fns_tax_regime_controlled_live as operator
from workspace_app.monitoring_service import _event_type


NOW = datetime(2026, 10, 6, 8, tzinfo=timezone.utc)


def _bundle():
    releases = {}
    for name, spec in fns_tax_regime._member_specs().items():
        structure = "20230425" if name == "legal" else "20241025"
        releases[name] = bulk.FnsRelease(
            source_page_url=spec.source_page_url,
            artifact_url=(
                f"https://file.nalog.ru/opendata/{spec.source_path}/"
                f"data-20260925-structure-{structure}.zip"
            ),
            xsd_url=(
                f"https://file.nalog.ru/opendata/{spec.source_path}/"
                f"structure-{structure}.xsd"
            ),
            source_data_date=date(2026, 9, 1),
            actual_until=date(2026, 10, 25),
            discovered_at=NOW,
            provenance="Данные на 01.09.2026",
            source_updated_at=date(2026, 9, 25),
        )
    return bulk.FnsReleaseBundle(releases)


def _head(url):
    size = 62_028_770 if "snr/" in url else 293_908_849
    if url.endswith(".xsd"):
        size = 13_116 if "snr/" in url else 11_847
    return {
        "Content-Length": str(size),
        "ETag": '"test-etag"',
    }


def _datasets():
    source_urls = {
        "fns_tax_regime": fns_tax_regime.LEGAL_SOURCE_PAGE_URL,
        "fns_snr": fns_tax_regime.LEGAL_SOURCE_PAGE_URL,
        "fns_snrip": fns_tax_regime.IP_SOURCE_PAGE_URL,
    }
    return [
        SimpleNamespace(
            code=code,
            enabled=False,
            source_url=source_urls[code],
            operational_status="not_configured",
            last_success_at=None,
            last_data_date=None,
            official_actual_until=None,
        )
        for code in ("fns_tax_regime", "fns_snr", "fns_snrip")
    ]


class FakeSession:
    def __init__(self, *, approval=True, state=None):
        self.approval = (
            SimpleNamespace(approved=True, enabled=True, live_mode=False)
            if approval
            else None
        )
        self.state = state

    def get(self, model, _identity):
        if model is WorkerHandlerRegistration:
            return self.approval
        if model is WorkerPublicationState:
            return self.state
        raise AssertionError(model)

    def scalars(self, _statement):
        return _datasets()


def test_preflight_is_read_only_scoped_and_reports_new_bundle(tmp_path):
    report = operator.build_preflight_report(
        FakeSession(),
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["status"] == "READY_FOR_CONTROLLED_LIVE"
    assert report["source_id"] == "fns_tax_regime"
    assert report["member_dataset_codes"] == ["fns_snr", "fns_snrip"]
    assert report["new_release_exists"] is True
    assert report["production_mutation"] is False
    assert set(report["resources"]["artifacts"]) == {"legal", "ip"}
    assert report["resources"]["estimated_peak_staging_and_raw_bytes"] == (
        report["resources"]["download_bytes"] * 4
    )


def test_preflight_fails_closed_for_handler_or_disk_safety(tmp_path):
    report = operator.build_preflight_report(
        FakeSession(approval=False),
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=10 * 1024**3,
            used=9 * 1024**3,
            free=1 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=1),
    )

    assert report["status"] == "BLOCKED"
    assert "durable_handler_approval_missing" in report["blockers"]
    assert "production_disk_free_percent_was_lowered" in report["blockers"]
    assert "fixed_free_space_floor_not_met_after_estimated_peak" in report["blockers"]


def test_preflight_fails_closed_for_substituted_dataset_source_url(
    tmp_path,
    monkeypatch,
):
    datasets = _datasets()
    datasets[-1].source_url = "https://www.nalog.gov.ru/opendata/substituted/"
    session = FakeSession()
    monkeypatch.setattr(session, "scalars", lambda _statement: datasets)

    report = operator.build_preflight_report(
        session,
        bundle=_bundle(),
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["status"] == "BLOCKED"
    assert report["source_url_mismatches"] == ["fns_snrip"]
    assert "dataset_source_url_mismatch" in report["blockers"]


def test_preflight_recognizes_same_accepted_bundle(tmp_path):
    bundle = _bundle()
    state = SimpleNamespace(
        validation_metadata={
            "validation": {"release_identity": bundle.identity},
            "checksum": "a" * 64,
        }
    )
    report = operator.build_preflight_report(
        FakeSession(state=state),
        bundle=bundle,
        raw_root=tmp_path,
        head=_head,
        disk_usage=lambda _path: SimpleNamespace(
            total=100 * 1024**3,
            used=10 * 1024**3,
            free=90 * 1024**3,
        ),
        config=SimpleNamespace(min_disk_free_percent=10),
    )

    assert report["new_release_exists"] is False
    assert report["current_publication_identity"] == bundle.identity


def test_controlled_enqueue_requires_exact_confirmation_token(tmp_path):
    try:
        operator._enqueue(tmp_path, confirm="wrong")
    except RuntimeError as error:
        assert "confirmation token differs" in str(error)
    else:
        raise AssertionError("controlled enqueue accepted a substituted token")


def test_passport_403_fails_closed_before_session_or_enqueue(tmp_path, monkeypatch):
    def unavailable():
        raise bulk.WorkerNetworkError("FNS request failed: HTTP Error 403")

    monkeypatch.setattr(operator, "_discover_bundle", unavailable)
    monkeypatch.setattr(
        operator, "SessionLocal", lambda: pytest.fail("preflight opened a session")
    )
    monkeypatch.setattr(
        operator,
        "enqueue_bulk_release_bundle",
        lambda *_args, **_kwargs: pytest.fail("unexpected enqueue"),
    )
    with pytest.raises(bulk.WorkerNetworkError, match="403"):
        operator._enqueue(tmp_path, confirm=operator.CONFIRM_TOKEN)


def test_controlled_enqueue_creates_one_scoped_job_without_running_it(
    tmp_path,
    monkeypatch,
):
    bundle = _bundle()
    session = FakeSession()
    session.committed = False
    session.__enter__ = lambda: session
    session.__exit__ = lambda *_args: None
    session.commit = lambda: setattr(session, "committed", True)
    captured = {}

    class SessionContext:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return None

    def fake_enqueue(_session, **kwargs):
        captured.update(kwargs)
        return SimpleNamespace(
            created=True,
            job=SimpleNamespace(
                id="job-1",
                status="queued",
                job_type="fns_tax_regime_release",
            ),
        )

    monkeypatch.setattr(
        operator,
        "_preflight",
        lambda _raw_root: ({"status": "READY_FOR_CONTROLLED_LIVE"}, bundle),
    )
    monkeypatch.setattr(operator, "SessionLocal", SessionContext)
    monkeypatch.setattr(operator, "enqueue_bulk_release_bundle", fake_enqueue)

    result = operator._enqueue(tmp_path, confirm=operator.CONFIRM_TOKEN)

    assert session.committed is True
    assert result["status"] == "ENQUEUED"
    assert result["production_scheduler_enabled"] is False
    assert result["check_only"] is False
    assert "--allow-source fns_tax_regime" in result["worker_command"]
    assert "--work-budget 1" in result["worker_command"]
    assert captured["bundle"] is bundle
    assert captured["extra_schedule_metadata"] == {
        "controlled_live": True,
        "controlled_live_scope": "fns_tax_regime",
        "operator": "c6-home-controlled-live-v1",
    }


def test_tax_regime_semantic_coordinate_uses_generic_monitoring_event():
    assert _event_type("tax", "regime") == "GENERIC_FACT_CHANGED"


@pytest.mark.parametrize("member", ("legal", "ip"))
@pytest.mark.parametrize("path_kind", (
    "literal_traversal", "encoded_traversal", "nested_traversal",
    "encoded_separator", "malformed_percent", "wrong_sibling", "backslash",
))
def test_discovery_rejects_ambiguous_official_links(member, path_kind):
    spec = fns_tax_regime._member_specs()[member]
    sibling = fns_tax_regime._member_specs()["ip" if member == "legal" else "legal"]
    base = f"/opendata/{spec.source_path}/"
    name = "data-20260925-structure-20230425.zip"
    path = {
        "literal_traversal": base + "../" + sibling.source_path + "/" + name,
        "encoded_traversal": base + "%2e%2e/" + sibling.source_path + "/" + name,
        "nested_traversal": base + "%252e%252e/" + sibling.source_path + "/" + name,
        "encoded_separator": base + "%2F../" + name,
        "malformed_percent": base + "data-20260925-structure-20230425%GG.zip",
        "wrong_sibling": f"/opendata/{sibling.source_path}/{name}",
        "backslash": base + "..\\" + sibling.source_path + "/" + name,
    }[path_kind]
    html = f'''
      <td property="dc:identifier">{spec.source_path}</td>
      <a href="https://file.nalog.ru{path}">ZIP</a>
      <a href="https://file.nalog.ru{base}structure-20230425.xsd">XSD</a>
      <td property="dc:modified" content="25.09.2026">25.09.2026</td>
      <td property="dc:provenance">Данные на 01.09.2026</td>
    '''.encode()
    with pytest.raises((bulk.InvalidDataError, bulk.SchemaMismatchError)):
        bulk.discover_fns_release(spec, now=NOW, fetch=lambda _url: (html, {}))


@pytest.mark.parametrize("member", ("legal", "ip"))
@pytest.mark.parametrize("path_kind", (
    "literal_traversal", "encoded_traversal", "nested_traversal",
    "wrong_sibling", "xsd_sibling", "encoded_separator", "malformed_percent",
))
def test_staging_rejects_ambiguous_links_before_download(
    member, path_kind, tmp_path, monkeypatch
):
    spec = fns_tax_regime._member_specs()[member]
    sibling = fns_tax_regime._member_specs()["ip" if member == "legal" else "legal"]
    release = _bundle().releases[member]
    base = f"https://file.nalog.ru/opendata/{spec.source_path}/"
    name = release.artifact_url.rsplit("/", 1)[-1]
    artifact_url = {
        "literal_traversal": base + "../" + sibling.source_path + "/" + name,
        "encoded_traversal": base + "%2e%2e/" + sibling.source_path + "/" + name,
        "nested_traversal": base + "%252e%252e/" + sibling.source_path + "/" + name,
        "wrong_sibling": f"https://file.nalog.ru/opendata/{sibling.source_path}/{name}",
        "encoded_separator": base + "%2F../" + name,
        "malformed_percent": base + name.replace(".zip", "%GG.zip"),
    }.get(path_kind, release.artifact_url)
    xsd_url = (
        f"https://file.nalog.ru/opendata/{sibling.source_path}/"
        + release.xsd_url.rsplit("/", 1)[-1]
        if path_kind == "xsd_sibling" else release.xsd_url
    )
    calls = []
    monkeypatch.setattr(
        bulk, "_download_temp", lambda *args: calls.append(args)
    )
    invalid = bulk.FnsRelease(
        **{**release.__dict__, "artifact_url": artifact_url, "xsd_url": xsd_url}
    )
    raw_root = tmp_path / "raw"
    with pytest.raises(bulk.InvalidDataError):
        bulk.stage_release(spec, invalid, raw_root=raw_root)
    assert calls == []
    assert not raw_root.exists()


def test_current_legal_and_ip_urls_pass_canonical_validation():
    for member, release in _bundle().releases.items():
        spec = fns_tax_regime._member_specs()[member]
        assert bulk.validate_official_release_url(
            spec, release.artifact_url, artifact=True
        )[1] == bulk.validate_official_release_url(
            spec, release.xsd_url, artifact=False
        )[1]


@pytest.mark.parametrize("bad", ("%", "%0", "%GG", "%2Z"))
def test_canonical_url_rejects_malformed_percent_at_each_decode_layer(bad):
    spec = fns_tax_regime._member_specs()["legal"]
    url = (
        "https://file.nalog.ru/opendata/7707329152-snr/"
        f"data-20260925-structure-20230425{bad}.zip"
    )
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(spec, url, artifact=True)


@pytest.mark.parametrize("url", (
    "https://user@file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip",
    "https://file.nalog.ru:443/opendata/7707329152-snr/data-20260925-structure-20230425.zip",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip?download=1",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip?",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip#fragment",
    "https://file.nalog.ru/opendata/7707329152-snr/data-20260925-structure-20230425.zip#",
))
def test_canonical_url_rejects_authority_query_and_fragment(url):
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(
            fns_tax_regime._member_specs()["legal"], url, artifact=True
        )


@pytest.mark.parametrize("control", ("\r", "\n", "\t", "\x00", "\x7f"))
def test_canonical_url_rejects_raw_controls_before_urlparse(control):
    url = (
        "https://file.nalog.ru/opendata/7707329152-snr/"
        f"data-20260925-structure-20230425{control}.zip"
    )
    with pytest.raises(bulk.InvalidDataError):
        bulk.validate_official_release_url(
            fns_tax_regime._member_specs()["legal"], url, artifact=True
        )
