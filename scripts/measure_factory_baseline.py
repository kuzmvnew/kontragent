"""Print a read-only company-factory baseline as JSON."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from app.database.postgres import SessionLocal

try:
    from app.services.factory_metrics_service import collect_factory_metrics
except ModuleNotFoundError:  # Standalone copy used for pre-deploy live measurement.
    from factory_metrics_service import collect_factory_metrics


def _collect(window_hours: float) -> dict:
    with SessionLocal() as session:
        result = collect_factory_metrics(session, window_hours=window_hours)
        session.rollback()
        return result


def _process_snapshot() -> dict:
    completed = subprocess.run(
        ["ps", "-eo", "pid=,pcpu=,pmem=,rss=,args="],
        check=True,
        capture_output=True,
        text=True,
    )
    groups = {"worker": [], "public_sync": [], "postgres": []}
    for line in completed.stdout.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        fields = stripped.split(None, 4)
        if len(fields) < 5:
            continue
        pid, cpu, memory_percent, rss_kb, command = fields
        group = (
            "worker"
            if "scripts.run_data_readiness_scheduler" in command
            else "public_sync"
            if "scripts.run_public_sync" in command
            else "postgres"
            if "postgres" in command
            else None
        )
        if group:
            groups[group].append(
                {
                    "pid": int(pid),
                    "cpu_percent": float(cpu),
                    "memory_percent": float(memory_percent),
                    "rss_kb": int(rss_kb),
                }
            )
    return {
        group: {
            "processes": len(rows),
            "cpu_percent": round(sum(row["cpu_percent"] for row in rows), 3),
            "rss_kb": sum(row["rss_kb"] for row in rows),
            "members": rows,
        }
        for group, rows in groups.items()
    }


def _disk_snapshot() -> dict:
    raw_root = Path(os.environ.get("FIRMOTEKA_RAW_ROOT") or "/tmp")
    usage = shutil.disk_usage(raw_root if raw_root.exists() else raw_root.parent)
    return {
        "path": str(raw_root),
        "total_bytes": usage.total,
        "used_bytes": usage.used,
        "free_bytes": usage.free,
        "free_percent": round(usage.free * 100 / usage.total, 3),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--window-hours", type=float, default=1.0)
    parser.add_argument("--sample-seconds", type=int, default=0)
    args = parser.parse_args()
    if not 0 <= args.sample_seconds <= 60:
        parser.error("--sample-seconds must be between 0 and 60")
    first = _collect(args.window_hours)
    result = {
        "baseline": first,
        "processes": _process_snapshot(),
        "disk": _disk_snapshot(),
    }
    if args.sample_seconds:
        time.sleep(args.sample_seconds)
        second = _collect(args.window_hours)
        factor = 3600 / args.sample_seconds
        result["sample_end"] = second
        result["measured_growth"] = {
            "sample_seconds": args.sample_seconds,
            "postgres_bytes_per_hour": round(
                (
                    second["database"]["size_bytes"]
                    - first["database"]["size_bytes"]
                )
                * factor
            ),
            "known_raw_bytes_per_hour": round(
                (
                    second["storage"]["raw_bytes_known"]
                    - first["storage"]["raw_bytes_known"]
                )
                * factor
            ),
            "database_block_reads_per_hour": round(
                (second["database"]["blks_read"] - first["database"]["blks_read"])
                * factor
            ),
            "database_block_hits_per_hour": round(
                (second["database"]["blks_hit"] - first["database"]["blks_hit"])
                * factor
            ),
        }
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
