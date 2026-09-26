#!/usr/bin/env python3
"""Persistent HOME-initiated full-release public synchronizer."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
import io
import json
import logging
import os
from pathlib import Path
import re
import shlex
import subprocess
import tarfile
import time
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row
from sqlalchemy import select

from app.database.postgres import SessionLocal, engine
from app.incidents.classification import Classification
from app.incidents.controller import TERMINAL_STATUSES, upsert_incident
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.incident import SourceIncident
from app.models.publication import PublicProjectionPublication, PublicPublicationRequest
from app.services.publication_service import (
    PUBLIC_SYNC_LOCK_ID,
    PublicBuildError,
    PublicVpsUnavailable,
    accepted_cohort,
    claim_next_request,
    cohort_companies,
    current_main_sha,
    database_url_for_psycopg,
    fail_request,
    fetch_live_cohort,
    publishable_runs,
    recover_interrupted_requests,
    scan_public_ready_changes,
    schedule_retry,
    utc_now,
)
from public_app.contracts import (
    ChangedCompanySummary,
    PublicationInfo,
    PublicProjection,
    ReleaseManifest,
    scan_forbidden,
)
from scripts.export_public_release import build_projection, release_id_for
from scripts.public_release_common import (
    canonical_json,
    load_bundle,
    semantic_projection_sha256,
    write_checksums,
)


logger = logging.getLogger("nextcompany.public_sync")
SAFE_TARGET = re.compile(r"^[a-zA-Z0-9_.@:-]+$")
SAFE_RELEASE = re.compile(r"^[a-zA-Z0-9._-]{8,120}$")


class PublicTransportError(RuntimeError):
    def __init__(self, stage: str, message: str, *, transient: bool) -> None:
        super().__init__(message)
        self.stage = stage
        self.transient = transient


def _run(argv: list[str], *, input_bytes: bytes | None = None, timeout: int = 180) -> str:
    try:
        result = subprocess.run(
            argv,
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as error:
        raise PublicTransportError("network", "public transport timed out", transient=True) from error
    except OSError as error:
        raise PublicTransportError("network", type(error).__name__, transient=True) from error
    if result.returncode:
        message = (result.stderr or result.stdout or b"command failed").decode(
            "utf-8", errors="replace"
        )[-1600:]
        raise PublicTransportError(
            "network" if result.returncode == 255 else "remote",
            message,
            transient=result.returncode == 255,
        )
    return result.stdout.decode("utf-8", errors="replace")


class SshPublicTransport:
    def __init__(self, target: str | None = None) -> None:
        self.target = (target or os.getenv("PUBLIC_SSH_TARGET", "")).strip()
        if not SAFE_TARGET.fullmatch(self.target):
            raise PublicBuildError("PUBLIC_SSH_TARGET is missing or unsafe")

    @staticmethod
    def _archive(bundle_dir: Path) -> bytes:
        output = io.BytesIO()
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name in ("manifest.json", "companies.jsonl.gz", "checksums.sha256"):
                archive.add(bundle_dir / name, arcname=name, recursive=False)
        return output.getvalue()

    def upload(self, bundle_dir: Path, release_id: str) -> None:
        if not SAFE_RELEASE.fullmatch(release_id):
            raise PublicBuildError("unsafe public release ID")
        remote = f"/var/lib/nextcompany/incoming/{release_id}"
        command = (
            f"sudo install -d -o nextcompany-importer -g nextcompany -m 0750 {shlex.quote(remote)}"
            f" && sudo -u nextcompany-importer tar -C {shlex.quote(remote)} -xf -"
        )
        try:
            _run(["ssh", "-o", "BatchMode=yes", self.target, command], input_bytes=self._archive(bundle_dir))
        except PublicTransportError as error:
            raise PublicTransportError("upload", str(error), transient=error.transient) from error

    def import_release(self, release_id: str) -> dict[str, Any]:
        remote = f"/var/lib/nextcompany/incoming/{release_id}"
        command = (
            "sudo -u nextcompany-importer /bin/bash -lc "
            + shlex.quote(
                "set -a; source /etc/nextcompany/importer.env; set +a; "
                f"/opt/nextcompany/current/.venv/bin/python "
                f"/opt/nextcompany/current/scripts/import_public_release.py "
                f"{shlex.quote(remote)} --expected-release-id {shlex.quote(release_id)}"
            )
        )
        try:
            output = _run(["ssh", "-o", "BatchMode=yes", self.target, command])
            return json.loads(output.strip().splitlines()[-1])
        except PublicTransportError as error:
            raise PublicTransportError("import", str(error), transient=error.transient) from error
        except (ValueError, IndexError) as error:
            raise PublicTransportError("import", "invalid importer response", transient=False) from error

    def rollback(self, previous_release_id: str) -> dict[str, Any]:
        if not SAFE_RELEASE.fullmatch(previous_release_id):
            raise PublicBuildError("unsafe previous release ID")
        command = (
            "sudo -u nextcompany-importer /bin/bash -lc "
            + shlex.quote(
                "set -a; source /etc/nextcompany/importer.env; set +a; "
                "/opt/nextcompany/current/.venv/bin/python "
                "/opt/nextcompany/current/scripts/rollback_public_release.py "
                + shlex.quote(previous_release_id)
            )
        )
        output = _run(["ssh", "-o", "BatchMode=yes", self.target, command])
        return json.loads(output.strip().splitlines()[-1])


def _release_projection(
    projection: PublicProjection,
    *,
    release_id: str,
    published_at: datetime,
) -> PublicProjection:
    return projection.model_copy(
        update={
            "publication": projection.publication.model_copy(
                update={"release_id": release_id, "published_at": published_at}
            )
        }
    )


def _write_bundle(
    bundle_dir: Path,
    manifest: ReleaseManifest,
    projections: list[PublicProjection],
) -> None:
    bundle_dir.mkdir(parents=True, exist_ok=False)
    with gzip.open(
        bundle_dir / "companies.jsonl.gz", "wt", encoding="utf-8", newline="\n"
    ) as stream:
        for projection in projections:
            stream.write(
                canonical_json(projection.model_dump(mode="json")).decode("utf-8")
                + "\n"
            )
    (bundle_dir / "manifest.json").write_bytes(
        canonical_json(manifest.model_dump(mode="json")) + b"\n"
    )
    write_checksums(bundle_dir)
    load_bundle(bundle_dir)


def build_candidate(
    session,
    request: PublicPublicationRequest,
    *,
    now: datetime,
    output_root: Path,
    client: httpx.Client | None = None,
) -> tuple[Path | None, ReleaseManifest | None]:
    """Build one immutable full cohort, carrying only last-good non-ready cards."""

    accepted, cohort_hash = accepted_cohort()
    companies = cohort_companies(session, accepted)
    states = {
        row.company_id: row
        for row in session.scalars(
            select(PublicProjectionPublication).where(
                PublicProjectionPublication.company_id.in_([item.id for item in companies])
            )
        )
    }
    if len(states) != len(companies):
        raise PublicBuildError("published projection baseline is incomplete")
    previous_release_id, live = fetch_live_cohort(accepted, client=client)
    for company in companies:
        if semantic_projection_sha256(live[company.inn]) != states[company.id].last_published_hash:
            raise PublicBuildError(
                f"live projection hash is not the recorded last-good hash: {company.inn}"
            )

    source_sha = current_main_sha()
    if request.created_main_sha != source_sha:
        request.created_main_sha = source_sha
    release_id = release_id_for(now, source_sha, cohort_hash)
    if not SAFE_RELEASE.fullmatch(release_id):
        raise PublicBuildError("generated release ID is invalid")
    base_publication = PublicationInfo(
        schema_version="public-projection-v1",
        release_id=release_id,
        published_at=now,
        result_date=now.date(),
        content_updated_at=now,
        index_eligible=False,
    )
    runs = publishable_runs(session, companies)
    current: dict[int, PublicProjection] = {}
    with psycopg.connect(database_url_for_psycopg(), row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            for company in companies:
                if company.id in runs:
                    current[company.id] = build_projection(
                        cursor, company.inn, base_publication
                    )

    projections: list[PublicProjection] = []
    changes: list[ChangedCompanySummary] = []
    for company in companies:
        source = current.get(company.id, live[company.inn])
        projection = _release_projection(source, release_id=release_id, published_at=now)
        scan_forbidden(projection.model_dump(mode="json"))
        current_hash = semantic_projection_sha256(projection)
        previous_hash = states[company.id].last_published_hash
        if current_hash != previous_hash:
            if company.id not in runs:
                raise PublicBuildError(
                    f"non-ready company changed outside canonical readiness: {company.inn}"
                )
            changes.append(
                ChangedCompanySummary(
                    inn=company.inn,
                    previous_hash=previous_hash,
                    current_hash=current_hash,
                )
            )
        projections.append(projection)

    if not changes:
        request.status = "SUPERSEDED"
        request.changed_company_count = 0
        request.changed_company_ids = []
        request.changed_company_inns = []
        request.change_summary = []
        request.last_error = "NO_PUBLIC_CHANGE"
        request.completed_at = now
        request.updated_at = now
        return None, None

    inns = [projection.company.inn for projection in projections]
    expected_inns = [entity.inn for entity in accepted.entities]
    if inns != expected_inns or len(set(inns)) != len(inns):
        raise PublicBuildError("candidate is not the exact accepted cohort")
    content_updated_at = max(item.publication.content_updated_at for item in projections)
    release_manifest = ReleaseManifest(
        schema_version="public-projection-v1",
        release_id=release_id,
        source_main_sha=source_sha,
        cohort_manifest_path="docs/releases/public-v1-cohort-40.json",
        cohort_manifest_sha256=cohort_hash,
        cohort_source_main_sha=accepted.source_main_sha,
        previous_release_id=previous_release_id,
        created_at=now,
        result_date=max(item.publication.result_date for item in projections),
        content_updated_at=content_updated_at,
        record_count=len(projections),
        companies_file="companies.jsonl.gz",
        changed_company_count=len(changes),
        changed_companies=tuple(changes),
    )
    bundle_dir = output_root / release_id
    _write_bundle(bundle_dir, release_manifest, projections)
    request.candidate_release_id = release_id
    request.previous_release_id = previous_release_id
    request.projection_generation = release_id
    request.projection_hash = __import__("hashlib").sha256(
        canonical_json([(item.inn, item.current_hash) for item in changes])
    ).hexdigest()
    by_inn = {company.inn: company.id for company in companies}
    request.changed_company_count = len(changes)
    request.changed_company_inns = [item.inn for item in changes]
    request.changed_company_ids = [by_inn[item.inn] for item in changes]
    request.change_summary = [item.model_dump(mode="json") for item in changes]
    request.status = "READY"
    request.ready_at = now
    request.last_error = None
    request.updated_at = now
    return bundle_dir, release_manifest


def verify_https_release(
    bundle_dir: Path,
    manifest: ReleaseManifest,
    *,
    client: httpx.Client | None = None,
) -> None:
    owns_client = client is None
    http = client or httpx.Client(
        base_url=os.getenv("PUBLIC_ORIGIN", "https://nextcompany.pro").rstrip("/"),
        timeout=httpx.Timeout(15.0, connect=5.0),
        follow_redirects=False,
        headers={"User-Agent": "nextcompany-public-sync/1"},
    )
    try:
        response = http.get("/api/ready")
        if response.status_code != 200:
            raise PublicVpsUnavailable(f"HTTPS ready returned {response.status_code}")
        ready = response.json()
        if (
            ready.get("status") != "ready"
            or ready.get("release_id") != manifest.release_id
            or int(ready.get("record_count") or 0) != manifest.record_count
        ):
            raise PublicVpsUnavailable("HTTPS ready does not identify candidate release")
        _loaded_manifest, projections, _sha = load_bundle(bundle_dir)
        by_inn = {item.company.inn: item for item in projections}
        changed = [item.inn for item in manifest.changed_companies]
        verify_inns = changed if len(changed) <= 10 else changed[:3]
        for inn in verify_inns:
            html = http.get(f"/companies/{inn}")
            api = http.get(f"/api/company/{inn}")
            if html.status_code != 200 or api.status_code != 200:
                raise PublicVpsUnavailable(f"changed public card failed HTTPS verification: {inn}")
            observed = PublicProjection.model_validate(api.json())
            if semantic_projection_sha256(observed) != semantic_projection_sha256(by_inn[inn]):
                raise PublicVpsUnavailable(f"changed public card hash mismatch: {inn}")
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, PublicVpsUnavailable):
            raise
        raise PublicVpsUnavailable(type(error).__name__) from error
    finally:
        if owns_client:
            http.close()


def _public_incident(session, *, category: str, message: object, owner: str) -> None:
    upsert_incident(
        session,
        source_id="public_sync",
        dataset="public_release",
        classification=Classification(
            category=category,
            owner_domain=owner,
            severity="CRITICAL" if category in {"PUBLIC_IMPORT_ERROR", "PUBLIC_VERIFY_ERROR"} else "HIGH",
            remediation_level=1 if owner == "OUR_INFRASTRUCTURE" else 3,
        ),
        error_code=category.lower(),
        message=str(message),
    )


def _resolve_public_incidents(session, *, now: datetime) -> None:
    rows = list(
        session.scalars(
            select(SourceIncident).where(
                SourceIncident.source_id == "public_sync",
                SourceIncident.status.not_in(TERMINAL_STATUSES),
            )
        )
    )
    for row in rows:
        row.status = "RESOLVED"
        row.resolved_at = now
        row.updated_at = now
        row.resolution = "public release published and verified over HTTPS"
        row.resolution_evidence = {
            "verified_at": now.isoformat(), "channel": "public_sync"
        }


def _persist_published_hashes(
    session,
    request: PublicPublicationRequest,
    bundle_dir: Path,
    *,
    now: datetime,
) -> None:
    manifest, projections, _manifest_sha = load_bundle(bundle_dir)
    accepted, _ = accepted_cohort()
    companies = cohort_companies(session, accepted)
    by_inn = {company.inn: company for company in companies}
    runs = publishable_runs(session, companies)
    for projection in projections:
        company = by_inn[projection.company.inn]
        state = session.get(PublicProjectionPublication, company.id)
        if state is None:
            raise PublicBuildError("published projection baseline disappeared")
        state.last_published_hash = semantic_projection_sha256(projection)
        state.last_published_release_id = manifest.release_id
        state.last_enrichment_run_id = runs.get(company.id).id if company.id in runs else None
        state.published_at = now
        state.updated_at = now
    request.status = "PUBLISHED"
    request.published_release_id = manifest.release_id
    request.verified_at = now
    request.completed_at = now
    request.next_attempt_at = None
    request.last_error = None
    request.updated_at = now


def _active_release_id(client: httpx.Client | None = None) -> str | None:
    http = client or httpx.Client(
        base_url=os.getenv("PUBLIC_ORIGIN", "https://nextcompany.pro").rstrip("/"),
        timeout=8.0,
    )
    owns = client is None
    try:
        response = http.get("/api/ready")
        if response.status_code != 200:
            return None
        return response.json().get("release_id")
    except Exception:
        return None
    finally:
        if owns:
            http.close()


def publish_claimed_request(
    session,
    request: PublicPublicationRequest,
    *,
    output_root: Path,
    transport: SshPublicTransport,
    client: httpx.Client | None = None,
) -> str:
    now = utc_now()
    try:
        if request.candidate_release_id:
            bundle_dir = output_root / request.candidate_release_id
            manifest, _projections, _sha = load_bundle(bundle_dir)
        else:
            bundle_dir, manifest = build_candidate(
                session, request, now=now, output_root=output_root, client=client
            )
            session.commit()
            if bundle_dir is None or manifest is None:
                return "NO_PUBLIC_CHANGE"
        request.status = "UPLOADING"
        request.updated_at = utc_now()
        session.commit()
        transport.upload(bundle_dir, manifest.release_id)
        request.uploaded_at = utc_now()
        request.status = "IMPORTING"
        request.updated_at = request.uploaded_at
        session.commit()
        transport.import_release(manifest.release_id)
        request.imported_at = utc_now()
        request.status = "VERIFYING"
        request.updated_at = request.imported_at
        session.commit()
        verify_https_release(bundle_dir, manifest, client=client)
        completed = utc_now()
        _persist_published_hashes(session, request, bundle_dir, now=completed)
        _resolve_public_incidents(session, now=completed)
        session.commit()
        return "PUBLISHED"
    except PublicTransportError as error:
        session.rollback()
        request = session.get(PublicPublicationRequest, request.id)
        active = _active_release_id(client)
        if (
            request
            and request.candidate_release_id
            and (request.imported_at is not None or active == request.candidate_release_id)
        ):
            try:
                transport.rollback(request.previous_release_id or "")
                request.rollback_completed_at = utc_now()
            except Exception as rollback_error:
                error = PublicTransportError(
                    "rollback", f"{error}; rollback failed: {rollback_error}", transient=False
                )
            fail_request(request, error)
            _public_incident(
                session,
                category="PUBLIC_IMPORT_ERROR",
                message=error,
                owner="OUR_CODE",
            )
        elif request and error.transient:
            schedule_retry(request, error)
            _public_incident(
                session,
                category="PUBLIC_VPS_UNAVAILABLE" if error.stage == "network" else "PUBLIC_UPLOAD_ERROR",
                message=error,
                owner="OUR_INFRASTRUCTURE",
            )
        elif request:
            fail_request(request, error)
            _public_incident(
                session,
                category="PUBLIC_IMPORT_ERROR" if error.stage == "import" else "PUBLIC_UPLOAD_ERROR",
                message=error,
                owner="OUR_CODE",
            )
        session.commit()
        return request.status if request else "FAILED"
    except PublicVpsUnavailable as error:
        session.rollback()
        request = session.get(PublicPublicationRequest, request.id)
        active = _active_release_id(client)
        if (
            request
            and request.candidate_release_id
            and (request.imported_at is not None or active == request.candidate_release_id)
        ):
            try:
                transport.rollback(request.previous_release_id or "")
                request.rollback_completed_at = utc_now()
                accepted, _ = accepted_cohort()
                restored, _cards = fetch_live_cohort(accepted, client=client)
                if restored != request.previous_release_id:
                    raise PublicVpsUnavailable("rollback HTTPS verification returned wrong release")
            except Exception as rollback_error:
                error = PublicVpsUnavailable(f"{error}; rollback failed: {rollback_error}")
            fail_request(request, error)
            _public_incident(session, category="PUBLIC_VERIFY_ERROR", message=error, owner="OUR_CODE")
        elif request:
            schedule_retry(request, error)
            _public_incident(session, category="PUBLIC_VPS_UNAVAILABLE", message=error, owner="OUR_INFRASTRUCTURE")
        session.commit()
        return request.status if request else "FAILED"
    except Exception as error:
        session.rollback()
        request = session.get(PublicPublicationRequest, request.id)
        if request:
            fail_request(request, error)
            _public_incident(session, category="PUBLIC_BUILD_ERROR", message=error, owner="OUR_CODE")
            session.commit()
        return "FAILED"


def run_once(
    *,
    debounce_seconds: int = 120,
    output_root: Path | None = None,
    transport: SshPublicTransport | None = None,
    client: httpx.Client | None = None,
) -> dict[str, Any]:
    output_root = output_root or Path(
        os.getenv("PUBLIC_RELEASE_OUTPUT_ROOT", "/var/lib/nextcompany/public-sync")
    )
    output_root.mkdir(parents=True, exist_ok=True)
    # Keep the advisory-lock connection checked out for the entire cycle.
    # Publication state commits must not accidentally return the lock-owning
    # connection to the pool while network/import verification is still active.
    with engine.connect() as lock_connection:
        locked = bool(
            lock_connection.exec_driver_sql(
                "SELECT pg_try_advisory_lock(%s)", (PUBLIC_SYNC_LOCK_ID,)
            ).scalar()
        )
        if not locked:
            return {"status": "LOCKED", "scan": None}
        with SessionLocal() as session:
            try:
                recovered = recover_interrupted_requests(session)
                scan = scan_public_ready_changes(session)
                session.commit()
                request = claim_next_request(
                    session,
                    debounce_seconds=debounce_seconds,
                )
                if request is None:
                    session.commit()
                    return {"status": "IDLE", "recovered": recovered, "scan": scan}
                request_id = str(request.id)
                session.commit()
                outcome = publish_claimed_request(
                    session,
                    session.get(PublicPublicationRequest, request.id),
                    output_root=output_root,
                    transport=transport or SshPublicTransport(),
                    client=client,
                )
                return {
                    "status": outcome,
                    "request_id": request_id,
                    "recovered": recovered,
                    "scan": scan,
                }
            finally:
                lock_connection.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%s)", (PUBLIC_SYNC_LOCK_ID,)
                )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=30)
    parser.add_argument("--debounce-seconds", type=int, default=120)
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    while True:
        result = run_once(
            debounce_seconds=args.debounce_seconds,
            output_root=args.output_root,
        )
        logger.info("public sync cycle: %s", json.dumps(result, ensure_ascii=False, default=str))
        if args.once:
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 0 if result["status"] not in {"FAILED"} else 1
        time.sleep(min(60, max(5, args.poll_seconds)))


if __name__ == "__main__":
    raise SystemExit(main())
