from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
DEPLOY_SCRIPT = ROOT / "deploy/scripts/deploy_public.sh"
BACKUP_SCRIPT = ROOT / "deploy/scripts/backup_public.sh"
RESTORE_SCRIPT = ROOT / "deploy/scripts/restore_public_backup.sh"
BACKUP_SERVICE = ROOT / "deploy/systemd/nextcompany-backup.service"


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
    health_attempt_file = tmp_path / "health-attempts"
    service_check_file = tmp_path / "service-checks"
    uv_log = tmp_path / "uv.log"

    _write_executable(
        bin_dir / "install",
        'for argument in "$@"; do [[ "$argument" == /* ]] && mkdir -p "$argument"; done',
    )
    for command in ("rsync", "chown", "chmod", "nginx"):
        _write_executable(bin_dir / command, ":")
    _write_executable(
        bin_dir / "uv",
        """
project=""
while (($#)); do
  case "$1" in
    --project) project="$2"; shift 2 ;;
    *) shift ;;
  esac
done
target="${FAKE_UV_PYTHON_TARGET:-${UV_PYTHON_INSTALL_DIR}/cpython-test/bin/python3}"
printf 'HOME=%s XDG_CACHE_HOME=%s XDG_CONFIG_HOME=%s UV_CACHE_DIR=%s UV_PYTHON_INSTALL_DIR=%s\\n' \
  "$HOME" "$XDG_CACHE_HOME" "$XDG_CONFIG_HOME" "$UV_CACHE_DIR" \
  "$UV_PYTHON_INSTALL_DIR" >> "$FAKE_UV_LOG"
if [[ "$target" != /root/* && "$target" != /home/root/* ]]; then
  mkdir -p "$(dirname "$target")"
  printf '#!/usr/bin/env bash\\nexit 0\\n' > "$target"
  /bin/chmod +x "$target"
fi
mkdir -p "$project/.venv/bin"
/bin/ln -s "$target" "$project/.venv/bin/python"
""".strip(),
    )
    _write_executable(
        bin_dir / "sudo",
        'printf "sudo %s\\n" "$*" >> "$FAKE_COMMAND_LOG"',
    )
    _write_executable(
        bin_dir / "systemctl",
        """
printf 'systemctl %s\\n' "$*" >> "$FAKE_COMMAND_LOG"
if [[ "${1:-}" == "is-failed" ]]; then
  count=0
  [[ ! -f "$FAKE_SERVICE_CHECK_FILE" ]] || count="$(<"$FAKE_SERVICE_CHECK_FILE")"
  count=$((count + 1))
  printf '%s\\n' "$count" > "$FAKE_SERVICE_CHECK_FILE"
  if [[ -n "${FAKE_SERVICE_FAIL_AFTER:-}" ]] \
    && ((count >= FAKE_SERVICE_FAIL_AFTER)); then
    exit 0
  fi
  exit 1
fi
""".strip(),
    )
    _write_executable(
        bin_dir / "curl",
        """
if [[ -n "${FAKE_REMOVE_BEFORE_HEALTH:-}" ]]; then
  /bin/rm -rf "$FAKE_REMOVE_BEFORE_HEALTH"
fi
count=0
[[ ! -f "$FAKE_HEALTH_ATTEMPT_FILE" ]] || count="$(<"$FAKE_HEALTH_ATTEMPT_FILE")"
count=$((count + 1))
printf '%s\\n' "$count" > "$FAKE_HEALTH_ATTEMPT_FILE"
sequence="${FAKE_HEALTH_SEQUENCE:-200}"
[[ "${FAKE_HEALTH_FAIL:-0}" != "1" ]] || sequence="500"
IFS=',' read -r -a responses <<< "$sequence"
index=$((count - 1))
if ((index >= ${#responses[@]})); then
  index=$((${#responses[@]} - 1))
fi
response="${responses[$index]}"
printf 'curl-attempt %s response %s\\n' "$count" "$response" >> "$FAKE_COMMAND_LOG"
case "$response" in
  connection) printf '000'; exit 7 ;;
  reset) printf '000'; exit 56 ;;
  *) printf '%s' "$response" ;;
esac
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
        "NEXTCOMPANY_STARTUP_TIMEOUT_SECONDS": "1",
        "NEXTCOMPANY_STARTUP_POLL_SECONDS": "1",
        "NEXTCOMPANY_STARTUP_CURL_TIMEOUT_SECONDS": "1",
        "FAKE_COMMAND_LOG": str(command_log),
        "FAKE_HEALTH_ATTEMPT_FILE": str(health_attempt_file),
        "FAKE_SERVICE_CHECK_FILE": str(service_check_file),
        "FAKE_UV_LOG": str(uv_log),
    }

    def run(source: Path, **overrides: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [str(DEPLOY_SCRIPT), str(source)],
            env={**env, **overrides},
            text=True,
            capture_output=True,
            check=False,
        )

    return tmp_path, app_root, public_env, importer_env, command_log, uv_log, run


def _prior_release(app_root: Path, sha: str = "a" * 40) -> Path:
    release = app_root / "releases" / sha
    release.mkdir(parents=True)
    return release


def test_normal_first_deploy_activates_concrete_release(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
    source, sha = _make_source(tmp_path, "first")

    result = run(source)

    expected = app_root / "releases" / sha
    assert result.returncode == 0, result.stderr
    assert (app_root / "current").is_symlink()
    assert os.readlink(app_root / "current") == str(expected)
    assert expected.is_dir()


def test_normal_upgrade_switches_to_new_release(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, sha = _make_source(tmp_path, "upgrade")

    result = run(source)

    assert result.returncode == 0, result.stderr
    assert os.readlink(app_root / "current") == str(app_root / "releases" / sha)
    assert previous.is_dir()


def test_health_wait_accepts_immediate_http_200(deploy_harness) -> None:
    tmp_path, _, _, _, command_log, _, run = deploy_harness
    source, _ = _make_source(tmp_path, "immediate-health")

    result = run(source)

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().count("curl-attempt") == 1


def test_health_wait_tolerates_initial_connection_refusal(deploy_harness) -> None:
    tmp_path, _, _, _, command_log, _, run = deploy_harness
    source, _ = _make_source(tmp_path, "refused-then-healthy")

    result = run(
        source,
        FAKE_HEALTH_SEQUENCE="connection,200",
        NEXTCOMPANY_STARTUP_TIMEOUT_SECONDS="3",
    )

    assert result.returncode == 0, result.stderr
    assert "curl-attempt 1 response connection" in command_log.read_text()
    assert "curl-attempt 2 response 200" in command_log.read_text()


def test_health_wait_tolerates_multiple_transient_failures(deploy_harness) -> None:
    tmp_path, _, _, _, command_log, _, run = deploy_harness
    source, _ = _make_source(tmp_path, "transient-then-healthy")

    result = run(
        source,
        FAKE_HEALTH_SEQUENCE="connection,reset,503,200",
        NEXTCOMPANY_STARTUP_TIMEOUT_SECONDS="5",
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().count("curl-attempt") == 4


def test_health_wait_rejects_http_500_until_bounded_deadline(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, _ = _make_source(tmp_path, "persistent-500")

    result = run(source, FAKE_HEALTH_SEQUENCE="500")

    assert result.returncode == 1
    assert "timed out after 1s (last HTTP status: 500)" in result.stderr
    assert "rolled back" in result.stderr
    assert os.readlink(app_root / "current") == str(previous)


def test_health_wait_stops_when_service_enters_failed_state(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, command_log, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, _ = _make_source(tmp_path, "service-failed")

    result = run(source, FAKE_SERVICE_FAIL_AFTER="1")

    assert result.returncode == 1
    assert "entered failed state" in result.stderr
    assert "curl-attempt" not in command_log.read_text()
    assert os.readlink(app_root / "current") == str(previous)


def test_health_wait_times_out_on_connection_failures(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, _ = _make_source(tmp_path, "connection-timeout")

    result = run(source, FAKE_HEALTH_SEQUENCE="connection")

    assert result.returncode == 1
    assert "timed out after 1s without an HTTP response" in result.stderr
    assert "rolled back" in result.stderr


def test_successful_health_wait_does_not_rollback(deploy_harness) -> None:
    tmp_path, app_root, _, _, command_log, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, sha = _make_source(tmp_path, "healthy-upgrade")

    result = run(
        source,
        FAKE_HEALTH_SEQUENCE="connection,200",
        NEXTCOMPANY_STARTUP_TIMEOUT_SECONDS="3",
    )

    assert result.returncode == 0, result.stderr
    assert command_log.read_text().count("restart nextcompany-public.service") == 1
    assert os.readlink(app_root / "current") == str(app_root / "releases" / sha)


def test_health_check_failure_runs_rollback(deploy_harness) -> None:
    tmp_path, app_root, _, _, command_log, _, run = deploy_harness
    previous = _prior_release(app_root)
    (app_root / "current").symlink_to(previous)
    source, _ = _make_source(tmp_path, "failed-health")

    result = run(source, FAKE_HEALTH_FAIL="1")

    assert result.returncode == 1
    assert "rolled back" in result.stderr
    assert command_log.read_text().count("restart nextcompany-public.service") == 2


def test_uv_uses_shared_non_root_runtime_and_both_runtime_users(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, command_log, uv_log, run = deploy_harness
    source, sha = _make_source(tmp_path, "shared-python")

    result = run(source)

    expected_shared = app_root / "shared"
    expected_python = expected_shared / "python"
    expected_interpreter = expected_python / "cpython-test/bin/python3"
    release_python = app_root / "releases" / sha / ".venv/bin/python"
    assert result.returncode == 0, result.stderr
    assert release_python.resolve() == expected_interpreter
    assert "/root" not in str(release_python.resolve())
    uv_environment = uv_log.read_text()
    assert f"HOME={expected_shared / 'uv-home'}" in uv_environment
    assert f"UV_CACHE_DIR={expected_shared / 'uv-cache'}" in uv_environment
    assert f"UV_PYTHON_INSTALL_DIR={expected_python}" in uv_environment
    commands = command_log.read_text()
    assert f"sudo -u nextcompany-web {release_python} -c" in commands
    assert "sudo -u nextcompany-importer /bin/bash -c" in commands
    assert "public_alembic.ini" in commands


def test_deploy_rejects_release_python_under_root_home(deploy_harness) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
    source, sha = _make_source(tmp_path, "root-python")

    result = run(
        source,
        FAKE_UV_PYTHON_TARGET="/root/.local/share/uv/python/cpython/bin/python3",
    )

    assert result.returncode == 2
    assert "must not reference root's home" in result.stderr
    assert not (app_root / "current").exists()
    assert (app_root / "releases" / sha).is_dir()


def test_deploy_keeps_release_and_shared_python_root_owned_read_only() -> None:
    script = DEPLOY_SCRIPT.read_text()

    assert "--exclude .venv" in script
    assert 'chown -R root:nextcompany "$uv_python_install_dir"' in script
    assert 'chmod -R g+rX,g-w,o-rwx "$uv_python_install_dir"' in script
    assert 'chown -R root:nextcompany "$release_dir"' in script
    assert 'chmod -R g-w "$release_dir"' in script


def test_missing_previous_release_leaves_failed_release_inactive(
    deploy_harness,
) -> None:
    tmp_path, app_root, _, _, _, _, run = deploy_harness
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
    tmp_path, app_root, _, _, _, _, run = deploy_harness
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
    tmp_path, app_root, _, _, _, _, run = deploy_harness
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
    tmp_path, app_root, _, _, _, _, run = deploy_harness
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
    tmp_path, app_root, _, _, _, _, run = deploy_harness
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
    tmp_path, app_root, public_env, _, _, _, run = deploy_harness
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
    backup_file = Path(result.stdout.strip())
    checksum_file = Path(f"{backup_file}.sha256")
    assert backup_file.is_file()
    assert checksum_file.is_file()
    subprocess.run(
        ["sha256sum", "-c", checksum_file.name],
        cwd=checksum_file.parent,
        check=True,
        capture_output=True,
        text=True,
    )


def test_backup_service_canonically_declares_runtime_access_and_hardening() -> None:
    service = BACKUP_SERVICE.read_text()

    assert "User=postgres" in service
    assert "Group=postgres" in service
    assert "SupplementaryGroups=nextcompany" in service
    assert "EnvironmentFile=/etc/nextcompany/public.env" in service
    assert "EnvironmentFile=/etc/nextcompany/importer.env" in service
    assert "ExecStart=/opt/nextcompany/current/deploy/scripts/backup_public.sh" in service
    assert "NoNewPrivileges=true" in service
    assert "ProtectSystem=strict" in service
    assert "ProtectHome=true" in service
    assert "ReadWritePaths=/var/backups/nextcompany" in service
    assert "UMask=0077" in service


def test_restore_verifies_backup_and_runs_full_restore_check(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    command_log = tmp_path / "commands.log"
    backup_file = tmp_path / "nextcompany_public_test.dump"
    backup_file.write_bytes(b"test backup")
    checksum = hashlib.sha256(backup_file.read_bytes()).hexdigest()
    Path(f"{backup_file}.sha256").write_text(
        f"{checksum}  {backup_file.name}\n",
        encoding="utf-8",
    )
    app_root = tmp_path / "opt/nextcompany"
    backup_helper = app_root / "current/deploy/scripts/backup_public.sh"
    backup_helper.parent.mkdir(parents=True)
    _write_executable(
        backup_helper,
        'printf "pre-restore-backup\\n" >> "$FAKE_COMMAND_LOG"',
    )
    _write_executable(
        bin_dir / "pg_restore",
        'printf "pg_restore %s\\n" "$*" >> "$FAKE_COMMAND_LOG"',
    )
    _write_executable(
        bin_dir / "systemctl",
        'printf "systemctl %s\\n" "$*" >> "$FAKE_COMMAND_LOG"',
    )
    _write_executable(
        bin_dir / "curl",
        'printf "curl %s\\n" "$*" >> "$FAKE_COMMAND_LOG"',
    )

    result = subprocess.run(
        [
            str(RESTORE_SCRIPT),
            "--backup",
            str(backup_file),
            "--confirm",
            "RESTORE_PUBLIC_DB",
        ],
        env={
            **os.environ,
            "PATH": f"{bin_dir}:{os.environ['PATH']}",
            "NEXTCOMPANY_APP_ROOT": str(app_root),
            "PUBLIC_IMPORT_DATABASE_URL": (
                "postgresql+psycopg://importer:secret@db/public"
            ),
            "FAKE_COMMAND_LOG": str(command_log),
        },
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    commands = command_log.read_text().splitlines()
    assert commands[0] == "pre-restore-backup"
    assert commands[1] == "systemctl stop nextcompany-public.service"
    assert commands[2].startswith("pg_restore --clean --if-exists")
    assert "--dbname=postgresql://importer:secret@db/public" in commands[2]
    assert commands[3] == "systemctl start nextcompany-public.service"
    assert commands[4].endswith("http://127.0.0.1:8000/api/ready")


def test_restore_uses_libpq_url_not_sqlalchemy_driver_url() -> None:
    script = (ROOT / "deploy/scripts/restore_public_backup.sh").read_text()

    assert 'libpq_url="postgresql://${PUBLIC_IMPORT_DATABASE_URL#postgresql+psycopg://}"' in script
    assert 'pg_restore --clean --if-exists --no-owner --no-acl --dbname="$libpq_url"' in script
