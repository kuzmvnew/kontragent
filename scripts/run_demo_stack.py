#!/usr/bin/env python3
"""Run Public and Workspace Demo applications together in the foreground."""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.workspace_demo_support import require_demo_mode, validate_demo_topology  # noqa: E402


def _stop(processes: list[subprocess.Popen]) -> None:
    for process in processes:
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 10
    for process in processes:
        if process.poll() is not None:
            continue
        try:
            process.wait(timeout=max(0.1, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--public-port", type=int, default=8080)
    parser.add_argument("--workspace-port", type=int, default=8081)
    args = parser.parse_args()
    if not 1 <= args.public_port <= 65535 or not 1 <= args.workspace_port <= 65535:
        parser.error("ports must be between 1 and 65535")
    if args.public_port == args.workspace_port:
        parser.error("Public and Workspace ports must be different")

    require_demo_mode()
    validate_demo_topology(
        operational_url=os.getenv("DATABASE_URL"),
        public_import_url=os.getenv("PUBLIC_IMPORT_DATABASE_URL"),
        public_web_url=os.getenv("PUBLIC_DATABASE_URL"),
    )
    public_origin = f"http://127.0.0.1:{args.public_port}"
    workspace_origin = f"http://127.0.0.1:{args.workspace_port}"
    environment = os.environ.copy()
    environment.update(
        {
            "NEXTCOMPANY_DEMO_MODE": "1",
            "PUBLIC_FORCE_NOINDEX": "1",
            "PUBLIC_ORIGIN": public_origin,
            "WORKSPACE_ORIGIN": workspace_origin,
            "PUBLIC_TRUSTED_HOSTS": "127.0.0.1,localhost",
            "WORKSPACE_TRUSTED_HOSTS": "127.0.0.1,localhost",
            "PYTHONPATH": str(ROOT),
        }
    )
    commands = (
        (
            "Public",
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
        ),
        (
            "Workspace",
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
        ),
    )
    processes: list[subprocess.Popen] = []
    try:
        for _name, command in commands:
            processes.append(subprocess.Popen(command, cwd=ROOT, env=environment))
        print(f"Public:    {public_origin}")
        print(f"Workspace: {workspace_origin}")
        print("Demo stack runs in the foreground. Press Ctrl+C to stop both applications.")
        while True:
            for index, process in enumerate(processes):
                code = process.poll()
                if code is not None:
                    sibling = commands[index][0]
                    raise RuntimeError(f"{sibling} application exited with status {code}")
            time.sleep(0.25)
    except KeyboardInterrupt:
        return 0
    finally:
        _stop(processes)


if __name__ == "__main__":
    raise SystemExit(main())
