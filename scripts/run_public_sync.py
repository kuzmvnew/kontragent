#!/usr/bin/env python3
"""Persistent HOME-initiated full-release public synchronizer."""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import gzip
import hashlib
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
from uuid import UUID

import httpx
import psycopg
from psycopg.rows import dict_row
from sqlalchemy import select

from app.database.postgres import SessionLocal, engine
from app.incidents.classification import Classification
from app.incidents.controller import TERMINAL_STATUSES, upsert_incident
from app.incidents.safe import safe_text
from app.models.company_enrichment import CompanyEnrichmentRun
from app.models.incident import SourceIncident
from app.models.publication import PublicProjectionPublication, PublicPublicationRequest
from app.services.publication_service import (
    PUBLIC_SYNC_LOCK_ID,
    STALE_PARENT_RELEASE,
    PublicBuildError,
    PublicVpsUnavailable,
    TrustedReleaseProjectionBatch,
    TrustedProjectionReader,
    accepted_cohort,
    claim_next_request,
    cohort_companies,
    current_main_sha,
    database_url_for_psycopg,
    fail_request,
    fetch_live_cohort,
    fetch_live_release_id,
    fetch_transition_parent_cohort,
    normalize_after_successful_publication,
    normalize_publication_queue,
    publishable_runs,
    recover_failed_publication_if_eligible,
    recover_interrupted_requests,
    scan_public_ready_changes,
    schedule_retry,
    utc_now,
)
from public_app.contracts import (
    HASH_ALGORITHM_VERSION,
    PROJECTION_VERSION,
    ChangedCompanySummary,
    PublicationInfo,
    PublicProjection,
    ReleaseManifest,
    ReleaseTransitionSummary,
    scan_forbidden,
)
from scripts.export_public_release import build_projection, release_id_for
from scripts.public_release_common import (
    canonical_json,
    load_bundle,
    semantic_projection_sha256,
    sha256_file,
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
        self.ssh_argv = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=yes",
        ]
        known_hosts = self._configured_file("PUBLIC_SSH_KNOWN_HOSTS_FILE")
        identity = self._configured_file("PUBLIC_SSH_IDENTITY_FILE")
        if known_hosts:
            self.ssh_argv.extend(["-o", f"UserKnownHostsFile={known_hosts}"])
        if identity:
            self.ssh_argv.extend(["-o", "IdentitiesOnly=yes", "-i", identity])

    @staticmethod
    def _configured_file(variable: str) -> str | None:
        value = os.getenv(variable, "").strip()
        if not value:
            return None
        path = Path(value).expanduser()
        if not path.is_absolute() or not path.is_file():
            raise PublicBuildError(f"{variable} must name an existing absolute file")
        return str(path)

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
            _run([*self.ssh_argv, self.target, command], input_bytes=self._archive(bundle_dir))
        except PublicTransportError as error:
            raise PublicTransportError("upload", str(error), transient=error.transient) from error

    def read_active_projections(
        self,
        release_id: str,
        inns: tuple[str, ...],
    ) -> TrustedReleaseProjectionBatch:
        """Read full stored projections without exposing them through public HTTP."""

        if not SAFE_RELEASE.fullmatch(release_id):
            raise PublicBuildError("unsafe public release ID")
        if (
            tuple(sorted(inns)) != inns
            or len(inns) != len(set(inns))
            or any(not re.fullmatch(r"[0-9]{10}", inn) for inn in inns)
        ):
            raise PublicBuildError("unsafe trusted projection request")
        arguments = " ".join(
            f"--inn {shlex.quote(inn)}" for inn in inns
        )
        command = (
            "sudo -u nextcompany-importer /bin/bash -lc "
            + shlex.quote(
                "set -a; source /etc/nextcompany/importer.env; set +a; "
                "/opt/nextcompany/current/.venv/bin/python "
                "/opt/nextcompany/current/scripts/read_public_release.py "
                f"--expected-release-id {shlex.quote(release_id)}"
                + (f" {arguments}" if arguments else "")
            )
        )
        try:
            output = _run([*self.ssh_argv, self.target, command])
        except PublicTransportError as error:
            category = "network" if error.transient else "remote"
            raise PublicVpsUnavailable(
                "stage=transition_parent_storage "
                f"error_type=transport_{category}"
            ) from error
        try:
            raw = json.loads(output.strip().splitlines()[-1])
            returned_release_id = str(raw["release_id"])
            record_count = int(raw["record_count"])
            raw_member_inns = raw["member_inns"]
            if not isinstance(raw_member_inns, list):
                raise TypeError("member_inns must be a list")
            member_inns = tuple(str(inn) for inn in raw_member_inns)
            if (
                tuple(sorted(member_inns)) != member_inns
                or len(member_inns) != len(set(member_inns))
                or any(not re.fullmatch(r"[0-9]{10}", inn) for inn in member_inns)
                or len(member_inns) != record_count
            ):
                raise ValueError("invalid member inventory")
            rows = raw["projections"]
            if not isinstance(rows, list):
                raise TypeError("projections must be a list")
            projections: dict[str, Any] = {}
            payload_sha256s: dict[str, str] = {}
            for row in rows:
                if not isinstance(row, dict) or set(row) != {
                    "inn",
                    "payload",
                    "payload_sha256",
                }:
                    raise TypeError("invalid projection envelope")
                inn = str(row["inn"])
                if inn in projections:
                    raise ValueError("duplicate projection")
                projections[inn] = row["payload"]
                stored_payload_sha256 = str(row["payload_sha256"])
                if not re.fullmatch(r"[0-9a-f]{64}", stored_payload_sha256):
                    raise ValueError("invalid payload checksum")
                payload_sha256s[inn] = stored_payload_sha256
        except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise PublicVpsUnavailable(
                "stage=transition_parent_storage error_type=invalid_response"
            ) from error
        return TrustedReleaseProjectionBatch(
            release_id=returned_release_id,
            record_count=record_count,
            member_inns=member_inns,
            projections=projections,
            payload_sha256s=payload_sha256s,
        )

    def _remote_release_command(self, release_id: str, mode: str) -> dict[str, Any]:
        remote = f"/var/lib/nextcompany/incoming/{release_id}"
        if mode not in {"--stage-only", "--accept-staged", "--promote"}:
            raise PublicBuildError("unsafe public release command mode")
        command = (
            "sudo -u nextcompany-importer /bin/bash -lc "
            + shlex.quote(
                "set -a; source /etc/nextcompany/importer.env; set +a; "
                f"/opt/nextcompany/current/.venv/bin/python "
                f"/opt/nextcompany/current/scripts/import_public_release.py "
                f"{shlex.quote(remote)} --expected-release-id {shlex.quote(release_id)} "
                f"{mode}"
            )
        )
        try:
            output = _run([*self.ssh_argv, self.target, command])
            return json.loads(output.strip().splitlines()[-1])
        except PublicTransportError as error:
            stage = {
                "--stage-only": "import",
                "--accept-staged": "acceptance",
                "--promote": "promotion",
            }[mode]
            raise PublicTransportError(stage, str(error), transient=error.transient) from error
        except (ValueError, IndexError) as error:
            raise PublicTransportError("import", "invalid importer response", transient=False) from error

    def import_release(self, release_id: str) -> dict[str, Any]:
        return self._remote_release_command(release_id, "--stage-only")

    def accept_release(self, release_id: str) -> dict[str, Any]:
        return self._remote_release_command(release_id, "--accept-staged")

    def promote_release(self, release_id: str) -> dict[str, Any]:
        return self._remote_release_command(release_id, "--promote")

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
        output = _run([*self.ssh_argv, self.target, command])
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
) -> ReleaseManifest:
    bundle_dir.mkdir(parents=True, exist_ok=False)
    companies_path = bundle_dir / "companies.jsonl.gz"
    with companies_path.open("wb") as output:
        with gzip.GzipFile(filename="", mode="wb", fileobj=output, mtime=0) as compressed:
            for projection in projections:
                compressed.write(
                    canonical_json(projection.model_dump(mode="json")) + b"\n"
                )
    manifest = manifest.model_copy(
        update={
            "artifact_hashes": {
                "companies.jsonl.gz": sha256_file(companies_path),
            }
        }
    )
    (bundle_dir / "manifest.json").write_bytes(
        canonical_json(manifest.model_dump(mode="json")) + b"\n"
    )
    write_checksums(bundle_dir)
    load_bundle(bundle_dir)
    return manifest


def build_candidate(
    session,
    request: PublicPublicationRequest,
    *,
    now: datetime,
    output_root: Path,
    trusted_projection_reader: TrustedProjectionReader,
    client: httpx.Client | None = None,
) -> tuple[Path | None, ReleaseManifest | None]:
    """Build one immutable release from current-ready target membership."""

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
    by_inn = {company.inn: company for company in companies}
    runs = publishable_runs(session, companies)
    target_companies = [company for company in companies if company.id in runs]
    target_inns = [company.inn for company in target_companies]
    state_by_inn = {
        company.inn: states[company.id]
        for company in companies
        if company.id in states
    }
    parent = fetch_transition_parent_cohort(
        accepted,
        target_inns=target_inns,
        publication_states=state_by_inn,
        trusted_projection_reader=trusted_projection_reader,
        client=client,
    )
    previous_release_id = parent.release_id

    source_sha = current_main_sha()
    if request.created_main_sha != source_sha:
        request.created_main_sha = source_sha
    target_identity_hash = hashlib.sha256(
        canonical_json(
            {
                "parent_release_id": previous_release_id,
                "projection_version": PROJECTION_VERSION,
                "hash_algorithm_version": HASH_ALGORITHM_VERSION,
                "target_inns": target_inns,
            }
        )
    ).hexdigest()
    release_id = release_id_for(now, source_sha, target_identity_hash)
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
    current: dict[int, PublicProjection] = {}
    with psycopg.connect(database_url_for_psycopg(), row_factory=dict_row) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        with connection.cursor() as cursor:
            for company in target_companies:
                current[company.id] = build_projection(
                    cursor, company.inn, base_publication
                )

    projections: list[PublicProjection] = []
    current_hashes: dict[str, str] = {}
    for company in target_companies:
        projection = _release_projection(
            current[company.id], release_id=release_id, published_at=now
        )
        scan_forbidden(projection.model_dump(mode="json"))
        current_hashes[company.inn] = semantic_projection_sha256(projection)
        projections.append(projection)

    unchanged: list[ReleaseTransitionSummary] = []
    updated: list[ReleaseTransitionSummary] = []
    added: list[ReleaseTransitionSummary] = []
    withdrawn: list[ReleaseTransitionSummary] = []
    for inn in parent.retained_inns:
        company = by_inn[inn]
        state = states.get(company.id)
        versions_match = bool(
            state
            and state.projection_version == PROJECTION_VERSION
            and state.hash_algorithm_version == HASH_ALGORITHM_VERSION
        )
        transition = ReleaseTransitionSummary(
            inn=inn,
            transition=(
                "UNCHANGED"
                if versions_match
                and parent.previous_hashes[inn] == current_hashes[inn]
                else "UPDATED"
            ),
            previous_hash=parent.previous_hashes[inn],
            current_hash=current_hashes[inn],
        )
        (unchanged if transition.transition == "UNCHANGED" else updated).append(
            transition
        )
    for inn in parent.added_inns:
        added.append(
            ReleaseTransitionSummary(
                inn=inn,
                transition="ADDED",
                current_hash=current_hashes[inn],
            )
        )
    for inn in parent.withdrawn_inns:
        withdrawn.append(
            ReleaseTransitionSummary(
                inn=inn,
                transition="WITHDRAWN",
                previous_hash=parent.previous_hashes[inn],
            )
        )

    release_changes = [*updated, *added, *withdrawn]
    if not release_changes:
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
    if inns != target_inns or len(set(inns)) != len(inns):
        raise PublicBuildError("candidate is not the exact current-ready target cohort")
    content_updated_at = max(item.publication.content_updated_at for item in projections)
    target_changes = [*updated, *added]
    changes = [
        ChangedCompanySummary(
            inn=item.inn,
            previous_hash=item.previous_hash,
            current_hash=item.current_hash,
        )
        for item in target_changes
    ]
    release_manifest = ReleaseManifest(
        schema_version="public-projection-v1",
        projection_version=PROJECTION_VERSION,
        hash_algorithm_version=HASH_ALGORITHM_VERSION,
        transition_model_version="release-transition-v1",
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
        parent_record_count=parent.record_count,
        unchanged_count=len(unchanged),
        unchanged_companies=tuple(unchanged),
        updated_count=len(updated),
        updated_companies=tuple(updated),
        added_count=len(added),
        added_companies=tuple(added),
        withdrawn_count=len(withdrawn),
        withdrawn_companies=tuple(withdrawn),
    )
    bundle_dir = output_root / release_id
    release_manifest = _write_bundle(bundle_dir, release_manifest, projections)
    request.candidate_release_id = release_id
    request.previous_release_id = previous_release_id
    request.projection_generation = release_id
    request.projection_hash = hashlib.sha256(
        canonical_json([item.model_dump(mode="json") for item in release_changes])
    ).hexdigest()
    company_id_by_inn = {company.inn: company.id for company in companies}
    request.changed_company_count = len(release_changes)
    request.changed_company_inns = [item.inn for item in release_changes]
    request.changed_company_ids = [
        company_id_by_inn[item.inn] for item in release_changes
    ]
    run_by_inn = {
        company.inn: runs[company.id]
        for company in companies
        if company.id in runs
    }
    request.change_summary = []
    for item in release_changes:
        summary = item.model_dump(mode="json")
        run = run_by_inn.get(item.inn)
        if run is not None:
            summary["enrichment_run_id"] = str(run.id)
        request.change_summary.append(summary)
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
        for transition in manifest.withdrawn_companies:
            html = http.get(f"/companies/{transition.inn}")
            api = http.get(f"/api/company/{transition.inn}")
            if html.status_code != 404 or api.status_code != 404:
                raise PublicVpsUnavailable(
                    f"withdrawn public card is still served: {transition.inn}"
                )
    except (httpx.HTTPError, ValueError, TypeError, json.JSONDecodeError) as error:
        if isinstance(error, PublicVpsUnavailable):
            raise
        raise PublicVpsUnavailable(type(error).__name__) from error
    finally:
        if owns_client:
            http.close()


def _public_incident(session, *, category: str, message: object, owner: str) -> None:
    technical_message = safe_text(message)
    if category == "PUBLIC_VPS_UNAVAILABLE":
        incident = session.scalar(
            select(SourceIncident)
            .where(
                SourceIncident.source_id == "public_sync",
                SourceIncident.category == category,
                SourceIncident.status.not_in(TERMINAL_STATUSES),
            )
            .order_by(SourceIncident.updated_at.desc())
            .limit(1)
        )
        if incident is not None:
            incident.updated_at = utc_now()
            incident.safe_error_message = technical_message
            incident.resolution_evidence = {
                **(incident.resolution_evidence or {}),
                "home_verification_error": technical_message,
            }
            return
    incident, _created = upsert_incident(
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
        message=(
            "HOME public HTTPS verification unavailable"
            if category == "PUBLIC_VPS_UNAVAILABLE"
            else technical_message
        ),
    )
    if category == "PUBLIC_VPS_UNAVAILABLE":
        incident.safe_error_message = technical_message
        incident.resolution_evidence = {
            **(incident.resolution_evidence or {}),
            "home_verification_error": technical_message,
        }


def _resolve_public_incidents(
    session,
    *,
    now: datetime,
    categories: set[str] | None = None,
    resolution: str = "public release published and verified over HTTPS",
) -> None:
    query = select(SourceIncident).where(
        SourceIncident.source_id == "public_sync",
        SourceIncident.status.not_in(TERMINAL_STATUSES),
    )
    if categories:
        query = query.where(SourceIncident.category.in_(categories))
    rows = list(session.scalars(query))
    for row in rows:
        row.status = "RESOLVED"
        row.resolved_at = now
        row.updated_at = now
        row.resolution = resolution
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
    states = {
        row.company_id: row
        for row in session.scalars(
            select(PublicProjectionPublication).where(
                PublicProjectionPublication.company_id.in_(
                    [company.id for company in companies]
                )
            )
        )
    }
    published_run_ids = {
        item["inn"]: UUID(str(item["enrichment_run_id"]))
        for item in (request.change_summary or [])
        if item.get("enrichment_run_id")
    }
    for projection in projections:
        company = by_inn[projection.company.inn]
        state = states.get(company.id)
        if state is None:
            state = PublicProjectionPublication(
                company_id=company.id,
                last_published_hash=semantic_projection_sha256(projection),
                projection_version=manifest.projection_version,
                hash_algorithm_version=manifest.hash_algorithm_version,
                is_published=True,
                last_published_release_id=manifest.release_id,
                published_at=now,
                updated_at=now,
            )
            session.add(state)
            states[company.id] = state
        published_run_id = published_run_ids.get(company.inn)
        if published_run_id is not None:
            published_run = session.get(CompanyEnrichmentRun, published_run_id)
            if published_run is None or published_run.company_id != company.id:
                raise PublicBuildError("published enrichment run evidence is invalid")
        state.last_published_hash = semantic_projection_sha256(projection)
        state.projection_version = manifest.projection_version
        state.hash_algorithm_version = manifest.hash_algorithm_version
        state.is_published = True
        state.last_published_release_id = manifest.release_id
        if published_run_id is not None:
            state.last_enrichment_run_id = published_run_id
        state.published_at = now
        state.updated_at = now
    for transition in manifest.withdrawn_companies:
        company = by_inn[transition.inn]
        state = states.get(company.id)
        if state is None:
            raise PublicBuildError("withdrawn projection baseline disappeared")
        state.last_published_hash = transition.previous_hash or state.last_published_hash
        state.projection_version = manifest.projection_version
        state.hash_algorithm_version = manifest.hash_algorithm_version
        state.is_published = False
        state.last_published_release_id = manifest.release_id
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
    try:
        manifest, _ = accepted_cohort()
        return fetch_live_release_id(manifest, client=client)
    except Exception:
        return None


def _validated_candidate_parent(
    request: PublicPublicationRequest,
    manifest: ReleaseManifest,
    *,
    client: httpx.Client | None,
) -> str:
    accepted, _ = accepted_cohort()
    live_release_id = fetch_live_release_id(accepted, client=client)
    if (
        request.previous_release_id != live_release_id
        or manifest.previous_release_id != live_release_id
    ):
        raise StaleCandidateParent(
            expected=request.previous_release_id or manifest.previous_release_id,
            actual=live_release_id,
        )
    return live_release_id


class StaleCandidateParent(RuntimeError):
    def __init__(self, *, expected: str | None, actual: str) -> None:
        super().__init__(f"candidate parent {expected or 'missing'} != live {actual}")
        self.expected = expected
        self.actual = actual


def _rebase_stale_candidate(
    session,
    *,
    client: httpx.Client | None,
) -> str:
    result = normalize_publication_queue(
        session,
        client=client,
        force=True,
        reason_override=STALE_PARENT_RELEASE,
    )
    session.commit()
    return "NO_PUBLIC_CHANGE" if result.no_public_change else "REBASED"


def publish_claimed_request(
    session,
    request: PublicPublicationRequest,
    *,
    output_root: Path,
    transport: SshPublicTransport,
    client: httpx.Client | None = None,
) -> str:
    now = utc_now()
    promotion_attempted = False
    try:
        if request.candidate_release_id:
            bundle_dir = output_root / request.candidate_release_id
            manifest, _projections, _sha = load_bundle(bundle_dir)
        else:
            bundle_dir, manifest = build_candidate(
                session,
                request,
                now=now,
                output_root=output_root,
                trusted_projection_reader=transport.read_active_projections,
                client=client,
            )
            session.commit()
            if bundle_dir is None or manifest is None:
                return "NO_PUBLIC_CHANGE"
        _validated_candidate_parent(request, manifest, client=client)
        request.status = "UPLOADING"
        request.updated_at = utc_now()
        session.commit()
        transport.upload(bundle_dir, manifest.release_id)
        request.uploaded_at = utc_now()
        session.commit()
        _validated_candidate_parent(request, manifest, client=client)
        request.status = "IMPORTING"
        request.updated_at = request.uploaded_at
        session.commit()
        staged_protocol = hasattr(transport, "accept_release") and hasattr(
            transport, "promote_release"
        )
        if not staged_protocol:
            # Legacy transports perform the pointer switch inside import_release.
            promotion_attempted = True
        transport.import_release(manifest.release_id)
        request.imported_at = utc_now()
        request.status = "VERIFYING"
        request.updated_at = request.imported_at
        session.commit()
        if staged_protocol:
            transport.accept_release(manifest.release_id)
        _validated_candidate_parent(request, manifest, client=client)
        if staged_protocol:
            # If the remote command completes but its response is lost, rollback is
            # still the safe recovery whenever live state cannot be read back.
            promotion_attempted = True
            transport.promote_release(manifest.release_id)
        verify_https_release(bundle_dir, manifest, client=client)
        completed = utc_now()
        try:
            _persist_published_hashes(session, request, bundle_dir, now=completed)
            _resolve_public_incidents(session, now=completed)
            session.commit()
        except Exception as error:
            raise PublicVpsUnavailable(
                f"verified release state persistence failed: {type(error).__name__}"
            ) from error
        try:
            normalize_after_successful_publication(
                session,
                published_request=request,
                now=completed,
            )
            session.commit()
        except Exception as cleanup_error:
            session.rollback()
            _public_incident(
                session,
                category="PUBLIC_BUILD_ERROR",
                message=f"post-publication queue cleanup failed: {cleanup_error}",
                owner="OUR_CODE",
            )
            session.commit()
        return "PUBLISHED"
    except StaleCandidateParent:
        session.rollback()
        return _rebase_stale_candidate(session, client=client)
    except PublicTransportError as error:
        session.rollback()
        request = session.get(PublicPublicationRequest, request.id)
        active = _active_release_id(client)
        if (
            request
            and request.candidate_release_id
            and promotion_attempted
            and active in {None, request.candidate_release_id}
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
                category=(
                    "PUBLIC_IMPORT_ERROR"
                    if error.stage in {"import", "acceptance", "promotion", "rollback"}
                    else "PUBLIC_UPLOAD_ERROR"
                ),
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
            and promotion_attempted
            and active in {None, request.candidate_release_id}
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
                try:
                    scan = scan_public_ready_changes(session)
                except PublicVpsUnavailable as error:
                    session.rollback()
                    recovered = recover_interrupted_requests(session)
                    _public_incident(
                        session,
                        category="PUBLIC_VPS_UNAVAILABLE",
                        message=error,
                        owner="OUR_INFRASTRUCTURE",
                    )
                    session.commit()
                    return {
                        "status": "VPS_UNAVAILABLE",
                        "recovered": recovered,
                        "scan": None,
                    }
                except Exception as error:
                    session.rollback()
                    recovered = recover_interrupted_requests(session)
                    _public_incident(
                        session,
                        category="PUBLIC_BUILD_ERROR",
                        message=error,
                        owner="OUR_CODE",
                    )
                    session.commit()
                    return {"status": "FAILED", "recovered": recovered, "scan": None}
                # Recovery and the readiness scan are legitimate state transitions.
                # Commit them before the network-dependent normalization transaction
                # so a normalization rollback cannot erase already completed work.
                session.commit()
                try:
                    recover_failed_publication_if_eligible(
                        session,
                        client=client,
                    )
                    normalization = normalize_publication_queue(
                        session,
                        client=client,
                    )
                except PublicVpsUnavailable as error:
                    session.rollback()
                    _public_incident(
                        session,
                        category="PUBLIC_VPS_UNAVAILABLE",
                        message=error,
                        owner="OUR_INFRASTRUCTURE",
                    )
                    session.commit()
                    return {
                        "status": "VPS_UNAVAILABLE",
                        "recovered": recovered,
                        "scan": scan,
                        "normalization": None,
                    }
                except PublicBuildError as error:
                    session.rollback()
                    _public_incident(
                        session,
                        category="PUBLIC_BUILD_ERROR",
                        message=error,
                        owner="OUR_CODE",
                    )
                    session.commit()
                    return {
                        "status": "FAILED",
                        "recovered": recovered,
                        "scan": scan,
                        "normalization": None,
                        "error": type(error).__name__,
                    }
                except Exception as error:
                    session.rollback()
                    logger.exception("public sync queue normalization failed")
                    try:
                        _public_incident(
                            session,
                            category="PUBLIC_BUILD_ERROR",
                            message=error,
                            owner="OUR_CODE",
                        )
                        session.commit()
                    except Exception:
                        session.rollback()
                        logger.exception("could not persist normalization failure incident")
                    return {
                        "status": "FAILED",
                        "recovered": recovered,
                        "scan": scan,
                        "normalization": None,
                        "error": type(error).__name__,
                    }
                _resolve_public_incidents(
                    session,
                    now=utc_now(),
                    categories={"PUBLIC_VPS_UNAVAILABLE"},
                    resolution="public HTTPS scan and queue normalization succeeded",
                )
                session.commit()
                request = (
                    session.get(
                        PublicPublicationRequest,
                        normalization.replacement_request_id,
                    )
                    if normalization.replacement_request_id
                    else claim_next_request(
                        session,
                        debounce_seconds=debounce_seconds,
                    )
                )
                if request is None:
                    session.commit()
                    return {
                        "status": (
                            "NO_PUBLIC_CHANGE"
                            if normalization.no_public_change
                            else "IDLE"
                        ),
                        "recovered": recovered,
                        "scan": scan,
                        "normalization": {
                            "active": normalization.active_count,
                            "superseded": normalization.superseded_count,
                            "dirty": normalization.dirty_count,
                            "live_release_id": normalization.live_release_id,
                        },
                    }
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
                    "normalization": {
                        "active": normalization.active_count,
                        "superseded": normalization.superseded_count,
                        "dirty": normalization.dirty_count,
                        "live_release_id": normalization.live_release_id,
                    },
                }
            finally:
                lock_connection.exec_driver_sql(
                    "SELECT pg_advisory_unlock(%s)", (PUBLIC_SYNC_LOCK_ID,)
                )


def _record_unexpected_cycle_incident(error: Exception) -> None:
    try:
        with SessionLocal() as session:
            _public_incident(
                session,
                category="PUBLIC_BUILD_ERROR",
                message=error,
                owner="OUR_CODE",
            )
            session.commit()
    except Exception:
        logger.exception("could not persist unexpected public-sync cycle incident")


def _run_loop(
    *,
    once: bool,
    poll_seconds: int,
    debounce_seconds: int,
    output_root: Path | None,
    cycle=None,
    sleep=None,
    max_cycles: int | None = None,
) -> int:
    cycle = cycle or run_once
    sleep = sleep or time.sleep
    consecutive_failures = 0
    cycle_count = 0
    while True:
        try:
            result = cycle(
                debounce_seconds=debounce_seconds,
                output_root=output_root,
            )
        except Exception as error:
            logger.exception("unexpected public-sync cycle failure")
            _record_unexpected_cycle_incident(error)
            result = {
                "status": "FAILED",
                "error": type(error).__name__,
                "message": safe_text(error),
            }
        logger.info("public sync cycle: %s", json.dumps(result, ensure_ascii=False, default=str))
        failed = result["status"] in {"FAILED", "VPS_UNAVAILABLE"}
        if once:
            print(json.dumps(result, ensure_ascii=False, default=str))
            return 1 if failed else 0
        consecutive_failures = consecutive_failures + 1 if failed else 0
        cycle_count += 1
        if max_cycles is not None and cycle_count >= max_cycles:
            return 1 if failed else 0
        normal_delay = min(60, max(5, poll_seconds))
        failure_delay = min(300, 15 * (2 ** min(5, max(0, consecutive_failures - 1))))
        sleep(max(normal_delay, failure_delay) if failed else normal_delay)


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
    return _run_loop(
        once=args.once,
        poll_seconds=args.poll_seconds,
        debounce_seconds=args.debounce_seconds,
        output_root=args.output_root,
    )


if __name__ == "__main__":
    raise SystemExit(main())
