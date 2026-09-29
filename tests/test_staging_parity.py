from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from scripts.release_artifact import (
    ArtifactError,
    MANIFEST_NAME,
    _normalized_tar,
    canonical_json,
    sha256_file,
    systemd_hashes,
    tree_sha256,
    verify_artifact,
)
from scripts.render_staging_units import (
    UnitParityError,
    check_units,
    render_units,
)
from scripts.schema_fingerprint import (
    FingerprintMismatch,
    compare_fingerprints,
    payload_sha256,
    require_fingerprint_match,
)
from scripts.staging_acceptance import (
    AcceptanceError,
    _access_contract,
    _redact,
    migration_command,
    rollback_mode,
    validate_previous_manifest,
    validate_shape_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _fingerprint() -> dict:
    payload = {
        "contract_version": 1,
        "postgresql_version": "18.6",
        "schemas": [{"schema": "public"}],
        "relations": [
            {"schema": "public", "name": "companies", "kind": "table", "definition": None}
        ],
        "columns": [
            {
                "schema": "public",
                "relation": "companies",
                "name": "id",
                "position": 1,
                "type": "bigint",
                "nullable": False,
                "default": None,
                "identity": "d",
                "generated": None,
            }
        ],
        "indexes": [
            {
                "schema": "public",
                "relation": "companies",
                "name": "ix_companies_id",
                "unique": False,
                "primary": False,
                "valid": True,
                "constraint_backed": False,
                "definition": "CREATE INDEX ix_companies_id ON public.companies USING btree (id)",
                "predicate": None,
            }
        ],
        "unique_constraints": [],
        "foreign_keys": [],
        "check_constraints": [
            {
                "schema": "public",
                "relation": "companies",
                "name": "ck_companies_id",
                "definition": "CHECK (id > 0)",
                "validated": True,
            }
        ],
        "triggers": [],
        "routines": [],
        "migration_revisions": [{"revision": "head"}],
    }
    return {"payload": payload, "fingerprint_sha256": payload_sha256(payload)}


@pytest.mark.parametrize(
    "category",
    ("relations", "columns", "indexes", "check_constraints"),
)
def test_false_head_missing_object_fails_even_when_revision_is_head(category):
    expected = _fingerprint()
    observed = json.loads(json.dumps(expected))
    observed["payload"][category] = []
    observed["fingerprint_sha256"] = payload_sha256(observed["payload"])

    assert observed["payload"]["migration_revisions"] == [{"revision": "head"}]
    with pytest.raises(FingerprintMismatch, match="despite migration revision"):
        require_fingerprint_match(expected, observed)


def test_fingerprint_is_deterministic_and_declared_extra_schema_is_bounded():
    expected = _fingerprint()
    assert payload_sha256(expected["payload"]) == payload_sha256(expected["payload"])
    observed = json.loads(json.dumps(expected))
    observed["payload"]["schemas"].append({"schema": "legacy_v3_archive"})
    observed["payload"]["relations"].append(
        {
            "schema": "legacy_v3_archive",
            "name": "old_scores",
            "kind": "table",
            "definition": None,
        }
    )
    observed["fingerprint_sha256"] = payload_sha256(observed["payload"])

    assert not compare_fingerprints(expected, observed)["compatible"]
    assert compare_fingerprints(
        expected, observed, allowed_extra_schemas=["legacy_v3_archive"]
    )["compatible"]


def test_staging_units_are_generated_from_production_and_detect_drift(tmp_path):
    output = tmp_path / "units"
    manifest = render_units(output)

    assert manifest["canonical_source"] == "deploy/systemd"
    public = output / "nextcompany-staging-public.service"
    text = public.read_text(encoding="utf-8")
    assert "Environment=PUBLIC_FORCE_NOINDEX=1" in text
    assert "/opt/nextcompany-staging/current" in text
    assert "--port 18000" in text
    assert check_units(output)["status"] == "PASS"

    public.write_text(text + "\n# drift\n", encoding="utf-8")
    with pytest.raises(UnitParityError, match="content drift"):
        check_units(output)


def _fake_release(root: Path) -> tuple[Path, dict]:
    release = root / "release"
    (release / ".venv/bin").mkdir(parents=True)
    (release / "deploy/systemd").mkdir(parents=True)
    (release / "uv.lock").write_text("locked\n", encoding="utf-8")
    (release / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    python = release / ".venv/bin/python"
    python.write_text(
        "#!/bin/sh\nexec \"%s\" \"$@\"\n" % os.path.realpath(os.sys.executable),
        encoding="utf-8",
    )
    python.chmod(0o755)
    (release / "deploy/systemd/nextcompany-public.service").write_text(
        "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
    )
    runtime_hash = tree_sha256(release)
    source_hash = tree_sha256(release, excluded_prefixes=(".venv",))
    version = ".".join(map(str, os.sys.version_info[:3]))
    manifest = {
        "contract_version": 1,
        "source_git_sha": "a" * 40,
        "source_tree_sha256": source_hash,
        "runtime_tree_sha256": runtime_hash,
        "lock_sha256": sha256_file(release / "uv.lock"),
        "python_version": version,
        "python_minor": "3.14",
        "python_implementation": "CPython",
        "runtime_platform": os.uname().sysname.lower(),
        "runtime_machine": os.uname().machine,
        "postgresql_version": "18.6",
        "systemd_units": systemd_hashes(release),
    }
    (release / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
    return release, manifest


@pytest.mark.skipif(
    os.sys.version_info[:2] != (3, 14),
    reason="runtime artifact contract is pinned to Python 3.14",
)
def test_immutable_artifact_verifies_outer_and_runtime_hash(tmp_path):
    release, manifest = _fake_release(tmp_path)
    artifact = tmp_path / "candidate.tar.gz"
    _normalized_tar(release, artifact, 1)
    digest = sha256_file(artifact)
    artifact.with_name(artifact.name + ".sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )

    result = verify_artifact(artifact)
    assert result["artifact_sha256"] == digest
    assert result["lock_sha256"] == manifest["lock_sha256"]

    with artifact.open("ab") as stream:
        stream.write(b"tamper")
    with pytest.raises(ArtifactError, match="SHA-256 mismatch"):
        verify_artifact(artifact)


def _write_checked(path: Path, value: bytes = b"fixture") -> dict:
    path.write_bytes(value)
    return {"path": str(path), "sha256": hashlib.sha256(value).hexdigest()}


def test_previous_and_shape_contracts_fail_closed(tmp_path):
    artifact = _write_checked(tmp_path / "previous.tar.gz")
    (tmp_path / "previous.tar.gz.sha256").write_text(
        f"{artifact['sha256']}  previous.tar.gz\n", encoding="utf-8"
    )
    operational = _write_checked(tmp_path / "operational.dump")
    public = _write_checked(tmp_path / "public.dump")
    previous_path = tmp_path / "previous.json"
    previous_path.write_text(
        json.dumps(
            {
                "contract_version": 1,
                "accepted_artifact": artifact,
                "operational_backup": operational,
                "public_backup": public,
                "rollback": {
                    "db_downgrade_supported": False,
                    "forward_recovery": "reactivate candidate",
                },
            }
        ),
        encoding="utf-8",
    )
    assert validate_previous_manifest(previous_path)["_artifact_path"].is_file()

    shape_path = tmp_path / "shape.json"
    shape = {
        "contract_version": 1,
        "sanitized": True,
        "contains_sensitive_content": False,
        "mass_ingestion": False,
        "operational_backup": operational,
        "public_backup": public,
        "minimum_rows": {"operational": {}, "public": {}},
        "semantic_release_checks": [],
        "semantic_release_check_waiver": "not required for fixture",
    }
    shape_path.write_text(json.dumps(shape), encoding="utf-8")
    assert validate_shape_manifest(shape_path)["sanitized"] is True

    shape["contains_sensitive_content"] = True
    shape_path.write_text(json.dumps(shape), encoding="utf-8")
    with pytest.raises(AcceptanceError, match="sensitive"):
        validate_shape_manifest(shape_path)


def test_migration_parity_uses_same_runtime_and_only_canonical_configs(tmp_path):
    operational = migration_command(tmp_path, "operational")
    public = migration_command(tmp_path, "public")

    assert operational[:2] == public[:2]
    assert operational[-1] == "operational"
    assert public[-1] == "public"
    script = (
        ROOT / "deploy/scripts/run_release_migrations.sh"
    ).read_text(encoding="utf-8")
    assert 'exec "$python" -m alembic -c "$config" upgrade head' in script
    assert "staging" not in script.lower()


def test_access_boundary_is_loopback_authenticated_and_noindex():
    result = _access_contract(ROOT)
    assert result == {
        "status": "PASS",
        "listen": "127.0.0.1:18080",
        "authenticated": True,
    }


def test_acceptance_evidence_redacts_database_passwords():
    value = (
        "postgresql+psycopg://staging:very-secret@127.0.0.1:5432/db "
        "postgresql://other:second-secret@db/public"
    )
    redacted = _redact(value)
    assert "very-secret" not in redacted
    assert "second-secret" not in redacted
    assert redacted.count("***") == 2


def test_promotion_installs_exact_accepted_artifact_without_resolving_dependencies():
    script = (ROOT / "deploy/scripts/install_runtime_artifact.sh").read_text(
        encoding="utf-8"
    )
    assert "accepted artifact SHA-256 mismatch" in script
    assert "release_artifact.py\" verify" in script
    assert "uv sync" not in script
    assert "pip install" not in script
    assert 'releases_root="$app_root/releases"' in script
    assert '"$activation" == "--activate"' in script


def test_rollback_policy_fails_closed_or_requires_forward_recovery():
    assert rollback_mode(True, {"db_downgrade_supported": True}) == "previous_runtime"
    with pytest.raises(AcceptanceError, match="despite declared rollback support"):
        rollback_mode(False, {"db_downgrade_supported": True})
    with pytest.raises(AcceptanceError, match="forward-recovery"):
        rollback_mode(False, {"db_downgrade_supported": False})
    assert (
        rollback_mode(
            False,
            {
                "db_downgrade_supported": False,
                "forward_recovery": "reactivate accepted candidate",
            },
        )
        == "forward_recovery"
    )
