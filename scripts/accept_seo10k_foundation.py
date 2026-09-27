#!/usr/bin/env python3
"""Run local SEO foundation checks and emit a non-averaged A01-A22 matrix."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


FOCUSED_TESTS = (
    "tests/test_public_seo.py",
    "tests/test_public_web.py",
    "tests/test_public_semantic.py",
    "tests/test_public_projection.py",
    "tests/test_public_seo_migration.py",
    "tests/test_seo10k_acceptance_evidence.py",
)

LOCAL_GATES = {
    "A01": "SEO eligibility compiler",
    "A02": "public_ready alone fails closed",
    "A03": "semantic evidence gate",
    "A04": "thin-content gate and cluster detection",
    "A05": "allowlisted metadata",
    "A08": "visible-parity JSON-LD",
    "A09": "canonical and redirect contract",
    "A10": "robots and API X-Robots contract",
    "A11": "sitemap index membership",
    "A12": "16 deterministic shards",
    "A13": "crawlable catalog",
    "A14": "catalog pagination",
    "A15": "stale/disputed safety",
    "A16": "search-visible hash",
    "A17": "numeric index hard ban",
    "A18": "current public regression",
}

NOT_RUN_GATES = {
    "A06": "live Wordstat evidence",
    "A07": "live Google historical metrics",
    "A19": "production performance and 5x bot peak",
    "A20": "Search Console production observation",
    "A21": "Yandex Webmaster production observation",
    "A22": "production cohort release and deployment",
}


def build_matrix(*, focused_passed: bool, command: list[str]) -> dict:
    gates = {}
    for gate, description in LOCAL_GATES.items():
        gates[gate] = {
            "status": "PASS" if focused_passed else "FAIL",
            "description": description,
            "evidence": " ".join(command),
        }
    for gate, description in NOT_RUN_GATES.items():
        gates[gate] = {
            "status": "NOT_RUN",
            "description": description,
            "evidence": None,
            "release_blocking": True,
        }
    ordered = {gate: gates[gate] for gate in (f"A{number:02d}" for number in range(1, 23))}
    return {
        "task": "SEO-10K-IMPL-01-A",
        "contract_version": "1.0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "no_averaged_score": True,
        "release_blocked": any(item["status"] != "PASS" for item in ordered.values()),
        "waves": {"500": "NOT_RELEASED", "2000": "NOT_RELEASED", "10000": "NOT_RELEASED"},
        "gates": ordered,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-run", action="store_true", help="emit FAIL for local gates without invoking pytest")
    args = parser.parse_args()
    command = [sys.executable, "-m", "pytest", *FOCUSED_TESTS, "-q"]
    evidence_command = ["python", "-m", "pytest", *FOCUSED_TESTS, "-q"]
    passed = False
    if not args.no_run:
        completed = subprocess.run(command, check=False)
        passed = completed.returncode == 0
    evidence = build_matrix(focused_passed=passed, command=evidence_command)
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())
