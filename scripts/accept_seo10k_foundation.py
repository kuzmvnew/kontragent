#!/usr/bin/env python3
"""Run scoped SEO foundation gates without claiming canonical release acceptance."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTRACT_PATH = ROOT / "SEO-10K_RELEASE_CONTRACT.md"
CONTRACT_SHA256 = "051b13f14a09d25f6bc9f7ec1079cab65bbc02db0c439e33838b32010bfdd599"
BASE_SHA = "475b3bd9bc12408017dfe3858fe7f261c0cee0cf"

RELEASE_GATES = {
    "SEO10K-A01": "Input integrity",
    "SEO10K-A02": "Entity scope",
    "SEO10K-A03": "Public boundary",
    "SEO10K-A04": "Numeric Index off",
    "SEO10K-A05": "Semantic compiler",
    "SEO10K-A06": "Demand evidence",
    "SEO10K-A07": "Cohort manifest",
    "SEO10K-A08": "Index eligibility",
    "SEO10K-A09": "SSR consistency",
    "SEO10K-A10": "URL contract",
    "SEO10K-A11": "Metadata",
    "SEO10K-A12": "Structured data",
    "SEO10K-A13": "Thin content",
    "SEO10K-A14": "Freshness/dispute",
    "SEO10K-A15": "robots/meta",
    "SEO10K-A16": "Sitemap",
    "SEO10K-A17": "Catalog/navigation",
    "SEO10K-A18": "Cache/republish",
    "SEO10K-A19": "Reliability",
    "SEO10K-A20": "Recovery",
    "SEO10K-A21": "Search consoles",
    "SEO10K-A22": "Legal/error path",
}

IMPLEMENTATION_GATE_SPECS = (
    (
        "SEOIMPL-A01",
        "SEO eligibility compiler",
        ("tests/test_public_seo.py::test_default_projection_passes_independent_compiler_not_legacy_flag",),
    ),
    (
        "SEOIMPL-A02",
        "public_ready fail-closed",
        ("tests/test_public_seo.py::test_public_ready_alone_fails_closed_without_semantic_evidence",),
    ),
    (
        "SEOIMPL-A03",
        "semantic minimum",
        (
            "tests/test_public_seo.py::test_semantic_and_thin_gates_are_deterministic",
            "tests/test_public_seo.py::test_unlinked_generated_action_text_cannot_satisfy_semantic_gate",
        ),
    ),
    (
        "SEOIMPL-A04",
        "thin-content protection",
        (
            "tests/test_public_seo.py::test_semantic_and_thin_gates_are_deterministic",
            "tests/test_public_seo.py::test_template_cluster_detection_flags_50_identical_non_identity_pages",
        ),
    ),
    (
        "SEOIMPL-A05",
        "metadata allowlist",
        ("tests/test_public_seo.py::test_metadata_jsonld_are_allowlisted_and_match_visible_identity",),
    ),
    (
        "SEOIMPL-A06",
        "JSON-LD visible parity",
        (
            "tests/test_public_seo.py::test_metadata_jsonld_are_allowlisted_and_match_visible_identity",
            "tests/test_public_web.py::test_robots_sitemap_canonical_open_graph_jsonld_and_404",
        ),
    ),
    (
        "SEOIMPL-A07",
        "canonical redirects and real 404",
        (
            "tests/test_public_web.py::test_company_url_normalization_and_tracking_parameters",
            "tests/test_public_web.py::test_production_host_normalizes_http_www_and_legacy_path_in_one_hop",
        ),
    ),
    (
        "SEOIMPL-A08",
        "robots and API X-Robots",
        (
            "tests/test_public_web.py::test_search_by_name_and_search_is_noindex",
            "tests/test_public_web.py::test_api_security_headers_and_no_cors",
        ),
    ),
    (
        "SEOIMPL-A09",
        "16 deterministic sitemap shards",
        (
            "tests/test_public_seo.py::test_sitemap_shard_is_first_sha256_nibble_and_stable",
            "tests/test_public_web.py::test_robots_sitemap_canonical_open_graph_jsonld_and_404",
        ),
    ),
    (
        "SEOIMPL-A10",
        "catalog and pagination",
        (
            "tests/test_public_seo.py::test_catalog_pagination_has_first_nearby_previous_and_next_links",
            "tests/test_public_web.py::test_catalog_is_crawlable_and_query_variants_are_noindex",
        ),
    ),
    (
        "SEOIMPL-A11",
        "atomic company revision snapshot",
        ("tests/test_public_release_postgres.py::test_active_release_switch_never_mixes_company_snapshot",),
    ),
    (
        "SEOIMPL-A12",
        "mixed-version fail-closed authority",
        (
            "tests/test_public_release_postgres.py::test_partial_seo_storage_and_stored_noindex_are_fail_closed",
            "tests/test_public_release_postgres.py::test_revision_mismatch_cannot_enter_page_sitemap_or_catalog",
        ),
    ),
    (
        "SEOIMPL-A13",
        "legacy and native reimport idempotency",
        (
            "tests/test_public_release_postgres.py::test_legacy_v1_reimport_after_v2_is_idempotent_without_backfill",
            "tests/test_public_release_postgres.py::test_native_v2_reimport_verifies_derived_state_and_detects_tamper",
            "tests/test_public_release_postgres.py::test_checksum_corruption_preserves_active_release",
        ),
    ),
    (
        "SEOIMPL-A14",
        "public Numeric Index hard ban",
        (
            "tests/test_public_seo.py::test_numeric_next_index_is_recursively_banned",
            "tests/test_public_semantic.py::test_public_numeric_index_is_fail_closed_and_absent_from_semantic_contract",
            "tests/test_public_web.py::test_numeric_index_payload_is_noindex_sanitized_and_absent_from_discovery",
        ),
    ),
    (
        "SEOIMPL-A15",
        "public_0002 data-preserving migration",
        (
            "tests/test_public_seo_migration.py::test_public_0002_upgrade_downgrade_are_symmetric_and_preserve_existing_rows",
            "tests/test_public_seo_migration.py::test_importer_detects_old_and_new_public_schema_without_guessing",
        ),
    ),
    (
        "SEOIMPL-A16",
        "search-visible hash and no-op republish",
        ("tests/test_public_seo.py::test_search_visible_hash_ignores_technical_republish_but_changes_with_visible_fact",),
    ),
    (
        "SEOIMPL-A17",
        "explicit SEO release-cohort authority",
        ("tests/test_public_release_postgres.py::test_active_public_release_without_seo_cohort_is_not_index_authority",),
    ),
)


def _git_value(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def _command(test_ids: tuple[str, ...]) -> list[str]:
    return [sys.executable, "-m", "pytest", *test_ids, "-q", "-rA"]


def _result_summary(output: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return lines[-1] if lines else "no pytest summary"


def run_gate(test_ids: tuple[str, ...], *, no_run: bool) -> dict:
    command = _command(test_ids)
    portable_command = ["python", "-m", "pytest", *test_ids, "-q", "-rA"]
    if no_run:
        return {
            "status": "NOT_RUN",
            "command": portable_command,
            "returncode": None,
            "summary": "not executed by request",
        }
    completed = subprocess.run(
        command,
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    summary = _result_summary(completed.stdout)
    skipped = bool(re.search(r"\b\d+ skipped\b", completed.stdout))
    status = "PASS" if completed.returncode == 0 and not skipped else (
        "NOT_RUN" if completed.returncode == 0 else "FAIL"
    )
    return {
        "status": status,
        "command": portable_command,
        "returncode": completed.returncode,
        "summary": summary,
    }


def build_evidence(
    *,
    head_sha: str,
    source_tree_sha: str,
    gate_results: dict[str, dict],
    generated_at: str | None = None,
) -> dict:
    implementation_gates = {}
    test_runs = []
    for gate_id, description, test_ids in IMPLEMENTATION_GATE_SPECS:
        result = gate_results[gate_id]
        implementation_gates[gate_id] = {
            "description": description,
            "status": result["status"],
            "test_ids": list(test_ids),
            "evidence_command": result["command"],
            "result_summary": result["summary"],
        }
        test_runs.append(
            {
                "gate_id": gate_id,
                "command": result["command"],
                "returncode": result["returncode"],
                "status": result["status"],
                "summary": result["summary"],
            }
        )
    release_gates = {
        gate_id: {
            "name": name,
            "status": "NOT_RUN",
            "evidence": None,
            "release_blocking": True,
        }
        for gate_id, name in RELEASE_GATES.items()
    }
    return {
        "task_id": "SEO-10K-IMPL-01-A-CORRECTION-01",
        "repository": "kuzmvnew/kontragent",
        "base_sha": BASE_SHA,
        "head_sha": head_sha,
        "source_tree_sha": source_tree_sha,
        "evidence_head_identity": {
            "binding": "source_commit",
            "meaning": "head_sha is the immutable tested code commit",
            "envelope_rule": (
                "the final evidence commit must be a direct child of head_sha "
                "and may change only docs/evidence/SEO-10K-IMPL-01-A.json"
            ),
        },
        "contract_version": "1.0",
        "canonical_contract_sha256": CONTRACT_SHA256,
        "generated_at": generated_at or datetime.now(timezone.utc).isoformat(),
        "test_runs": test_runs,
        "implementation_gates": implementation_gates,
        "release_gates": release_gates,
        "no_averaged_score": True,
        "release_blocked": True,
        "live_wordstat": "NOT_RUN",
        "live_google": "NOT_RUN",
        "waves": {
            "500": "NOT_RELEASED",
            "2000": "NOT_RELEASED",
            "10000": "NOT_RELEASED",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--no-run", action="store_true")
    args = parser.parse_args()

    observed_contract_sha = hashlib.sha256(CONTRACT_PATH.read_bytes()).hexdigest()
    if observed_contract_sha != CONTRACT_SHA256:
        raise SystemExit("canonical SEO contract identity mismatch")
    head_sha = _git_value("rev-parse", "HEAD")
    source_tree_sha = _git_value("rev-parse", "HEAD^{tree}")
    gate_results = {
        gate_id: run_gate(test_ids, no_run=args.no_run)
        for gate_id, _description, test_ids in IMPLEMENTATION_GATE_SPECS
    }
    evidence = build_evidence(
        head_sha=head_sha,
        source_tree_sha=source_tree_sha,
        gate_results=gate_results,
    )
    serialized = json.dumps(evidence, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        output = args.output if args.output.is_absolute() else ROOT / args.output
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(serialized, encoding="utf-8")
    print(serialized, end="")
    return 0 if all(
        item["status"] == "PASS" for item in evidence["implementation_gates"].values()
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
