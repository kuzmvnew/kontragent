#!/usr/bin/env python3
"""Run Public Card and Workspace for the prepared Alan local preview."""

from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.local_real_preview_support import (
    INN,
    LIBPQ_ENDPOINT_ENVIRONMENT,
    require_preview_mode,
    validate_database_topology,
    verify_preview,
)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--public-port", type=int, default=8080)
    parser.add_argument("--workspace-port", type=int, default=8081)
    args = parser.parse_args()
    environment = dict(os.environ)
    require_preview_mode(environment)
    source_url = environment.get("ALAN_PREVIEW_SOURCE_DATABASE_URL", "")
    operational_url = environment.get("DATABASE_URL", "")
    public_url = environment.get("PUBLIC_DATABASE_URL", "")
    validate_database_topology(
        source_url=source_url,
        operational_url=operational_url,
        public_url=public_url,
        environment=environment,
    )
    # The guard has already rejected every inherited libpq endpoint source.
    # Remove them defensively before either server process is created.
    for name in LIBPQ_ENDPOINT_ENVIRONMENT:
        environment.pop(name, None)
    verify_preview(operational_url, public_url)

    public_origin = f"http://127.0.0.1:{args.public_port}"
    workspace_origin = f"http://127.0.0.1:{args.workspace_port}"
    environment.update(
        {
            "PUBLIC_ORIGIN": public_origin,
            "WORKSPACE_ORIGIN": workspace_origin,
            "PUBLIC_FORCE_NOINDEX": "1",
            "PUBLIC_TRUSTED_HOSTS": "127.0.0.1,localhost",
            "WORKSPACE_TRUSTED_HOSTS": "127.0.0.1,localhost",
            "NEXTCOMPANY_DEMO_MODE": "0",
        }
    )
    commands = (
        [
            sys.executable,
            "-m",
            "uvicorn",
            "public_app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.public_port),
        ],
        [
            sys.executable,
            "-m",
            "uvicorn",
            "workspace_app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(args.workspace_port),
        ],
    )
    processes = [subprocess.Popen(command, env=environment) for command in commands]

    def stop(_signal=None, _frame=None):
        for process in processes:
            if process.poll() is None:
                process.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    print(f"Public Card: {public_origin}/companies/{INN}", flush=True)
    print(
        f"Workspace: {workspace_origin}/login?return_to=%2Fapp%2Fcompanies%2F{INN}",
        flush=True,
    )
    try:
        while all(process.poll() is None for process in processes):
            time.sleep(0.25)
    finally:
        stop()
        for process in processes:
            process.wait(timeout=10)
    return next((process.returncode for process in processes if process.returncode), 0)


if __name__ == "__main__":
    raise SystemExit(main())
