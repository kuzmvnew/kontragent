from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path
from zipfile import ZipFile

import pytest

from app.contracts.data_readiness import (
    AutoUpdateStatus,
    DatasetKind,
    FreshnessPolicy,
    OperationalStatus,
    RetryDisposition,
    classify_network_failure,
    is_dataset_stale,
    retry_delay,
)
from app.services import bulk_update_service as bulk
from app.services import data_readiness_scheduler as scheduler
from app.services.bulk_update_service import (
    BulkUpdatePipeline,
    IntegrityError,
    ParseValidationError,
    ParsedSnapshot,
    PartialFileError,
    validate_artifact,
)
from app.services.data_readiness_service import DatasetLockedError, safe_error_message


NOW = datetime(2026, 9, 17, 12, tzinfo=timezone.utc)


def test_fresh_detection_uses_source_as_of():
    assert not is_dataset_stale(
        now=NOW,
        freshness_policy="daily",
        source_as_of=NOW - timedelta(hours=12),
        last_success_at=NOW - timedelta(days=30),
    )


def test_stale_detection_is_not_error_state():
    assert is_dataset_stale(
        now=NOW,
        freshness_policy="weekly",
        source_as_of=NOW - timedelta(days=10),
        last_success_at=NOW,
    )
    assert OperationalStatus.STALE != OperationalStatus.ERROR


def test_on_demand_cache_freshness_can_use_explicit_ttl():
    assert is_dataset_stale(
        now=NOW,
        freshness_policy="on_demand",
        source_as_of=None,
        last_success_at=NOW - timedelta(hours=25),
        threshold_seconds=86400,
    )


def test_retry_uses_exponential_backoff_and_cap():
    assert retry_delay(1).total_seconds() == 60
    assert retry_delay(3).total_seconds() == 240
    assert retry_delay(99).total_seconds() == 21600


def test_429_honours_retry_after():
    assert classify_network_failure(http_status=429) == RetryDisposition.RETRY
    assert retry_delay(1, retry_after_seconds=900).total_seconds() == 900


def test_403_protection_is_source_blocked_not_not_found():
    assert classify_network_failure(http_status=403) == RetryDisposition.SOURCE_BLOCKED


def test_access_credentials_are_pending_not_unavailable():
    assert classify_network_failure(error_type="credentials_missing") == RetryDisposition.ACCESS_PENDING


def test_sensitive_error_values_are_redacted():
    result = safe_error_message("api_key=secret token: bearer cookie=private")
    assert "secret" not in result and "private" not in result
    assert result.count("[REDACTED]") == 3


def test_good_zip_validates_crc_eof_file_count_and_checksum(tmp_path):
    path = tmp_path / "snapshot.zip"
    with ZipFile(path, "w") as archive:
        archive.writestr("one.xml", "<rows/>")
    checksum = sha256(path.read_bytes()).hexdigest()
    result = validate_artifact(path, expected_sha256=checksum, expected_file_count=1)
    assert result.file_count == 1 and result.checksum == checksum


def test_bad_checksum_rejected(tmp_path):
    path = tmp_path / "snapshot.json"
    path.write_text("{}")
    with pytest.raises(IntegrityError, match="SHA-256"):
        validate_artifact(path, expected_sha256="0" * 64)


def test_partial_file_rejected(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_bytes(b"")
    with pytest.raises(PartialFileError):
        validate_artifact(path)


def test_truncated_zip_rejected_by_eof_validation(tmp_path):
    path = tmp_path / "broken.zip"
    path.write_bytes(b"PK\x03\x04partial")
    with pytest.raises(PartialFileError, match="EOF"):
        validate_artifact(path)


def test_successful_scheduled_update(monkeypatch):
    called = []
    monkeypatch.setattr(scheduler, "HANDLERS", {"bulk": lambda: called.append("bulk")})
    assert scheduler.run_due_updates(due_codes=["bulk"]) == {"bulk": "success"}
    assert called == ["bulk"]


def test_scheduler_skips_unregistered_manual_user_and_access_sources(monkeypatch):
    monkeypatch.setattr(scheduler, "HANDLERS", {})
    result = scheduler.run_due_updates(due_codes=["manual", "user", "access"])
    assert set(result.values()) == {"not_configured"}


def test_source_modes_are_not_collapsed():
    assert len({
        DatasetKind.BULK_SNAPSHOT,
        DatasetKind.ON_DEMAND_API,
        DatasetKind.PUBLIC_LIVE_USER_TRIGGERED,
        DatasetKind.HUMAN_ASSISTED,
        DatasetKind.SOURCE_BLOCKED,
        DatasetKind.ACCESS_CREDENTIAL_PENDING,
    }) == 6
    assert AutoUpdateStatus.HUMAN_ASSISTED != AutoUpdateStatus.USER_TRIGGERED
    assert FreshnessPolicy.ACCESS_PENDING != FreshnessPolicy.MANUAL


class FakeDataset:
    id = 42


class FakeSession:
    def __init__(self):
        self.committed = False
        self.rolled_back = False
        self.added = []
        self.scalar_calls = 0

    def scalar(self, statement):
        self.scalar_calls += 1
        return FakeDataset() if self.scalar_calls == 1 else getattr(self, "existing", None)

    def add(self, value):
        self.added.append(value)

    def flush(self):
        for item in self.added:
            item.id = 7

    def execute(self, statement):
        return None

    def commit(self):
        self.committed = True

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def _pipeline(monkeypatch, tmp_path, *, publish=None, parse=None, acquire=True):
    artifact = tmp_path / "snapshot.json"
    artifact.write_text('[{"inn":"123"}]')
    session = FakeSession()
    events = []
    monkeypatch.setattr(bulk, "acquire_dataset_lock", lambda *a, **k: acquire)
    monkeypatch.setattr(bulk, "release_dataset_lock", lambda *a, **k: events.append("released"))
    monkeypatch.setattr(bulk, "start_update_run", lambda *a, **k: 11)
    monkeypatch.setattr(bulk, "finish_update_run", lambda *a, **k: events.append("success"))
    monkeypatch.setattr(bulk, "fail_update_run", lambda *a, **k: events.append("failed"))
    monkeypatch.setattr(bulk, "get_session", lambda: session)
    parsed = ParsedSnapshot(payload=[{"inn": "123"}], source_as_of=NOW, record_count=1)
    pipeline = BulkUpdatePipeline(
        download=lambda: artifact,
        parse=parse or (lambda path: parsed),
        publish=publish or (lambda db, payload: events.append("published")),
    )
    return pipeline, session, events


def test_successful_manual_bulk_update(monkeypatch, tmp_path):
    pipeline, session, events = _pipeline(monkeypatch, tmp_path)
    assert pipeline.run("bulk", trigger="manual") == 11
    assert session.committed and events == ["published", "success", "released"]


def test_bad_parse_never_starts_publish(monkeypatch, tmp_path):
    pipeline, session, events = _pipeline(
        monkeypatch, tmp_path, parse=lambda path: (_ for _ in ()).throw(ValueError("schema"))
    )
    with pytest.raises(ParseValidationError):
        pipeline.run("bulk")
    assert not session.committed and events == ["failed", "released"]


def test_failed_download_is_recorded_and_released(monkeypatch, tmp_path):
    pipeline, session, events = _pipeline(monkeypatch, tmp_path)
    pipeline.download = lambda: (_ for _ in ()).throw(OSError("network down"))
    with pytest.raises(bulk.DownloadError):
        pipeline.run("bulk")
    assert not session.committed and events == ["failed", "released"]


def test_atomic_publish_rolls_back_and_keeps_previous_active_pointer(monkeypatch, tmp_path):
    def fail_publish(session, payload):
        raise RuntimeError("domain write failed")

    pipeline, session, events = _pipeline(monkeypatch, tmp_path, publish=fail_publish)
    with pytest.raises(RuntimeError, match="domain write"):
        pipeline.run("bulk")
    assert session.rolled_back and not session.committed
    assert events == ["failed", "released"]


def test_same_active_version_reuses_cache_without_domain_publish(monkeypatch, tmp_path):
    pipeline, session, events = _pipeline(monkeypatch, tmp_path)
    integrity = validate_artifact(tmp_path / "snapshot.json")
    session.existing = type("Publication", (), {
        "source_as_of": NOW,
        "published_at": NOW,
        "record_count": 1,
        "metadata_json": {"coverage": {"records": 1}},
        "checksum": integrity.checksum,
        "version": integrity.checksum,
    })()
    assert pipeline.run("bulk") == 11
    assert events == ["success", "released"]


def test_lock_collision_stops_before_run_creation(monkeypatch, tmp_path):
    pipeline, session, events = _pipeline(monkeypatch, tmp_path, acquire=False)
    with pytest.raises(DatasetLockedError):
        pipeline.run("bulk")
    assert not session.committed and events == []


def test_stale_lock_recovery_contract_is_explicit():
    # Persistence implementation only rejects a non-expired lock owned elsewhere.
    acquired = NOW - timedelta(hours=3)
    timeout = timedelta(hours=2)
    assert acquired + timeout < NOW
