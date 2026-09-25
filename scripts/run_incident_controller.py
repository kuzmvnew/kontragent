"""Run the persistent incident detector and safe remediation controller."""

from __future__ import annotations

import argparse
import json

from app.incidents.controller import controller_loop, run_controller_once


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=60)
    parser.add_argument("--source")
    args = parser.parse_args()
    if args.loop and args.once:
        parser.error("choose either --loop or --once")
    if args.loop:
        controller_loop(poll_seconds=args.poll_seconds)
        return
    print(json.dumps(run_controller_once(only_source=args.source), sort_keys=True))


if __name__ == "__main__":
    main()
