from __future__ import annotations

import csv
import io
import json
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from uuid import uuid4

import pytest
import sqlalchemy as sa
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.database.postgres import SessionLocal, engine
from app.models.company import Company
from app.models.workspace import (
    WorkspaceAuditEvent,
    WorkspaceBulkItem,
    WorkspaceBulkJob,
    WorkspaceEntitlement,
)
from tests.public_test_support import legal_inn, projection
from tests.test_workspace_p0 import _bootstrap, _cleanup, _login
from public_app.repository import PublicRepository
from workspace_app.bulk_service import (
    BulkExportValidationError,
    MAX_FILE_BYTES,
    cancel_bulk_job,
    create_bulk_job,
    bulk_csv_bytes,
    bulk_json_bytes,
    list_bulk_items,
    parse_bulk_csv,
    process_bulk_job_chunk,
    resume_bulk_job,
    retry_bulk_job,
)
from workspace_app.main import create_app
from workspace_app.service import ActionDenied


class FakeBulkRepository:
    def __init__(self, releases, active_release):
        self.releases = {
            release_id: {item.company.inn: item for item in items}
            for release_id, items in releases.items()
        }
        self.active_release_id = active_release
        self.batch_calls = []
        self.fail_batches = 0

    def active_release(self):
        count = len(self.releases[self.active_release_id])
        return {
            "release_id": self.active_release_id,
            "schema_version": "public-projection-v1",
            "record_count": count,
            "actual_record_count": count,
        }

    def get_companies(self, inns, *, release_id):
        self.batch_calls.append((tuple(inns), release_id))
        if self.fail_batches:
            self.fail_batches -= 1
            raise RuntimeError("transient public read")
        return [self.releases[release_id][inn] for inn in inns if inn in self.releases[release_id]]

    def get_company(self, inn):
        return self.releases[self.active_release_id].get(inn)

    def search(self, query, limit=20):
        return [item for item in self.releases[self.active_release_id].values() if query in item.company.inn or query.casefold() in item.company.name.casefold()][:limit]


def _add_companies(*items):
    with Session(engine) as session:
        session.add_all(Company(inn=item.company.inn, name=item.company.name, entity_type="legal") for item in items)
        session.commit()


def _csv(*inns: str, delimiter: str = ",") -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, delimiter=delimiter, lineterminator="\n")
    writer.writerow(["ИНН"])
    for inn in inns:
        writer.writerow([inn])
    return output.getvalue().encode("utf-8-sig")


def test_bulk_parser_contract_security_validation_dedup_and_order():
    first = legal_inn(400_000_001)
    second = legal_inn(400_000_002)
    rows = parse_bulk_csv(_csv(f"  {first}  ", second, first, "", "123456789012", "=1+1", delimiter=";"))
    assert [row.row_number for row in rows] == [2, 3, 4, 5, 6, 7]
    assert [row.status for row in rows] == ["PENDING", "PENDING", "DUPLICATE", "INVALID_INN", "INVALID_INN", "INVALID_INN"]
    assert rows[0].normalized_inn == first
    assert rows[2].duplicate_of_row == 2
    assert rows[4].error_code == "ip_inn_unsupported"
    assert rows[5].raw_inn == "=1+1"

    assert parse_bulk_csv(b"inn\n\n" + first.encode() + b"\n")[0].row_number == 3
    with pytest.raises(ActionDenied, match="2") as oversized:
        parse_bulk_csv(b"x" * (MAX_FILE_BYTES + 1))
    assert oversized.value.code == "bulk_file_too_large"
    cases = (
        (b"\xff", "bulk_invalid_utf8"),
        (b"inn\nabc\x00", "bulk_invalid_character"),
        (b"name\nfoo\n", "bulk_missing_inn_header"),
        (b"inn,INN\n1,2\n", "bulk_duplicate_inn_header"),
        (b"inn,name\n\"unterminated", "bulk_malformed_csv"),
        (b"inn,name\n1\n", "bulk_malformed_row"),
    )
    for content, code in cases:
        with pytest.raises(ActionDenied) as rejected:
            parse_bulk_csv(content)
        assert rejected.value.code == code


def test_public_repository_batch_read_is_exact_pinned_and_single_query():
    item = projection(405_000_001, release_id="public-release-batch")

    class Cursor:
        def __init__(self):
            self.calls = []

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def execute(self, sql, params):
            self.calls.append((sql, params))

        def fetchall(self):
            return [{"payload": item.model_dump(mode="json")}]

    class Connection:
        def __init__(self, cursor):
            self.value = cursor

        def cursor(self):
            return self.value

    cursor = Cursor()
    repository = PublicRepository("postgresql://unused")

    @contextmanager
    def connection():
        yield Connection(cursor)

    repository._connection = connection
    result = repository.get_companies(
        [item.company.inn, item.company.inn], release_id="public-release-batch"
    )
    assert [projection.company.inn for projection in result] == [item.company.inn]
    assert len(cursor.calls) == 1
    sql, params = cursor.calls[0]
    assert "p.release_id = %s" in sql and "p.inn = ANY(%s)" in sql
    assert "public_publication_state" not in sql
    assert params == ("public-release-batch", [item.company.inn])


def test_bulk_service_release_pin_results_exports_and_no_side_effects():
    email = f"bulk-service-{uuid4()}@example.test"
    release_a = "public-release-A"
    release_b = "public-release-B"
    item_a = projection(410_000_001, release_id=release_a)
    item_b = projection(410_000_002, release_id=release_a)
    b_a = projection(410_000_001, release_id=release_b)
    b_b = projection(410_000_002, release_id=release_b)
    missing_master = projection(410_000_003, release_id=release_a)
    master_no_projection = projection(410_000_004, release_id=release_a)
    repository = FakeBulkRepository({release_a: (item_a, item_b), release_b: (b_a, b_b)}, release_a)
    try:
        _add_companies(item_a, item_b, master_no_projection)
        user_id, workspace_id = _bootstrap(email, "Bulk Service")
        content = _csv(item_a.company.inn, item_b.company.inn, item_a.company.inn, "=1+1", missing_master.company.inn, master_no_projection.company.inn)
        with Session(engine) as session:
            job = create_bulk_job(session, user_id=user_id, workspace_id=workspace_id, filename="../../unsafe.csv", content=content)
            job_id = job.id
            assert job.original_filename == "unsafe.csv"
            assert (job.total_rows, job.unique_valid_count, job.invalid_count, job.duplicate_count) == (6, 4, 1, 1)
            session.commit()

        with Session(engine) as session:
            job = process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, projection_repository=repository, chunk_size=1)
            assert job.public_release_id == release_a
            session.commit()
        repository.active_release_id = release_b
        with Session(engine) as session:
            while True:
                job = process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, projection_repository=repository, chunk_size=2)
                terminal = job.status in {"COMPLETED", "COMPLETED_WITH_ERRORS"}
                session.commit()
                if terminal:
                    break
        assert {release_id for _inns, release_id in repository.batch_calls} == {release_a}

        with Session(engine) as session:
            job = session.get(WorkspaceBulkJob, job_id)
            assert (job.status, job.ready_count, job.not_resolved_count, job.not_ready_count, job.failed_count) == ("COMPLETED", 2, 1, 1, 0)
            ready = session.scalars(sa.select(WorkspaceBulkItem).where(WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.status == "READY")).all()
            assert {row.result_payload["release_id"] for row in ready} == {release_a}
            assert all(row.result_sha256 for row in ready)
            assert session.scalar(sa.select(sa.func.count()).select_from(WorkspaceAuditEvent).where(WorkspaceAuditEvent.workspace_id == workspace_id, WorkspaceAuditEvent.action == "bulk.process")) == 1
            exported = json.loads(bulk_json_bytes(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id))
            assert [row["row_number"] for row in exported["items"]] == sorted(row["row_number"] for row in exported["items"])
            csv_text = bulk_csv_bytes(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id).decode("utf-8-sig")
            assert "'=1+1" in csv_text
            assert release_a in csv_text and release_b not in csv_text
            unsafe = session.scalar(sa.select(WorkspaceBulkItem).where(WorkspaceBulkItem.job_id == job_id, WorkspaceBulkItem.raw_inn == "=1+1"))
            unsafe.raw_inn = "\x01=1+1"
            session.flush()
            with pytest.raises(BulkExportValidationError) as rejected:
                bulk_csv_bytes(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id)
            assert rejected.value.code == "bulk_export_invalid_value"
            session.rollback()
    finally:
        _cleanup(email, inns=(item_a.company.inn, item_b.company.inn, master_no_projection.company.inn))


def test_bulk_cancel_resume_retry_idempotency_and_counters():
    email = f"bulk-lifecycle-{uuid4()}@example.test"
    release_id = "public-release-lifecycle"
    items = tuple(projection(420_000_000 + index, release_id=release_id) for index in range(1, 5))
    repository = FakeBulkRepository({release_id: items}, release_id)
    try:
        _add_companies(*items)
        user_id, workspace_id = _bootstrap(email, "Bulk Lifecycle")
        with Session(engine) as session:
            job = create_bulk_job(session, user_id=user_id, workspace_id=workspace_id, filename="items.csv", content=_csv(*(item.company.inn for item in items)))
            job_id = job.id
            session.commit()
        with Session(engine) as session:
            process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, projection_repository=repository, chunk_size=1)
            session.commit()
        with Session(engine) as session:
            job = cancel_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id)
            assert (job.status, job.ready_count, job.cancelled_count) == ("CANCELLED", 1, 3)
            assert cancel_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id).cancelled_count == 3
            pinned = job.public_release_id
            session.commit()
        with Session(engine) as session:
            job = resume_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id)
            assert job.public_release_id == pinned
            assert (job.status, job.cancelled_count) == ("READY", 0)
            session.commit()
        with Session(engine) as session:
            process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, projection_repository=repository, chunk_size=100)
            session.commit()
        with Session(engine) as session:
            job = session.get(WorkspaceBulkJob, job_id)
            assert (job.status, job.ready_count, job.processed_count) == ("COMPLETED", 4, 4)
            assert session.scalar(sa.select(sa.func.count()).select_from(WorkspaceBulkItem).where(WorkspaceBulkItem.job_id == job_id)) == 4

        with Session(engine) as session:
            second = create_bulk_job(session, user_id=user_id, workspace_id=workspace_id, filename="retry.csv", content=_csv(items[0].company.inn))
            second_id = second.id
            session.commit()
        repository.fail_batches = 1
        with Session(engine) as session:
            failed = process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=second_id, projection_repository=repository)
            assert (failed.status, failed.failed_count) == ("COMPLETED_WITH_ERRORS", 1)
            session.commit()
        with Session(engine) as session:
            retried = retry_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=second_id)
            assert (retried.status, retried.failed_count) == ("READY", 0)
            assert retry_bulk_job(session, user_id=user_id, workspace_id=workspace_id, job_id=second_id).status == "READY"
            session.commit()
        with Session(engine) as session:
            completed = process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=second_id, projection_repository=repository)
            assert (completed.status, completed.ready_count) == ("COMPLETED", 1)
            session.commit()
    finally:
        _cleanup(email, inns=tuple(item.company.inn for item in items))


def test_bulk_quota_rbac_tenant_scope_and_pagination():
    email_a = f"bulk-tenant-a-{uuid4()}@example.test"
    email_b = f"bulk-tenant-b-{uuid4()}@example.test"
    item = projection(430_000_001, release_id="public-release-tenant")
    try:
        _add_companies(item)
        user_a, workspace_a = _bootstrap(email_a, "Bulk Tenant A")
        user_b, workspace_b = _bootstrap(email_b, "Bulk Tenant B")
        with Session(engine) as session:
            entitlement = session.scalar(sa.select(WorkspaceEntitlement).where(WorkspaceEntitlement.workspace_id == workspace_a, WorkspaceEntitlement.entitlement_key == "bulk_check.enabled"))
            assert entitlement.enabled and entitlement.limit_value == 1000
            entitlement.limit_value = 0
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as quota:
                create_bulk_job(session, user_id=user_a, workspace_id=workspace_a, filename="quota.csv", content=_csv(item.company.inn))
            assert quota.value.code == "bulk_quota_exceeded"
            assert session.scalar(sa.select(sa.func.count()).select_from(WorkspaceBulkJob).where(WorkspaceBulkJob.workspace_id == workspace_a)) == 0
            entitlement = session.scalar(sa.select(WorkspaceEntitlement).where(WorkspaceEntitlement.workspace_id == workspace_a, WorkspaceEntitlement.entitlement_key == "bulk_check.enabled"))
            entitlement.limit_value = 1000
            session.commit()
        with Session(engine) as session:
            job = create_bulk_job(session, user_id=user_a, workspace_id=workspace_a, filename="scope.csv", content=_csv(item.company.inn, item.company.inn, "bad"))
            job_id = job.id
            session.commit()
        with Session(engine) as session:
            with pytest.raises(ActionDenied) as hidden:
                list_bulk_items(session, user_id=user_b, workspace_id=workspace_b, job_id=job_id)
            assert hidden.value.code == "bulk_job_not_found"
            page = list_bulk_items(session, user_id=user_a, workspace_id=workspace_a, job_id=job_id, page=1, page_size=2)
            assert (page.total, page.pages, len(page.items)) == (3, 2, 2)
            assert [row.row_number for row in page.items] == sorted(row.row_number for row in page.items)
    finally:
        _cleanup(email_a, email_b, inns=(item.company.inn,))


def test_bulk_process_concurrency_gate_does_not_duplicate_results():
    email = f"bulk-concurrency-{uuid4()}@example.test"
    release_id = "public-release-concurrency"
    items = tuple(projection(440_000_000 + index, release_id=release_id) for index in range(1, 3))
    repository = FakeBulkRepository({release_id: items}, release_id)
    try:
        _add_companies(*items)
        user_id, workspace_id = _bootstrap(email, "Bulk Concurrency")
        with Session(engine) as session:
            job = create_bulk_job(session, user_id=user_id, workspace_id=workspace_id, filename="concurrency.csv", content=_csv(*(item.company.inn for item in items)))
            job_id = job.id
            session.commit()

        def process_one():
            with Session(engine) as session:
                result = process_bulk_job_chunk(session, user_id=user_id, workspace_id=workspace_id, job_id=job_id, projection_repository=repository, chunk_size=1)
                status = result.status
                session.commit()
                return status

        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses = tuple(executor.map(lambda _value: process_one(), range(2)))
        assert "COMPLETED" in statuses
        with Session(engine) as session:
            job = session.get(WorkspaceBulkJob, job_id)
            rows = session.scalars(sa.select(WorkspaceBulkItem).where(WorkspaceBulkItem.job_id == job_id).order_by(WorkspaceBulkItem.row_number)).all()
            assert (job.ready_count, job.processed_count, len(rows)) == (2, 2, 2)
            assert all(row.result_sha256 for row in rows)
    finally:
        _cleanup(email, inns=tuple(item.company.inn for item in items))


def test_bulk_html_api_flow_csrf_filters_exports_and_workspace_switch_scope():
    email_a = f"bulk-http-a-{uuid4()}@example.test"
    email_b = f"bulk-http-b-{uuid4()}@example.test"
    release_id = "public-release-http"
    items = tuple(projection(450_000_000 + index, release_id=release_id) for index in range(1, 3))
    repository = FakeBulkRepository({release_id: items}, release_id)
    try:
        _add_companies(*items)
        _user_a, _workspace_a = _bootstrap(email_a, "Bulk HTTP A")
        _user_b, _workspace_b = _bootstrap(email_b, "Bulk HTTP B")
        client = TestClient(create_app(public_repository=repository, session_factory=SessionLocal))
        _login(client, email_a)
        history = client.get("/app/bulk")
        assert history.status_code == 200 and "Пока нет массовых проверок" in history.text
        assert client.post("/app/bulk", files={"file": ("items.csv", _csv(*(item.company.inn for item in items)), "text/csv")}).status_code == 403
        csrf_token = client.cookies.get("nextcompany_csrf")
        created = client.post("/app/bulk", data={"csrf": csrf_token}, files={"file": ("items.csv", _csv(*(item.company.inn for item in items)), "text/csv")}, follow_redirects=False)
        assert created.status_code == 303
        detail_url = created.headers["location"]
        job_id = detail_url.rsplit("/", 1)[-1]
        detail = client.get(detail_url)
        assert detail.status_code == 200 and "Обработать следующий блок" in detail.text
        processed = client.post(f"/app/api/bulk-jobs/{job_id}/process-next", headers={"X-CSRF-Token": csrf_token})
        assert processed.status_code == 200 and processed.json()["status"] == "COMPLETED"
        filtered = client.get(f"{detail_url}?status=READY")
        assert filtered.status_code == 200 and filtered.text.count('data-item-status="READY"') == 2
        assert f'/app/companies/{items[0].company.inn}' in filtered.text
        csv_export = client.get(f"/app/api/bulk-jobs/{job_id}/export.csv")
        json_export = client.get(f"/app/api/bulk-jobs/{job_id}/export.json")
        assert csv_export.status_code == 200 and csv_export.content.startswith(b"\xef\xbb\xbf")
        assert json_export.status_code == 200 and len(json_export.json()["items"]) == 2

        other = TestClient(create_app(public_repository=repository, session_factory=SessionLocal))
        _login(other, email_b)
        assert other.get(f"/app/api/bulk-jobs/{job_id}").status_code == 404
        assert other.post(f"/app/api/bulk-jobs/{job_id}/cancel", headers={"X-CSRF-Token": other.cookies.get("nextcompany_csrf")}).status_code == 404
        assert other.get(f"/app/api/bulk-jobs/{job_id}/export.csv").status_code == 404
        assert other.get("/app/api/bulk-jobs").json()["items"] == []
    finally:
        _cleanup(email_a, email_b, inns=tuple(item.company.inn for item in items))
