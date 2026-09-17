"""Run the data-readiness scheduler once or as a supervised worker."""

import argparse
import json

from app.services.data_readiness_scheduler import run_due_updates, worker_loop


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--loop", action="store_true", help="Run a supervised polling loop")
    parser.add_argument("--poll-seconds", type=int, default=60)
    args = parser.parse_args()
    if args.loop:
        worker_loop(poll_seconds=args.poll_seconds)
    else:
        print(json.dumps(run_due_updates(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
