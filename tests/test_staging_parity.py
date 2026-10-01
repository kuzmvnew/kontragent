from __future__ import annotations

import hashlib
import io
import json
import os
from pathlib import Path
import tarfile

import pytest

from scripts.release_artifact import (
    ArtifactError,
    BUILDER_VERSION,
    MANIFEST_NAME,
    _normalized_tar,
    _normalize_tree_modes,
    canonical_json,
    extract_artifact,
    sha256_file,
    systemd_hashes,
    tree_sha256,
    verify_artifact,
    verify_release_tree,
)
from scripts.render_staging_units import (
    TEXT_REPLACEMENTS,
    UnitParityError,
    check_units,
    production_units,
    render_text,
    render_units,
    staging_name,
)
from scripts.schema_fingerprint import (
    CONTRACT_VERSION as FINGERPRINT_CONTRACT_VERSION,
    FingerprintMismatch,
    _canonical_sql,
    compare_fingerprints,
    payload_sha256,
    require_fingerprint_match,
)
from scripts.staging_acceptance import (
    AcceptanceError,
    DatabaseFactory,
    _access_contract,
    _header_value,
    _redact,
    migration_command,
    rollback_mode,
    validate_previous_manifest,
    validate_shape_manifest,
)


ROOT = Path(__file__).resolve().parents[1]


def _fingerprint() -> dict:
    payload = {
        "contract_version": FINGERPRINT_CONTRACT_VERSION,
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


def test_fingerprint_normalizes_postgres_dump_restore_varchar_array_casts():
    migrated = (
        "CHECK (status::text = ANY "
        "(ARRAY['staged'::character varying, 'active'::character varying]::text[]))"
    )
    restored = (
        "CHECK (status::text = ANY "
        "(ARRAY['staged'::character varying::text, "
        "'active'::character varying::text]))"
    )
    assert _canonical_sql(migrated) == _canonical_sql(restored)

    migrated_predicate = (
        "WHERE ((status)::text <> ALL "
        "((ARRAY['RESOLVED'::character varying, "
        "'CANCELLED'::character varying])::text[]))"
    )
    restored_predicate = (
        "WHERE ((status)::text <> ALL "
        "(ARRAY[('RESOLVED'::character varying)::text, "
        "('CANCELLED'::character varying)::text]))"
    )
    assert _canonical_sql(migrated_predicate) == _canonical_sql(restored_predicate)


def test_staging_units_are_generated_from_production_and_detect_drift(tmp_path):
    output = tmp_path / "units"
    manifest = render_units(output)

    assert manifest["canonical_source"] == "deploy/systemd"
    public = output / "nextcompany-staging-public.service"
    text = public.read_text(encoding="utf-8")
    assert "Environment=PUBLIC_FORCE_NOINDEX=1" in text
    assert "/opt/nextcompany-staging/current" in text
    assert "--port 18000" in text
    assert all(
        record["staging_unit"] == staging_name(record["production_unit"])
        for record in manifest["units"]
    )
    assert {record["production_unit"] for record in manifest["units"]} == {
        path.name for path in production_units()
    }
    assert all(
        "staging-staging" not in path.read_text(encoding="utf-8")
        for path in output.iterdir()
        if path.suffix in {".service", ".timer"}
    )
    assert check_units(output)["status"] == "PASS"

    second_output = tmp_path / "units-second"
    assert render_units(second_output) == manifest
    assert (second_output / "unit-parity.json").read_bytes() == (
        output / "unit-parity.json"
    ).read_bytes()

    public.write_text(text + "\n# drift\n", encoding="utf-8")
    with pytest.raises(UnitParityError, match="content drift"):
        check_units(output)


@pytest.mark.parametrize(("production", "staging"), TEXT_REPLACEMENTS)
def test_each_production_token_is_rendered_once_and_idempotently(
    production,
    staging,
):
    source = (
        "[Unit]\n"
        "Description=NEXT Company fixture\n"
        "After=nextcompany-public.service\n"
        "[Service]\n"
        f"ExecStart={production}\n"
    )

    rendered = render_text(source)

    assert staging in rendered
    assert "nextcompany-staging-public.service" in rendered
    assert "nextcompany-staging-staging" not in rendered
    assert render_text(rendered) == rendered


@pytest.mark.parametrize("path", production_units(), ids=lambda path: path.name)
def test_every_production_unit_name_gets_one_staging_suffix(path):
    assert staging_name(path.name) == path.name.replace(
        "nextcompany-", "nextcompany-staging-", 1
    )
    assert staging_name(staging_name(path.name)) == staging_name(path.name)


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
    (release / ".venv/bin/python3").symlink_to("python")
    (release / "deploy/systemd/nextcompany-public.service").write_text(
        "[Service]\nExecStart=/bin/true\n", encoding="utf-8"
    )
    _normalize_tree_modes(release)
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


@pytest.mark.skipif(
    os.sys.version_info[:2] != (3, 14),
    reason="runtime artifact contract is pinned to Python 3.14",
)
@pytest.mark.parametrize("umask", (0o022, 0o077))
def test_artifact_extract_restores_declared_modes_and_tree_hash(tmp_path, umask):
    release, manifest = _fake_release(tmp_path / "source")
    (release / ".venv").chmod(0o750)
    (release / "deploy").chmod(0o750)
    manifest["source_tree_sha256"] = tree_sha256(
        release,
        excluded={MANIFEST_NAME},
        excluded_prefixes=(".runtime", ".venv"),
    )
    manifest["runtime_tree_sha256"] = tree_sha256(
        release,
        excluded={MANIFEST_NAME},
    )
    (release / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
    artifact = tmp_path / f"candidate-{umask:o}.tar.gz"
    _normalized_tar(release, artifact, 1)
    digest = sha256_file(artifact)
    artifact.with_name(artifact.name + ".sha256").write_text(
        f"{digest}  {artifact.name}\n", encoding="utf-8"
    )

    previous_umask = os.umask(umask)
    try:
        result = verify_artifact(artifact, extract_to=tmp_path / f"extract-{umask:o}")
    finally:
        os.umask(previous_umask)

    extracted = tmp_path / f"extract-{umask:o}" / "release"
    assert result["runtime_tree_sha256"] == manifest["runtime_tree_sha256"]
    assert tree_sha256(extracted, excluded={MANIFEST_NAME}) == manifest[
        "runtime_tree_sha256"
    ]
    assert (extracted / ".venv").stat().st_mode & 0o777 == 0o750
    assert (extracted / "deploy").stat().st_mode & 0o777 == 0o750
    assert (extracted / ".venv/bin/python").stat().st_mode & 0o777 == 0o755


def test_artifact_extract_rejects_unsafe_declared_modes(tmp_path):
    artifact = tmp_path / "unsafe.tar.gz"
    with tarfile.open(artifact, "w:gz") as archive:
        root = tarfile.TarInfo("release")
        root.type = tarfile.DIRTYPE
        root.mode = 0o755
        archive.addfile(root)
        unsafe = tarfile.TarInfo("release/unsafe")
        unsafe.mode = 0o4755
        payload = b"unsafe\n"
        unsafe.size = len(payload)
        archive.addfile(unsafe, io.BytesIO(payload))

    with pytest.raises(ArtifactError, match="unsafe archive mode"):
        extract_artifact(artifact, tmp_path / "extracted")


def test_artifact_mode_normalization_is_safe_and_preserves_executability(tmp_path):
    root = tmp_path / "release"
    root.mkdir(mode=0o700)
    plain = root / "plain.txt"
    plain.write_text("plain\n", encoding="utf-8")
    plain.chmod(0o600)
    executable = root / "run"
    executable.write_text("#!/bin/sh\n", encoding="utf-8")
    executable.chmod(0o700)
    link = root / "run-link"
    link.symlink_to("run")

    _normalize_tree_modes(root)

    assert root.stat().st_mode & 0o777 == 0o755
    assert plain.stat().st_mode & 0o777 == 0o644
    assert executable.stat().st_mode & 0o777 == 0o755
    assert link.lstat().st_mode & 0o777 == 0o777


@pytest.mark.skipif(
    os.sys.version_info[:2] != (3, 14),
    reason="runtime artifact contract is pinned to Python 3.14",
)
def test_historical_reacceptance_records_distinct_builder_provenance(tmp_path):
    release, manifest = _fake_release(tmp_path)
    manifest.update(
        {
            "contract_version": 2,
            "builder": {
                "git_sha": "b" * 40,
                "script_sha256": "c" * 64,
                "version": BUILDER_VERSION,
            },
            "build_kind": "historical_reacceptance",
        }
    )
    (release / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")

    verified = verify_release_tree(release)

    assert verified["source_git_sha"] == "a" * 40
    assert verified["builder"]["git_sha"] == "b" * 40
    assert verified["build_kind"] == "historical_reacceptance"

    manifest["build_kind"] = "source_builder_same_commit"
    (release / MANIFEST_NAME).write_bytes(canonical_json(manifest) + b"\n")
    with pytest.raises(ArtifactError, match="provenance relationship"):
        verify_release_tree(release)


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


def test_noindex_header_name_is_case_insensitive_and_value_remains_exact():
    required = "noindex, nofollow, nosnippet"

    assert _header_value({"x-RoBoTs-TaG": required}, "X-Robots-Tag") == required
    assert _header_value(
        {"x-robots-tag": "noindex, nofollow"}, "X-Robots-Tag"
    ) != required
    assert _header_value(
        {"X-Robots-Tag": required, "x-robots-tag": required},
        "X-Robots-Tag",
    ) is None


def test_acceptance_evidence_redacts_database_passwords():
    value = (
        "postgresql+psycopg://staging:very-secret@127.0.0.1:5432/db "
        "postgresql://other:second-secret@db/public"
    )
    redacted = _redact(value)
    assert "very-secret" not in redacted
    assert "second-secret" not in redacted
    assert redacted.count("***") == 2


def test_database_factory_preserves_sqlalchemy_driver_and_uses_libpq_for_psycopg(
    monkeypatch,
):
    connected = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def execute(self, statement):
            return None

    def connect(url, **kwargs):
        connected.append((url, kwargs))
        return Connection()

    monkeypatch.setattr("scripts.staging_acceptance.psycopg.connect", connect)
    factory = DatabaseFactory(
        "postgresql+psycopg://user:pass@host/postgres",
        "token",
    )

    created_url = factory.create("db")

    assert factory.sqlalchemy_admin_url == (
        "postgresql+psycopg://user:pass@host/postgres"
    )
    assert factory.libpq_admin_url == "postgresql://user:pass@host/postgres"
    assert created_url == "postgresql+psycopg://user:pass@host/sp01_token_db"
    assert connected == [
        ("postgresql://user:pass@host/postgres", {"autocommit": True})
    ]


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


def test_canonical_builder_can_reaccept_a_distinct_historical_source_tree():
    script = (ROOT / "deploy/scripts/build_runtime_artifact.sh").read_text(
        encoding="utf-8"
    )

    assert 'builder_root="$(cd --' in script
    assert '--project "$builder_root"' in script
    assert '"$builder_root/scripts/release_artifact.py" build' in script
    assert '--source "$source_repository"' in script


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
