from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy/scripts/deploy_public.sh"
BACKUP_SCRIPT = ROOT / "deploy/scripts/backup_public.sh"


def _write_executable(path: Path, body: str) -> None:
    path.write_text(f"#!/usr/bin/env bash\nset -euo pipefail\n{body}\n", encoding="utf-8")
    path.chmod(0o755)


def _make_source(parent: Path, name: str) -> tuple[Path, str]:
    source = parent / name
    source.mkdir()
    (source / "pyproject.toml").write_text("[project]\nname='deploy-fixture'\nversion='0'\n")
    (source / "public_alembic.ini").write_text("[alembic]\n")
    subprocess.run(["git", "init", "-q", source], check=True)
    subprocess.run(["git", "-C", source, "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            source,
            "-c",
            "user.name=Deploy Test",
            "-c",
            "user.email=deploy@example.invalid",
            "commit",
            "-q",
            "-m",
            name,
        ],
        check=True,
    )
    sha = subprocess.check_output(
        ["git", "-C", source, "rev-parse", "HEAD"], text=True
    ).strip()
    return source, sha


@pytest.fixture
def deploy_harness(tmp_path: Path):
    app_root = tmp_path / "opt/nextcompany"
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    command_log.touch()

    _write_executable(
        bin_dir / "install",
        'for argument in "$@"; do [[ "$argument" == /* ]] && mkdir -p "$argument"; done',
    )
    for command in ("rsync", "uv", "chown", "chmod", "sudo", "nginx"):
        _write_executable(bin_dir / command, ":")
    _write_executable(
        bin_dir / "systemctl", 'printf "%s\\n" "$*" >> "$FAKE_COMMAND_LOG"'
    )
    _write_executable(
        bin_dir / "curl",
        """
if [[ -n "${FAKE_REMOVE_BEFORE_HEALTH:-}" ]]; then
  /bin/rm -rf "$FAKE_REMOVE_BEFORE_HEALTH"
fi
[[ "${FAKE_HEALTH_FAIL:-0}" != "1" ]]
""".strip(),
    )
    _write_executable(
        bin_dir / "ln",
        'if [[ "${1:-}" == "-s" && "${2:-}" == "--" ]]; then /bin/ln -s "$3" "$4"; else /bin/ln "$@"; fi',
    )
    _write_executable(
        bin_dir / "mv",
        'if [[ "${1:-}" == "-Tf" && "${2:-}" == "--" ]]; then /bin/rm -f "$4"; /bin/mv "$3" "$4"; else /bin/mv "$@"; fi',
    )
    _write_executable(
        bin_dir / "rm",
        'if [[ "${1:-}" == "-f" && "${2:-}" == "--" ]]; then /bin/rm -f "$3"; else /bin/rm "$@"; fi',
    )

    public_env = tmp_path / "public.env"
    importer_env = tmp_path / "importer.env"
    public_env.write_text(
        "PUBLIC_DATABASE_URL=postgresql+psycopg://web:secret@db/public\n",
        encoding="utf-8",
    )
    importer_env.write_text(
        "PUBLIC_IMPORT_DATABASE_URL=postgresql+psycopg://importer:secret@db/public\n",
        encoding="utf-8",
    )
    env = {
        **os.environ,
        "PATH": f"{bin_dir}:{os.environ['PATH']}",
        "NEXTCOMPANY_APP_ROOT": str(app_root),
        "NEXTCOMPANY_PUBLIC_ENV_FILE": str(public_env),
        "NEXTCOMPANY_IMPORTER_ENV_FILE": str(importer_env),
        "FAKE_COMMAND_LOG": str(command_log),
    }

    def run(source: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEPLOY_SCRIPT), str(source)],
            env={**env, **overrides},
            text=True,
            capture_output=True,
            check=False,
        )

    return tmp_path, app_root, public_env, importer_env, command_log, run


def _prior_release(app_root: Path, sha: str = "a" * 40) -> Path:
    release = app_root / "releases" / sha
    release.mkdir(parents=True)
    return release


def test_normal_first_deploy_activates_concrete_release(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    source, sha = _make_source(tmp_path, "first")

    result = run(source)

    expected = app_root / "releases" / sha
    assert result.returncode == 0, result.stderr
    assert (app_root / "current").is_symlink()
    assert os.readlink(app_root / "current") == str(expected)
    assert expected.is_dir()


def test_normal_upgrade_switches_to_new_release(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, sha = _make_source(tmp_path, "upgrade")

    result = run(source)

    assert result.returncode == 0, result.stderr
    assert os.readlink(app_root / "current") == str(app_root / "releases" / sha)
    assert previous.is_dir()


def test_health_check_failure_runs_rollback(deploy_harness) -> None:
    tmp_path, app_root, _, _, command_log, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, _ = _make_source(tmp_path, "failed-health")

    result = run(source, FAKE_HEALTH_FAIL="1")

    assert result.returncode == 1
    assert "rolled back" in result.stderr
    assert command_log.read_text().count("restart nextcompany-public.service") == 2


def test_missing_previous_release_leaves_failed_release_inactive(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, sha = _make_source(tmp_path, "missing-previous")

    result = run(
        source,
        FAKE_HEALTH_FAIL="1",
        FAKE_REMOVE_BEFORE_HEALTH=str(previous),
    )

    assert result.returncode == 1
    assert "rollback target invalid" in result.stderr
    assert "failed release left inactive" in result.stderr
    assert not (app_root / "current").exists()
    assert not (app_root / "current").is_symlink()
    assert (app_root / "releases" / sha).is_dir()


def test_broken_current_symlink_is_rejected_before_activation(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    missing = app_root / "releases" / ("b" * 40)
    missing.parent.mkdir(parents=True)
    (app_root / "current").symlink_to(missing)
    source, sha = _make_source(tmp_path, "broken-current")

    result = run(source)

    assert result.returncode == 2
    assert "does not exist as a directory" in result.stderr
    assert os.readlink(app_root / "current") == str(missing)
    assert not (app_root / "releases" / sha).exists()


def test_self_referencing_current_is_rejected(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    app_root.mkdir(parents=True)
    current = app_root / "current"
    current.symlink_to(current)
    source, sha = _make_source(tmp_path, "self-current")

    result = run(source)

    assert result.returncode == 2
    assert "must not reference itself" in result.stderr
    assert os.readlink(current) == str(current)
    assert not (app_root / "releases" / sha).exists()


def test_current_target_outside_releases_is_rejected(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    outside = tmp_path / "outside-release"
    outside.mkdir()
    app_root.mkdir(parents=True)
    (app_root / "current").symlink_to(outside)
    source, sha = _make_source(tmp_path, "outside-current")

    result = run(source)

    assert result.returncode == 2
    assert "must live directly under" in result.stderr
    assert os.readlink(app_root / "current") == str(outside)
    assert not (app_root / "releases" / sha).exists()


def test_successful_rollback_restores_exact_prior_sha_directory(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, _, run = deploy_harness
    prior_sha = "0123456789abcdef" * 2 + "01234567"
    previous = _prior_release(app_root, prior_sha)
    (app_root / "current").symlink_to(previous)
    source, new_sha = _make_source(tmp_path, "exact-rollback")

    result = run(source, FAKE_HEALTH_FAIL="1")

    assert result.returncode == 1
    assert os.readlink(app_root / "current") == str(previous)
    assert os.readlink(app_root / "current") != str(app_root / "current")
    assert os.readlink(app_root / "current") != str(app_root / "releases" / new_sha)


def test_canonical_env_examples_pin_sqlalchemy_psycopg3_dsn() -> None:
    public_env = (ROOT / "deploy/env/public.env.example").read_text()
    importer_env = (ROOT / "deploy/env/importer.env.example").read_text()

    assert "PUBLIC_DATABASE_URL=postgresql+psycopg://" in public_env
    assert "PUBLIC_IMPORT_DATABASE_URL=postgresql+psycopg://" in importer_env
    assert "PUBLIC_DATABASE_URL=postgresql://" not in public_env
    assert "PUBLIC_IMPORT_DATABASE_URL=postgresql://" not in importer_env


def test_deploy_rejects_unsupported_default_dsn_before_activation(
    deploy_harness,
) -> None:
    tmp_path, app_root, public_env, _, _, run = deploy_harness
    public_env.write_text(
        "PUBLIC_DATABASE_URL=postgresql://web:secret@db/public\n", encoding="utf-8"
    )
    source, sha = _make_source(tmp_path, "bad-dsn")

    result = run(source)

    assert result.returncode == 2
    assert "PUBLIC_DATABASE_URL must use postgresql+psycopg://" in result.stderr
    assert not (app_root / "current").exists()
    assert not (app_root / "releases" / sha).exists()


def test_backup_converts_sqlalchemy_psycopg3_dsn_for_libpq(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    backup_dir = tmp_path / "backups"
    dsn_log = tmp_path / "dsn.log"
    _write_executable(
        bin_dir / "install",
        'for argument in "$@"; do [[ "$argument" == /* ]] && mkdir -p "$argument"; done',
    )
    _write_executable(bin_dir / "flock", ":")
    _write_executable(
        bin_dir / "pg_dump",
        """
for argument in "$@"; do
  case "$argument" in
    --file=*) : > "${argument#--file=}" ;;
    postgresql://*) printf "%s\\n" "$argument" > "$FAKE_DSN_LOG" ;;
  esac
done
""".strip(),
    )

    result = subprocess.run(
        [str(BACKUP_SCRIPT)],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "PUBLIC_IMPORT_DATABASE_URL": (
                "postgresql+psycopg://importer:secret@db/public"
            ),
            "PUBLIC_BACKUP_DIR": str(backup_dir),
            "FAKE_DSN_LOG": str(dsn_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert dsn_log.read_text().strip() == "postgresql://importer:secret@db/public"


def test_restore_uses_libpq_url_not_sqlalchemy_driver_url() -> None:
    script = (ROOT / "deploy/scripts/restore_public_backup.sh").read_text()

    assert 'libpq_url="postgresql://${PUBLIC_IMPORT_DATABASE_URL#postgresql+psycopg://}"' in script
    assert 'pg_restore --clean --if-exists --no-owner --no-acl --dbname="$libpq_url"' in script
