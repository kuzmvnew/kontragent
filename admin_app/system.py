"""Constrained host probes used by the private operations console."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
from typing import Any


WORKER_SERVICE = "nextcompany-source-worker.service"
ADMIN_SERVICE = "nextcompany-admin.service"
INCIDENT_SERVICE = "nextcompany-incident-controller.service"
PUBLIC_SYNC_SERVICE = "nextcompany-public-sync.service"
_ALLOWED_SERVICES = frozenset(
    {WORKER_SERVICE, ADMIN_SERVICE, INCIDENT_SERVICE, PUBLIC_SYNC_SERVICE}
)
_BACKUP_NAME = re.compile(r"^nextcompany_operational_\d{8}T\d{6}Z\.dump$")


def _run(
    argv: list[str],
    *,
    timeout: int = 8,
    allowed_env: tuple[str, ...] = (),
) -> subprocess.CompletedProcess[str]:
    environment = {
        "PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
    }
    for name in allowed_env:
        value = os.environ.get(name)
        if value:
            environment[name] = value
    return subprocess.run(
        argv,
        check=False,
        capture_output=True,
        text=True,
        timeout=timeout,
        env=environment,
    )


def systemd_status(service: str) -> dict[str, Any]:
    if service not in _ALLOWED_SERVICES:
        raise ValueError("unsupported service")
    properties = (
        "LoadState,ActiveState,UnitFileState,MainPID,NRestarts,"
        "ExecMainStartTimestamp,ActiveEnterTimestampMonotonic"
    )
    try:
        result = _run(
            ["systemctl", "show", service, "--no-page", f"--property={properties}"]
        )
        values = _systemd_values(result.stdout)
        scope = "system"
        if values.get("LoadState") == "not-found":
            result = _run(
                [
                    "systemctl",
                    "--user",
                    "show",
                    service,
                    "--no-page",
                    f"--property={properties}",
                ],
                allowed_env=("XDG_RUNTIME_DIR", "DBUS_SESSION_BUS_ADDRESS"),
            )
            values = _systemd_values(result.stdout)
            scope = "user"
    except (OSError, subprocess.SubprocessError) as error:
        return {
            "service": service,
            "available": False,
            "active": False,
            "enabled": False,
            "pid": None,
            "restart_count": None,
            "started_at": None,
            "uptime_seconds": None,
            "error": type(error).__name__,
            "scope": None,
        }
    active_monotonic = int(values.get("ActiveEnterTimestampMonotonic", "0") or 0)
    uptime_seconds = (
        max(0, int(time.monotonic() - active_monotonic / 1_000_000))
        if active_monotonic and values.get("ActiveState") == "active"
        else None
    )
    return {
        "service": service,
        "available": result.returncode == 0,
        "active": values.get("ActiveState") == "active",
        "enabled": values.get("UnitFileState") == "enabled",
        "pid": int(values.get("MainPID", "0") or 0) or None,
        "restart_count": int(values.get("NRestarts", "0") or 0),
        "started_at": values.get("ExecMainStartTimestamp") or None,
        "uptime_seconds": uptime_seconds,
        "error": None if result.returncode == 0 else "systemd unavailable",
        "scope": scope,
    }


def _systemd_values(output: str) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in output.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value
    return values


def deployed_git_sha() -> str | None:
    configured = os.environ.get("ADMIN_DEPLOYED_SHA", "").strip()
    if re.fullmatch(r"[0-9a-f]{7,40}", configured):
        return configured
    try:
        result = _run(["git", "rev-parse", "HEAD"])
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if result.returncode == 0 and re.fullmatch(r"[0-9a-f]{40}", value) else None


def _directory_size(root: Path) -> int | None:
    try:
        total = 0
        for directory, _subdirectories, files in os.walk(root):
            for name in files:
                try:
                    total += (Path(directory) / name).stat().st_size
                except OSError:
                    continue
        return total
    except OSError:
        return None


def storage_status() -> dict[str, Any]:
    raw_root = Path(os.environ.get("FNS_RAW_ROOT", "var/raw/fns")).resolve()
    probe = raw_root if raw_root.exists() else raw_root.parent
    try:
        usage = shutil.disk_usage(probe)
        stat = probe.stat()
        return {
            "raw_root": str(raw_root),
            "raw_size": _directory_size(raw_root) if raw_root.exists() else 0,
            "disk_total": usage.total,
            "disk_used": usage.used,
            "disk_free": usage.free,
            "mount_identity": f"device:{stat.st_dev}",
            "available": True,
        }
    except OSError as error:
        return {
            "raw_root": str(raw_root),
            "raw_size": None,
            "disk_total": None,
            "disk_used": None,
            "disk_free": None,
            "mount_identity": None,
            "available": False,
            "error": type(error).__name__,
        }


def backup_directory() -> Path:
    return Path(
        os.environ.get("OPERATIONS_BACKUP_DIR", "/var/backups/nextcompany-operational")
    ).resolve()


def list_backups(*, limit: int = 50) -> list[dict[str, Any]]:
    root = backup_directory()
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for path in root.iterdir():
        if not path.is_file() or not _BACKUP_NAME.fullmatch(path.name):
            continue
        try:
            stat = path.stat()
        except OSError:
            continue
        checksum = None
        checksum_path = path.with_name(path.name + ".sha256")
        try:
            first = checksum_path.read_text(encoding="utf-8", errors="replace").split()[0]
            if re.fullmatch(r"[0-9a-f]{64}", first):
                checksum = first
        except (OSError, IndexError):
            pass
        restore_status: dict[str, Any] = {"status": "NOT RUN", "checked_at": None}
        restore_path = path.with_name(path.name + ".restore-test.json")
        try:
            payload = json.loads(restore_path.read_text(encoding="utf-8"))
            status = str(payload.get("status", "UNKNOWN"))[:30].upper()
            restore_status = {
                "status": status if status in {"PASS", "FAIL", "NOT CONFIGURED"} else "UNKNOWN",
                "checked_at": str(payload.get("checked_at") or "")[:40] or None,
            }
        except (OSError, ValueError, TypeError):
            pass
        rows.append(
            {
                "name": path.name,
                "timestamp": datetime.fromtimestamp(stat.st_mtime, tz=timezone.utc),
                "size": stat.st_size,
                "sha256": checksum,
                "restore_test": restore_status,
            }
        )
    rows.sort(key=lambda row: row["timestamp"], reverse=True)
    return rows[:limit]


def invoke_fixed_helper(kind: str) -> dict[str, Any]:
    if kind != "create_backup":
        raise ValueError("unsupported helper")
    # The path and argument vector are compile-time constants.  The release is
    # root-owned in production and the browser supplies neither a path nor args.
    candidates = (
        Path("/opt/nextcompany/current/deploy/scripts/backup_operational.sh"),
        Path("/home/mikhail/nextcompany-runtime/current/deploy/scripts/backup_operational.sh"),
    )
    helper = next((path for path in candidates if path.is_file()), candidates[0])
    try:
        result = _run(
            [str(helper)],
            timeout=120,
            allowed_env=(
                "DATABASE_URL",
                "OPERATIONS_BACKUP_DIR",
                "OPERATIONS_RESTORE_TEST_DATABASE_URL",
                "OPERATIONS_RESTORE_TEST_DATABASE",
            ),
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError(f"approved helper failed: {type(error).__name__}") from error
    if result.returncode != 0:
        message = (result.stderr or result.stdout or "helper failed").strip()[:400]
        raise RuntimeError(message)
    return {"status": "success", "output": result.stdout.strip()[:400]}
