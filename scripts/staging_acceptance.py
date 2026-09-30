#!/usr/bin/env python3
"""Run the fail-closed STAGING-PARITY-01 acceptance gate.

This command is intended for the isolated staging worker. It never connects to
production by discovery, never ingests source datasets and never rebuilds the
candidate. All databases it creates have a random sp01_ prefix and are removed
unless --keep-databases is explicitly requested.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path
from typing import Any

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.release_artifact import (  # noqa: E402
    ArtifactError,
    canonical_json,
    sha256_file,
    verify_artifact,
)
from scripts.render_staging_units import (  # noqa: E402
    UnitParityError,
    check_units,
    render_units,
)
from scripts.schema_fingerprint import (  # noqa: E402
    EXPECTED_POSTGRESQL_VERSION,
    FingerprintMismatch,
    fingerprint_connection,
    require_fingerprint_match,
    write_fingerprint,
)


CONFIRMATION = "STAGING-PARITY-01"
DATABASE_PREFIX = "sp01_"
PROTECTED_DATABASES = {
    "kontragent",
    "nextcompany_operational",
    "nextcompany_public",
}
SAFE_SEMANTIC_MODULES = {
    "scripts.import_public_release",
    "scripts.recover_post_migration_factory",
}


class AcceptanceError(RuntimeError):
    """A mandatory staging gate failed."""


def _redact(value: str) -> str:
    return re.sub(
        r"(postgresql(?:\+psycopg)?://[^:/@\s]+:)[^@\s]+@",
        r"\1***@",
        value,
    )


def _libpq_url(value: str) -> str:
    return value.replace("postgresql+psycopg://", "postgresql://", 1)


def _database_url(admin_url: str, database: str) -> str:
    url = make_url(admin_url).set(database=database)
    return url.render_as_string(hide_password=False)


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise AcceptanceError(f"cannot read manifest {path}: {error}") from error
    if not isinstance(value, dict):
        raise AcceptanceError(f"manifest must be an object: {path}")
    return value


def _resolve_file(manifest_path: Path, value: str) -> Path:
    path = Path(value)
    if not path.is_absolute():
        path = manifest_path.parent / path
    return path.resolve()


def _verified_file(manifest_path: Path, specification: dict[str, Any]) -> Path:
    if not {"path", "sha256"} <= set(specification):
        raise AcceptanceError(f"backup specification is incomplete in {manifest_path}")
    path = _resolve_file(manifest_path, str(specification["path"]))
    if not path.is_file():
        raise AcceptanceError(f"required file is missing: {path}")
    observed = sha256_file(path)
    if observed != specification["sha256"]:
        raise AcceptanceError(
            f"checksum mismatch for {path.name}: expected {specification['sha256']}, "
            f"observed {observed}"
        )
    return path


def validate_previous_manifest(path: Path) -> dict[str, Any]:
    value = _json(path)
    required = {
        "contract_version",
        "accepted_artifact",
        "operational_backup",
        "public_backup",
        "rollback",
    }
    if value.get("contract_version") != 1 or not required <= set(value):
        raise AcceptanceError("previous-release manifest contract is incomplete")
    artifact = _verified_file(path, value["accepted_artifact"])
    checksum_sidecar = artifact.with_name(artifact.name + ".sha256")
    if not checksum_sidecar.is_file():
        raise AcceptanceError("previous accepted artifact checksum sidecar is missing")
    operational = _verified_file(path, value["operational_backup"])
    public = _verified_file(path, value["public_backup"])
    rollback = value["rollback"]
    if not isinstance(rollback, dict) or "db_downgrade_supported" not in rollback:
        raise AcceptanceError("rollback contract is missing")
    if (
        not rollback["db_downgrade_supported"]
        and not str(rollback.get("forward_recovery", "")).strip()
    ):
        raise AcceptanceError(
            "a migration without downgrade support requires a forward-recovery rule"
        )
    return {
        **value,
        "_manifest_path": path,
        "_artifact_path": artifact,
        "_operational_backup_path": operational,
        "_public_backup_path": public,
    }


def validate_shape_manifest(path: Path) -> dict[str, Any]:
    value = _json(path)
    required = {
        "contract_version",
        "sanitized",
        "contains_sensitive_content",
        "mass_ingestion",
        "operational_backup",
        "public_backup",
        "minimum_rows",
        "semantic_release_checks",
        "semantic_release_check_waiver",
    }
    if value.get("contract_version") != 1 or not required <= set(value):
        raise AcceptanceError("production-shape manifest contract is incomplete")
    if value["sanitized"] is not True:
        raise AcceptanceError("production-shape input is not declared sanitized")
    if value["contains_sensitive_content"] is not False:
        raise AcceptanceError("production-shape input may contain sensitive content")
    if value["mass_ingestion"] is not False:
        raise AcceptanceError("production-shape lane must not run mass ingestion")
    minimum_rows = value["minimum_rows"]
    if not isinstance(minimum_rows, dict) or not {
        "operational",
        "public",
    } <= set(minimum_rows):
        raise AcceptanceError("production-shape cardinality contract is missing")
    checks = value["semantic_release_checks"]
    waiver = str(value["semantic_release_check_waiver"]).strip()
    if not isinstance(checks, list) or (not checks and not waiver):
        raise AcceptanceError(
            "production-shape semantic release checks require commands or a waiver"
        )
    return {
        **value,
        "_manifest_path": path,
        "_operational_backup_path": _verified_file(path, value["operational_backup"]),
        "_public_backup_path": _verified_file(path, value["public_backup"]),
    }


class DatabaseFactory:
    def __init__(self, admin_url: str, token: str):
        self.admin_url = _libpq_url(admin_url)
        self.token = token
        self.created: list[str] = []
        parsed = make_url(admin_url)
        if parsed.database not in {"postgres", "template1"}:
            raise AcceptanceError(
                "STAGING_DB_ADMIN_URL must target postgres or template1"
            )

    def check_server(self) -> dict[str, Any]:
        with psycopg.connect(self.admin_url) as connection:
            row = connection.execute(
                "SELECT current_database(), current_user, "
                "current_setting('server_version'), "
                "current_setting('server_version_num')::int, "
                "current_setting('nextcompany.environment', true)"
            ).fetchone()
        version = row[2]
        if version.split()[0] != EXPECTED_POSTGRESQL_VERSION:
            raise AcceptanceError(
                f"PostgreSQL version mismatch: expected "
                f"{EXPECTED_POSTGRESQL_VERSION}, observed {version}"
            )
        if row[4] != "staging":
            raise AcceptanceError(
                "PostgreSQL cluster is not marked nextcompany.environment=staging"
            )
        return {
            "database": row[0],
            "role": row[1],
            "postgresql_version": version,
            "postgresql_version_num": row[3],
            "environment_marker": row[4],
        }

    def create(self, label: str) -> str:
        safe_label = re.sub(r"[^a-z0-9_]", "_", label.lower())[:24]
        name = f"{DATABASE_PREFIX}{self.token}_{safe_label}"
        if name in PROTECTED_DATABASES or not name.startswith(DATABASE_PREFIX):
            raise AcceptanceError("unsafe acceptance database name")
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(name)))
        self.created.append(name)
        return _database_url(self.admin_url, name)

    def drop_all(self) -> None:
        with psycopg.connect(self.admin_url, autocommit=True) as connection:
            for name in reversed(self.created):
                if not name.startswith(DATABASE_PREFIX) or name in PROTECTED_DATABASES:
                    raise AcceptanceError(f"refusing unsafe database cleanup: {name}")
                connection.execute(
                    "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                    "WHERE datname = %s AND pid <> pg_backend_pid()",
                    (name,),
                )
                connection.execute(
                    sql.SQL("DROP DATABASE IF EXISTS {}").format(sql.Identifier(name))
                )
        self.created.clear()


def _run(
    command: list[str],
    *,
    cwd: Path,
    environment: dict[str, str] | None = None,
    output: Path | None = None,
) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        command,
        cwd=cwd,
        env={**os.environ, **(environment or {})},
        text=True,
        capture_output=True,
        check=False,
    )
    if output:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            _redact("$ " + " ".join(command) + "\n" + result.stdout + result.stderr),
            encoding="utf-8",
        )
    if result.returncode:
        raise AcceptanceError(
            f"command failed ({command[0]}): "
            f"{_redact((result.stderr or result.stdout)[-1000:])}"
        )
    return result


def migration_command(release: Path, lane: str) -> list[str]:
    if lane not in {"operational", "public"}:
        raise AcceptanceError(f"unknown migration lane: {lane}")
    return [
        str(release / "deploy/scripts/run_release_migrations.sh"),
        str(release),
        lane,
    ]


def migrate(
    release: Path,
    lane: str,
    database_url: str,
    *,
    evidence_path: Path,
) -> None:
    variable = "DATABASE_URL" if lane == "operational" else "PUBLIC_IMPORT_DATABASE_URL"
    _run(
        migration_command(release, lane),
        cwd=release,
        environment={variable: database_url},
        output=evidence_path,
    )


def schema_fingerprint(database_url: str, path: Path) -> dict[str, Any]:
    with psycopg.connect(_libpq_url(database_url)) as connection:
        result = fingerprint_connection(connection)
    write_fingerprint(result, path)
    return result


def _restore(database_url: str, backup: Path, log: Path) -> None:
    _run(
        [
            "pg_restore",
            "--no-owner",
            "--no-acl",
            "--exit-on-error",
            "--dbname",
            _libpq_url(database_url),
            str(backup),
        ],
        cwd=ROOT,
        output=log,
    )


def _backup(database_url: str, backup: Path, log: Path) -> str:
    _run(
        [
            "pg_dump",
            "--format=custom",
            "--no-owner",
            "--no-acl",
            "--file",
            str(backup),
            _libpq_url(database_url),
        ],
        cwd=ROOT,
        output=log,
    )
    checksum = sha256_file(backup)
    backup.with_name(backup.name + ".sha256").write_text(
        f"{checksum}  {backup.name}\n", encoding="utf-8"
    )
    return checksum


def _client_version(command: str) -> str:
    value = subprocess.check_output([command, "--version"], text=True).strip()
    if EXPECTED_POSTGRESQL_VERSION not in value:
        raise AcceptanceError(
            f"{command} divergence: expected {EXPECTED_POSTGRESQL_VERSION}, "
            f"observed {value}"
        )
    return value


def _count_contract(
    database_url: str,
    minimum_rows: dict[str, Any],
) -> dict[str, int]:
    results = {}
    with psycopg.connect(_libpq_url(database_url)) as connection:
        for qualified, minimum in sorted(minimum_rows.items()):
            parts = qualified.split(".")
            if len(parts) != 2 or not all(
                re.fullmatch(r"[a-z_][a-z0-9_]*", part) for part in parts
            ):
                raise AcceptanceError(f"unsafe cardinality object: {qualified}")
            value = connection.execute(
                sql.SQL("SELECT count(*) FROM {}.{}").format(
                    sql.Identifier(parts[0]), sql.Identifier(parts[1])
                )
            ).fetchone()[0]
            results[qualified] = value
            if value < int(minimum):
                raise AcceptanceError(
                    f"production-shape cardinality below contract for {qualified}: "
                    f"{value} < {minimum}"
                )
    return results


def _semantic_release_checks(
    release: Path,
    shape: dict[str, Any],
    operational_url: str,
    public_url: str,
    evidence_dir: Path,
) -> dict[str, Any]:
    checks = shape["semantic_release_checks"]
    if not checks:
        return {
            "status": "WAIVED",
            "reason": shape["semantic_release_check_waiver"],
        }
    results = []
    for index, specification in enumerate(checks, start=1):
        if not isinstance(specification, dict):
            raise AcceptanceError("semantic release check must be an object")
        module = specification.get("module")
        arguments = specification.get("arguments", [])
        if module not in SAFE_SEMANTIC_MODULES:
            raise AcceptanceError(f"semantic release module is not allowed: {module}")
        if not isinstance(arguments, list) or not all(
            isinstance(value, str) for value in arguments
        ):
            raise AcceptanceError("semantic release arguments must be strings")
        _run(
            [
                str(release / ".venv/bin/python"),
                "-m",
                module,
                *arguments,
            ],
            cwd=release,
            environment={
                "DATABASE_URL": operational_url,
                "PUBLIC_DATABASE_URL": public_url,
                "PUBLIC_IMPORT_DATABASE_URL": public_url,
            },
            output=evidence_dir / f"shape-semantic-{index}.log",
        )
        results.append(
            {
                "status": "PASS",
                "module": module,
                "argument_count": len(arguments),
            }
        )
    return {"status": "PASS", "checks": results}


def _mutation_target(fingerprint: dict[str, Any], kind: str) -> dict[str, Any]:
    payload = fingerprint["payload"]
    if kind == "table":
        candidates = [
            row
            for row in payload["relations"]
            if row["kind"] in {"table", "partitioned_table"}
            and row["name"] != "alembic_version"
        ]
    elif kind == "column":
        candidates = [
            row
            for row in payload["columns"]
            if row["relation"] != "alembic_version"
        ]
    elif kind == "index":
        candidates = [
            row
            for row in payload["indexes"]
            if not row["primary"] and not row["constraint_backed"]
        ]
    elif kind == "constraint":
        candidates = [
            *payload["check_constraints"],
            *payload["foreign_keys"],
            *[
                row
                for row in payload["unique_constraints"]
                if not row["definition"].startswith("PRIMARY KEY")
            ],
        ]
    else:
        raise AcceptanceError(f"unknown false-head mutation: {kind}")
    if not candidates:
        raise AcceptanceError(f"no {kind} is available for false-head proof")
    return candidates[0]


def false_head_probe(
    database_url: str,
    expected: dict[str, Any],
    *,
    kinds: tuple[str, ...] = ("table", "column", "index", "constraint"),
) -> dict[str, Any]:
    results = {}
    for kind in kinds:
        target = _mutation_target(expected, kind)
        with psycopg.connect(_libpq_url(database_url)) as connection:
            before_revisions = expected["payload"]["migration_revisions"]
            if kind == "table":
                command = sql.SQL("DROP TABLE {}.{} CASCADE").format(
                    sql.Identifier(target["schema"]), sql.Identifier(target["name"])
                )
            elif kind == "column":
                command = sql.SQL("ALTER TABLE {}.{} DROP COLUMN {} CASCADE").format(
                    sql.Identifier(target["schema"]),
                    sql.Identifier(target["relation"]),
                    sql.Identifier(target["name"]),
                )
            elif kind == "index":
                command = sql.SQL("DROP INDEX {}.{} CASCADE").format(
                    sql.Identifier(target["schema"]), sql.Identifier(target["name"])
                )
            else:
                command = sql.SQL(
                    "ALTER TABLE {}.{} DROP CONSTRAINT {} CASCADE"
                ).format(
                    sql.Identifier(target["schema"]),
                    sql.Identifier(target["relation"]),
                    sql.Identifier(target["name"]),
                )
            connection.execute(command)
            observed = fingerprint_connection(connection)
            if observed["payload"]["migration_revisions"] != before_revisions:
                raise AcceptanceError("false-head probe changed Alembic revision")
            try:
                require_fingerprint_match(expected, observed)
            except FingerprintMismatch:
                results[kind] = {
                    "status": "PASS",
                    "target": {
                        key: target[key]
                        for key in ("schema", "relation", "name")
                        if key in target
                    },
                    "revision_unchanged": True,
                }
            else:
                raise AcceptanceError(
                    f"removed {kind} was accepted while revision remained HEAD"
                )
            connection.rollback()
    return results


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _wait_json(url: str, process: subprocess.Popen[str], *, ready: bool = False) -> dict:
    deadline = time.monotonic() + 45
    last_error = ""
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise AcceptanceError(f"runtime exited before health: {url}")
        try:
            with urllib.request.urlopen(url, timeout=3) as response:
                payload = json.loads(response.read())
                if response.status == 200 and (
                    not ready or payload.get("status") == "ready"
                ):
                    return payload
                last_error = f"HTTP {response.status}: {payload}"
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = str(error)
        time.sleep(0.5)
    raise AcceptanceError(f"runtime health timeout for {url}: {last_error}")


@contextlib.contextmanager
def runtime_pair(
    release: Path,
    operational_url: str,
    public_url: str,
    evidence_dir: Path,
    label: str,
):
    operational_port = _free_port()
    public_port = _free_port()
    environment = {
        **os.environ,
        "DATABASE_URL": operational_url,
        "PUBLIC_DATABASE_URL": public_url,
        "PUBLIC_IMPORT_DATABASE_URL": public_url,
        "PUBLIC_ORIGIN": f"http://127.0.0.1:{public_port}",
        "PUBLIC_TRUSTED_HOSTS": "127.0.0.1,localhost",
        "PUBLIC_FORCE_NOINDEX": "1",
        "DADATA_API_KEY": "staging-disabled",
    }
    commands = {
        "operational": [
            str(release / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(operational_port),
        ],
        "public": [
            str(release / ".venv/bin/python"),
            "-m",
            "uvicorn",
            "public_app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(public_port),
        ],
    }
    logs = {}
    processes = {}
    try:
        for name, command in commands.items():
            log = (evidence_dir / f"{label}-{name}.log").open("w", encoding="utf-8")
            logs[name] = log
            processes[name] = subprocess.Popen(
                command,
                cwd=release,
                env=environment,
                stdout=log,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
        yield {
            "operational_process": processes["operational"],
            "public_process": processes["public"],
            "operational_health": f"http://127.0.0.1:{operational_port}/api/health",
            "public_health": f"http://127.0.0.1:{public_port}/api/health",
            "public_ready": f"http://127.0.0.1:{public_port}/api/ready",
            "public_origin": f"http://127.0.0.1:{public_port}",
        }
    finally:
        for process in processes.values():
            if process.poll() is None:
                os.killpg(process.pid, signal.SIGTERM)
        for process in processes.values():
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.wait(timeout=5)
        for log in logs.values():
            log.close()


def _http_text(url: str) -> tuple[int, dict[str, str], str]:
    with urllib.request.urlopen(url, timeout=5) as response:
        return response.status, dict(response.headers.items()), response.read().decode()


def _header_value(headers: dict[str, str], name: str) -> str | None:
    """Return one HTTP header value using case-insensitive field-name rules."""

    values = [
        value
        for key, value in headers.items()
        if key.casefold() == name.casefold()
    ]
    return values[0] if len(values) == 1 else None


def _active_release(database_url: str) -> str | None:
    with psycopg.connect(_libpq_url(database_url)) as connection:
        exists = connection.execute(
            "SELECT to_regclass('public.public_publication_state')"
        ).fetchone()[0]
        if not exists:
            return None
        row = connection.execute(
            "SELECT active_release_id FROM public_publication_state "
            "WHERE singleton = TRUE"
        ).fetchone()
        return row[0] if row else None


def rollback_mode(
    previous_runtime_healthy: bool,
    rollback_contract: dict[str, Any],
) -> str:
    if previous_runtime_healthy:
        return "previous_runtime"
    if rollback_contract.get("db_downgrade_supported"):
        raise AcceptanceError(
            "previous runtime is incompatible despite declared rollback support"
        )
    if not str(rollback_contract.get("forward_recovery", "")).strip():
        raise AcceptanceError("forward-recovery rule is required")
    return "forward_recovery"


def _runtime_accept(
    release: Path,
    operational_url: str,
    public_url: str,
    evidence_dir: Path,
    label: str,
    *,
    require_ready: bool,
) -> dict[str, Any]:
    with runtime_pair(
        release, operational_url, public_url, evidence_dir, label
    ) as runtime:
        operational = _wait_json(
            runtime["operational_health"], runtime["operational_process"]
        )
        public = _wait_json(runtime["public_health"], runtime["public_process"])
        ready = None
        if require_ready:
            ready = _wait_json(
                runtime["public_ready"], runtime["public_process"], ready=True
            )
        status, headers, landing = _http_text(runtime["public_origin"] + "/")
        _, _, robots = _http_text(runtime["public_origin"] + "/robots.txt")
        if (
            status != 200
            or _header_value(headers, "X-Robots-Tag")
            != "noindex, nofollow, nosnippet"
            or robots != "User-agent: *\nDisallow: /\n"
        ):
            raise AcceptanceError("staging noindex runtime contract failed")
        return {
            "status": "PASS",
            "operational_health": operational,
            "public_health": public,
            "public_ready": ready,
            "noindex": True,
            "landing_bytes": len(landing.encode()),
        }


def _access_contract(release: Path) -> dict[str, Any]:
    path = release / "deploy" / "nginx" / "nextcompany-staging.conf"
    text = path.read_text(encoding="utf-8")
    required = (
        "listen 127.0.0.1:18080",
        "auth_basic",
        "auth_basic_user_file",
        "proxy_pass http://127.0.0.1:18000",
        'add_header X-Robots-Tag "noindex, nofollow, nosnippet" always',
    )
    missing = [value for value in required if value not in text]
    if missing:
        raise AcceptanceError("staging access boundary is incomplete: " + ", ".join(missing))
    return {"status": "PASS", "listen": "127.0.0.1:18080", "authenticated": True}


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(canonical_json(result) + b"\n")


def run_acceptance(args: argparse.Namespace) -> dict[str, Any]:
    if os.getenv("STAGING_ACCEPTANCE_CONFIRM") != CONFIRMATION:
        raise AcceptanceError(
            f"set STAGING_ACCEPTANCE_CONFIRM={CONFIRMATION} on the isolated worker"
        )
    admin_url = os.getenv(args.admin_url_env)
    if not admin_url:
        raise AcceptanceError(f"{args.admin_url_env} is required")
    evidence_dir = args.evidence_dir.resolve()
    evidence_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "task": "STAGING-PARITY-01",
        "status": "FAIL",
        "postgresql_expected": EXPECTED_POSTGRESQL_VERSION,
        "gates": {},
    }
    token = uuid.uuid4().hex[:10]
    databases = DatabaseFactory(admin_url, token)
    work_context = (
        contextlib.nullcontext(args.work_dir.resolve())
        if args.work_dir
        else tempfile.TemporaryDirectory(prefix="staging-parity-")
    )
    try:
        postgres_identity = databases.check_server()
        result["postgresql"] = postgres_identity
        result["gates"]["postgresql"] = "PASS"
        result["postgresql_clients"] = {
            "pg_dump": _client_version("pg_dump"),
            "pg_restore": _client_version("pg_restore"),
        }
        previous = validate_previous_manifest(args.previous_release_manifest.resolve())
        shape = validate_shape_manifest(args.production_shape_manifest.resolve())
        with work_context as work_value:
            work = Path(work_value)
            candidate_extract = work / "candidate"
            artifact = verify_artifact(args.artifact, extract_to=candidate_extract)
            release = candidate_extract / "release"
            result["artifact"] = {
                key: artifact[key]
                for key in (
                    "artifact_sha256",
                    "source_git_sha",
                    "lock_sha256",
                    "runtime_tree_sha256",
                    "python_version",
                    "postgresql_version",
                )
            }
            result["gates"]["immutable_artifact"] = "PASS"
            units = evidence_dir / "generated-units"
            render_units(units, source_dir=release / "deploy/systemd")
            check_units(units, source_dir=release / "deploy/systemd")
            result["gates"]["systemd_parity"] = "PASS"
            result["access"] = _access_contract(release)
            result["gates"]["access_control"] = "PASS"

            fresh = {}
            for lane in ("operational", "public"):
                database_url = databases.create(f"{lane}_fresh")
                migrate(
                    release,
                    lane,
                    database_url,
                    evidence_path=evidence_dir / f"fresh-{lane}-migration.log",
                )
                fingerprint = schema_fingerprint(
                    database_url, evidence_dir / f"fresh-{lane}-fingerprint.json"
                )
                fresh[lane] = {
                    "url": database_url,
                    "fingerprint": fingerprint,
                }
            _run(
                [
                    str(release / ".venv/bin/python"),
                    str(release / "scripts/check_schema_completeness.py"),
                    "--output",
                    str(evidence_dir / "fresh-operational-model-audit.json"),
                ],
                cwd=release,
                environment={"DATABASE_URL": fresh["operational"]["url"]},
                output=evidence_dir / "fresh-operational-model-audit.log",
            )
            result["gates"]["fresh_operational"] = "PASS"
            result["gates"]["fresh_public"] = "PASS"
            result["fresh_fingerprints"] = {
                lane: fresh[lane]["fingerprint"]["fingerprint_sha256"]
                for lane in ("operational", "public")
            }

            result["false_head"] = {}
            for lane in ("operational", "public"):
                result["false_head"][lane] = false_head_probe(
                    fresh[lane]["url"], fresh[lane]["fingerprint"]
                )
            result["gates"]["false_head_detection"] = "PASS"

            upgraded = {}
            for lane in ("operational", "public"):
                database_url = databases.create(f"{lane}_upgrade")
                backup = previous[f"_{lane}_backup_path"]
                _restore(
                    database_url,
                    backup,
                    evidence_dir / f"previous-{lane}-restore.log",
                )
                with psycopg.connect(_libpq_url(database_url)) as connection:
                    before = fingerprint_connection(connection)["payload"][
                        "migration_revisions"
                    ]
                migrate(
                    release,
                    lane,
                    database_url,
                    evidence_path=evidence_dir / f"previous-{lane}-migration.log",
                )
                fingerprint = schema_fingerprint(
                    database_url, evidence_dir / f"previous-{lane}-fingerprint.json"
                )
                require_fingerprint_match(
                    fresh[lane]["fingerprint"],
                    fingerprint,
                    allowed_extra_schemas=previous.get(
                        f"{lane}_allowed_extra_schemas", []
                    ),
                )
                upgraded[lane] = {
                    "url": database_url,
                    "revisions_before": before,
                    "revisions_after": fingerprint["payload"][
                        "migration_revisions"
                    ],
                }
            result["previous_release"] = {
                lane: {
                    "revisions_before": upgraded[lane]["revisions_before"],
                    "revisions_after": upgraded[lane]["revisions_after"],
                }
                for lane in upgraded
            }
            result["gates"]["previous_operational_upgrade"] = "PASS"
            result["gates"]["previous_public_upgrade"] = "PASS"

            shaped = {}
            for lane in ("operational", "public"):
                database_url = databases.create(f"{lane}_shape")
                _restore(
                    database_url,
                    shape[f"_{lane}_backup_path"],
                    evidence_dir / f"shape-{lane}-restore.log",
                )
                migrate(
                    release,
                    lane,
                    database_url,
                    evidence_path=evidence_dir / f"shape-{lane}-migration.log",
                )
                fingerprint = schema_fingerprint(
                    database_url, evidence_dir / f"shape-{lane}-fingerprint.json"
                )
                require_fingerprint_match(
                    fresh[lane]["fingerprint"],
                    fingerprint,
                    allowed_extra_schemas=shape.get(
                        f"{lane}_allowed_extra_schemas", []
                    ),
                )
                shaped[lane] = {
                    "url": database_url,
                    "fingerprint": fingerprint,
                    "cardinality": _count_contract(
                        database_url, shape["minimum_rows"][lane]
                    ),
                }
            result["production_shape"] = {
                lane: {
                    "fingerprint_sha256": shaped[lane]["fingerprint"][
                        "fingerprint_sha256"
                    ],
                    "cardinality": shaped[lane]["cardinality"],
                }
                for lane in shaped
            }
            result["semantic_release_checks"] = _semantic_release_checks(
                release,
                shape,
                shaped["operational"]["url"],
                shaped["public"]["url"],
                evidence_dir,
            )
            result["gates"]["production_shape"] = "PASS"
            result["gates"]["schema_fingerprints"] = "PASS"

            backup_result = {}
            for lane in ("operational", "public"):
                backup = evidence_dir / f"acceptance-{lane}.dump"
                checksum = _backup(
                    shaped[lane]["url"],
                    backup,
                    evidence_dir / f"backup-{lane}.log",
                )
                restore_url = databases.create(f"{lane}_restore")
                _restore(
                    restore_url,
                    backup,
                    evidence_dir / f"backup-{lane}-restore.log",
                )
                restored = schema_fingerprint(
                    restore_url,
                    evidence_dir / f"restored-{lane}-fingerprint.json",
                )
                require_fingerprint_match(shaped[lane]["fingerprint"], restored)
                backup_result[lane] = {
                    "sha256": checksum,
                    "fingerprint_sha256": restored["fingerprint_sha256"],
                }
            result["backup_restore"] = backup_result
            result["gates"]["backup_restore"] = "PASS"

            runtime = _runtime_accept(
                release,
                shaped["operational"]["url"],
                shaped["public"]["url"],
                evidence_dir,
                "candidate",
                require_ready=bool(shape.get("require_public_ready", True)),
            )
            result["runtime"] = runtime
            result["gates"]["runtime_health"] = "PASS"
            result["gates"]["public_health"] = "PASS"
            result["gates"]["noindex"] = "PASS"

            active_before = _active_release(shaped["public"]["url"])
            previous_extract = work / "previous"
            previous_artifact = verify_artifact(
                previous["_artifact_path"], extract_to=previous_extract
            )
            selected_rollback_mode = "previous_runtime"
            try:
                previous_runtime = _runtime_accept(
                    previous_extract / "release",
                    shaped["operational"]["url"],
                    shaped["public"]["url"],
                    evidence_dir,
                    "rollback-previous",
                    require_ready=bool(shape.get("require_public_ready", True)),
                )
                selected_rollback_mode = rollback_mode(
                    True, previous["rollback"]
                )
            except AcceptanceError as error:
                selected_rollback_mode = rollback_mode(
                    False, previous["rollback"]
                )
                previous_runtime = {
                    "status": "INCOMPATIBLE",
                    "error": str(error),
                    "forward_recovery": previous["rollback"]["forward_recovery"],
                }
                _runtime_accept(
                    release,
                    shaped["operational"]["url"],
                    shaped["public"]["url"],
                    evidence_dir,
                    "rollback-forward-recovery",
                    require_ready=bool(shape.get("require_public_ready", True)),
                )
            active_after = _active_release(shaped["public"]["url"])
            if active_after != active_before:
                raise AcceptanceError("rollback changed the public release pointer")
            result["rollback"] = {
                "status": "PASS",
                "mode": selected_rollback_mode,
                "previous_artifact_sha256": previous_artifact["artifact_sha256"],
                "runtime": previous_runtime,
                "public_release_pointer": active_after,
            }
            result["gates"]["rollback"] = "PASS"

            result["status"] = "PASS"
            result["gates"]["migration_parity"] = "PASS"
            result["gates"]["lockfile_parity"] = "PASS"
            _write_result(
                evidence_dir / "accepted-artifact.json",
                {
                    "status": "ACCEPTED",
                    "artifact_sha256": artifact["artifact_sha256"],
                    "source_git_sha": artifact["source_git_sha"],
                    "lock_sha256": artifact["lock_sha256"],
                    "evidence": "acceptance-result.json",
                },
            )
    finally:
        if not args.keep_databases:
            databases.drop_all()
    return result


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--artifact", type=Path, required=True)
    parser.add_argument("--previous-release-manifest", type=Path, required=True)
    parser.add_argument("--production-shape-manifest", type=Path, required=True)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=Path("/var/lib/nextcompany-staging/evidence/latest"),
    )
    parser.add_argument("--work-dir", type=Path)
    parser.add_argument("--admin-url-env", default="STAGING_DB_ADMIN_URL")
    parser.add_argument("--keep-databases", action="store_true")
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    result: dict[str, Any] = {
        "task": "STAGING-PARITY-01",
        "status": "FAIL",
        "gates": {},
    }
    try:
        result = run_acceptance(args)
    except (
        AcceptanceError,
        ArtifactError,
        FingerprintMismatch,
        UnitParityError,
        OSError,
        psycopg.Error,
        subprocess.CalledProcessError,
    ) as error:
        result["error"] = str(error)
    result_path = args.evidence_dir.resolve() / "acceptance-result.json"
    _write_result(result_path, result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result["status"] == "PASS" else 1


if __name__ == "__main__":
    raise SystemExit(main())
