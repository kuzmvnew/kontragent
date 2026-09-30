#!/usr/bin/env python3
"""Fail-closed forced-command boundary for NEXT Company public sync SSH.

Install this file as /usr/local/sbin/nextcompany-public-sync-ssh and bind the
dedicated SSH public key to it with an authorized_keys ``command=`` option.
The raw SSH command is never evaluated.  It must match one canonical transport
shape, after which this wrapper reconstructs fixed argv and shell input.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re
import subprocess
import sys


SAFE_RELEASE = r"[A-Za-z0-9._-]{8,120}"
INCOMING_ROOT = "/var/lib/nextcompany/incoming"
IMPORTER_PREFIX = (
    "set -a; source /etc/nextcompany/importer.env; set +a; "
    "/opt/nextcompany/current/.venv/bin/python "
)
OUTER_PREFIX = "sudo -u nextcompany-importer /bin/bash -lc '"
OUTER_SUFFIX = "'"

UPLOAD_PATTERN = re.compile(
    r"sudo install -d -o nextcompany-importer -g nextcompany -m 0750 "
    rf"{re.escape(INCOMING_ROOT)}/(?P<directory>{SAFE_RELEASE})"
    r" && sudo -u nextcompany-importer tar -C "
    rf"{re.escape(INCOMING_ROOT)}/(?P<target>{SAFE_RELEASE}) -xf -"
)
READ_PATTERN = re.compile(
    re.escape(OUTER_PREFIX + IMPORTER_PREFIX)
    + r"/opt/nextcompany/current/scripts/read_public_release\.py "
    + rf"--expected-release-id (?P<release>{SAFE_RELEASE})"
    + r"(?P<inns>(?: --inn [0-9]{10})*)"
    + re.escape(OUTER_SUFFIX)
)
IMPORT_PATTERN = re.compile(
    re.escape(OUTER_PREFIX + IMPORTER_PREFIX)
    + r"/opt/nextcompany/current/scripts/import_public_release\.py "
    + rf"{re.escape(INCOMING_ROOT)}/(?P<directory>{SAFE_RELEASE}) "
    + rf"--expected-release-id (?P<expected>{SAFE_RELEASE}) "
    + r"(?P<mode>--stage-only|--accept-staged|--promote)"
    + re.escape(OUTER_SUFFIX)
)
ROLLBACK_PATTERN = re.compile(
    re.escape(OUTER_PREFIX + IMPORTER_PREFIX)
    + r"/opt/nextcompany/current/scripts/rollback_public_release\.py "
    + rf"(?P<release>{SAFE_RELEASE})"
    + re.escape(OUTER_SUFFIX)
)


class CommandRejected(ValueError):
    """The requested SSH command is outside the public-sync allowlist."""


@dataclass(frozen=True)
class AllowedCommand:
    action: str
    release_id: str
    mode: str | None = None
    inns: tuple[str, ...] = ()


def parse_original_command(command: str | None) -> AllowedCommand:
    """Parse only exact commands emitted by ``SshPublicTransport``."""

    if not command:
        raise CommandRejected("interactive SSH sessions are disabled")

    match = UPLOAD_PATTERN.fullmatch(command)
    if match:
        if match["directory"] != match["target"]:
            raise CommandRejected("upload release IDs do not match")
        return AllowedCommand("upload", match["directory"])

    match = READ_PATTERN.fullmatch(command)
    if match:
        inns = tuple(match["inns"].replace(" --inn ", " ").split())
        if inns != tuple(sorted(set(inns))):
            raise CommandRejected("trusted reader INNs must be sorted and unique")
        return AllowedCommand("read_active_projections", match["release"], inns=inns)

    match = IMPORT_PATTERN.fullmatch(command)
    if match:
        if match["directory"] != match["expected"]:
            raise CommandRejected("import release IDs do not match")
        return AllowedCommand(
            "import",
            match["directory"],
            mode=match["mode"],
        )

    match = ROLLBACK_PATTERN.fullmatch(command)
    if match:
        return AllowedCommand("rollback", match["release"])

    raise CommandRejected("SSH command is not allowed")


def _importer_shell(command: str) -> list[str]:
    return [
        "/usr/bin/sudo",
        "-n",
        "-u",
        "nextcompany-importer",
        "/bin/bash",
        "-lc",
        command,
    ]


def execute(command: AllowedCommand) -> None:
    """Execute a validated action using reconstructed fixed command arguments."""

    incoming = f"{INCOMING_ROOT}/{command.release_id}"
    if command.action == "upload":
        subprocess.run(
            [
                "/usr/bin/sudo",
                "-n",
                "/usr/bin/install",
                "-d",
                "-o",
                "nextcompany-importer",
                "-g",
                "nextcompany",
                "-m",
                "0750",
                incoming,
            ],
            check=True,
        )
        subprocess.run(
            [
                "/usr/bin/sudo",
                "-n",
                "-u",
                "nextcompany-importer",
                "/usr/bin/tar",
                "-C",
                incoming,
                "-xf",
                "-",
            ],
            check=True,
        )
        return

    if command.action == "read_active_projections":
        arguments = "".join(f" --inn {inn}" for inn in command.inns)
        inner = (
            IMPORTER_PREFIX
            + "/opt/nextcompany/current/scripts/read_public_release.py "
            + f"--expected-release-id {command.release_id}{arguments}"
        )
    elif command.action == "import" and command.mode in {
        "--stage-only",
        "--accept-staged",
        "--promote",
    }:
        inner = (
            IMPORTER_PREFIX
            + "/opt/nextcompany/current/scripts/import_public_release.py "
            + f"{incoming} --expected-release-id {command.release_id} {command.mode}"
        )
    elif command.action == "rollback":
        inner = (
            IMPORTER_PREFIX
            + "/opt/nextcompany/current/scripts/rollback_public_release.py "
            + command.release_id
        )
    else:  # Defensive: callers cannot synthesize an unrecognized dataclass.
        raise CommandRejected("validated action is not executable")
    subprocess.run(_importer_shell(inner), check=True)


def main() -> int:
    try:
        command = parse_original_command(os.environ.get("SSH_ORIGINAL_COMMAND"))
        execute(command)
    except CommandRejected as error:
        print(f"public sync SSH command rejected: {error}", file=sys.stderr)
        return 126
    except subprocess.CalledProcessError as error:
        return error.returncode or 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
