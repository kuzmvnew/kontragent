from __future__ import annotations

import json
from pathlib import Path

import pytest

import scripts.run_public_sync as public_sync
from scripts.public_sync_ssh_wrapper import (
    AllowedCommand,
    CommandRejected,
    execute,
    parse_original_command,
)


RELEASE = "public-v1-wrapper-test"


def test_wrapper_accepts_every_command_emitted_by_public_sync_transport(
    tmp_path,
    monkeypatch,
):
    bundle = tmp_path / "bundle"
    bundle.mkdir()
    for name in ("manifest.json", "companies.jsonl.gz", "checksums.sha256"):
        (bundle / name).write_bytes(b"fixture")
    commands: list[str] = []

    def fake_run(argv, **_kwargs):
        command = argv[-1]
        commands.append(command)
        if "read_public_release.py" in command:
            return json.dumps(
                {
                    "release_id": RELEASE,
                    "record_count": 0,
                    "member_inns": [],
                    "projections": [],
                }
            )
        return "{}"

    monkeypatch.setattr(public_sync, "_run", fake_run)
    transport = public_sync.SshPublicTransport("sync@example.invalid")

    transport.upload(bundle, RELEASE)
    transport.read_active_projections(RELEASE, ())
    transport.import_release(RELEASE)
    transport.accept_release(RELEASE)
    transport.promote_release(RELEASE)
    transport.rollback(RELEASE)

    parsed = [parse_original_command(command) for command in commands]
    assert [(command.action, command.mode) for command in parsed] == [
        ("upload", None),
        ("read_active_projections", None),
        ("import", "--stage-only"),
        ("import", "--accept-staged"),
        ("import", "--promote"),
        ("rollback", None),
    ]
    assert {command.release_id for command in parsed} == {RELEASE}


def _upload(directory: str = RELEASE, target: str = RELEASE) -> str:
    return (
        "sudo install -d -o nextcompany-importer -g nextcompany -m 0750 "
        f"/var/lib/nextcompany/incoming/{directory} && sudo -u "
        "nextcompany-importer tar -C "
        f"/var/lib/nextcompany/incoming/{target} -xf -"
    )


def _inner(command: str) -> str:
    return (
        "sudo -u nextcompany-importer /bin/bash -lc 'set -a; source "
        "/etc/nextcompany/importer.env; set +a; "
        "/opt/nextcompany/current/.venv/bin/python "
        f"{command}'"
    )


def _import(directory: str = RELEASE, expected: str = RELEASE, mode: str = "--promote") -> str:
    return _inner(
        "/opt/nextcompany/current/scripts/import_public_release.py "
        f"/var/lib/nextcompany/incoming/{directory} "
        f"--expected-release-id {expected} {mode}"
    )


@pytest.mark.parametrize(
    "command",
    (
        None,
        "",
        "/bin/bash",
        "sudo id",
        _upload(directory="../../etc", target="../../etc"),
        _upload(target="public-v1-other-release"),
        _upload() + " extra",
        _import(directory="../wrapper-test"),
        _import(expected="public-v1-other-release"),
        _import(mode="--unknown"),
        _import() + " --database-url postgresql://attacker",
        _inner(
            "/opt/nextcompany/current/scripts/read_public_release.py "
            f"--expected-release-id {RELEASE} --inn 0274101890 --inn 0100000614"
        ),
        _inner(
            "/opt/nextcompany/current/scripts/read_public_release.py "
            f"--expected-release-id {RELEASE} --inn 0274101890 --inn 0274101890"
        ),
        _inner(
            "/opt/nextcompany/current/scripts/read_public_release.py "
            f"--expected-release-id {RELEASE} --inn 027410189X"
        ),
        _inner(
            "/opt/nextcompany/current/scripts/rollback_public_release.py "
            f"{RELEASE} extra"
        ),
        _inner(
            "/opt/nextcompany/current/scripts/rollback_public_release.py ../etc"
        ),
        _import() + "\n/bin/bash",
    ),
)
def test_wrapper_rejects_interactive_arbitrary_and_malformed_commands(command):
    with pytest.raises(CommandRejected):
        parse_original_command(command)


def test_wrapper_reconstructs_fixed_argv_instead_of_executing_original_text(
    monkeypatch,
):
    calls = []
    monkeypatch.setattr(
        "scripts.public_sync_ssh_wrapper.subprocess.run",
        lambda argv, **kwargs: calls.append((argv, kwargs)),
    )

    execute(
        AllowedCommand(
            "read_active_projections",
            RELEASE,
            inns=("0100000614", "0274101890"),
        )
    )

    assert calls[0][0][0:6] == [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "nextcompany-importer",
        "/bin/bash",
        "-lc",
    ]
    assert calls[0][1] == {"check": True}
    assert calls[0][0][-1].endswith(
        f"--expected-release-id {RELEASE} "
        "--inn 0100000614 --inn 0274101890"
    )


def test_wrapper_installation_is_repository_managed_and_root_owned():
    root = Path(__file__).resolve().parents[1]
    installer = (
        root / "deploy/scripts/install_public_sync_ssh_wrapper.sh"
    ).read_text(encoding="utf-8")

    assert "/usr/local/sbin/nextcompany-public-sync-ssh" in installer
    assert "scripts/public_sync_ssh_wrapper.py" in installer
    assert 'install -o root -g root -m 0755' in installer
    assert '"$mode" == "--check"' in installer
    assert (
        root / "deploy/ssh/nextcompany-public-sync-authorized-key-options"
    ).read_text(encoding="utf-8") == (
        'restrict,command="/usr/local/sbin/nextcompany-public-sync-ssh"\n'
    )
