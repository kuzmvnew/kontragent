from pathlib import Path
import os
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "deploy" / "scripts" / "backup_operational.sh"


def _executable(path: Path, body: str) -> None:
    path.write_text("#!/bin/bash\nset -e\n" + body, encoding="utf-8")
    path.chmod(0o755)


def test_docker_fallback_dumps_operational_database_from_database_url(tmp_path):
    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    docker_log = tmp_path / "docker.log"
    backup_dir = tmp_path / "backups"

    _executable(
        fake_bin / "install",
        'for value in "$@"; do target="$value"; done\n/bin/mkdir -p "$target"\n',
    )
    _executable(fake_bin / "flock", "exit 0\n")
    _executable(
        fake_bin / "date",
        'if [[ "$*" == *"+%Y%m%dT%H%M%SZ"* ]]; then '
        'echo 20260925T120000Z; else echo 2026-09-25T12:00:00Z; fi\n',
    )
    _executable(
        fake_bin / "sha256sum",
        'echo "$(printf a%.0s {1..64})  $1"\n',
    )
    _executable(
        fake_bin / "docker",
        'echo "$*" >> "$DOCKER_LOG"\n'
        'if [[ "$1" == "inspect" ]]; then exit 0; fi\n'
        'if [[ "$*" == *"printenv POSTGRES_USER"* ]]; then echo worker_user; exit 0; fi\n'
        'if [[ "$*" == *"printenv POSTGRES_DB"* ]]; then echo nextcompany_task045; exit 0; fi\n'
        'if [[ "$*" == *"pg_dump"* ]]; then printf "operational dump"; exit 0; fi\n'
        'exit 0\n',
    )

    environment = {
        "PATH": str(fake_bin),
        "DATABASE_URL": (
            "postgresql+psycopg://worker:secret@127.0.0.1:5432/"
            "nextcompany_operational?sslmode=disable"
        ),
        "OPERATIONS_BACKUP_DIR": str(backup_dir),
        "DOCKER_LOG": str(docker_log),
    }
    result = subprocess.run(
        ["/bin/bash", str(SCRIPT)],
        cwd=ROOT,
        env=environment,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    calls = docker_log.read_text(encoding="utf-8")
    assert "nextcompany_operational" in calls
    assert "nextcompany_task045" not in calls
    dump = backup_dir / "nextcompany_operational_20260925T120000Z.dump"
    assert dump.read_bytes() == b"operational dump"
    assert '"status":"NOT CONFIGURED"' in dump.with_name(
        dump.name + ".restore-test.json"
    ).read_text(encoding="utf-8")


def test_explicit_operational_database_name_overrides_url_path(tmp_path):
    script = SCRIPT.read_text(encoding="utf-8")
    assert 'OPERATIONS_DATABASE_NAME:-${database_url%%\\?*}' in script
    assert 'sh "$operational_database"' in script
    assert '"$restore_database" == "$operational_database"' in script
