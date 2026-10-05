from __future__ import annotations

from copy import deepcopy
from datetime import timedelta

import pytest

from scripts import project_status as status_tool


@pytest.fixture
def canonical_status() -> dict:
    return status_tool.load_yaml(status_tool.DEFAULT_STATUS)


@pytest.fixture
def schema() -> dict:
    return status_tool.load_schema(status_tool.DEFAULT_SCHEMA)


def verified_evidence(kind: str = "runtime_observation") -> list[dict[str, str]]:
    return [
        {
            "status": "VERIFIED",
            "kind": kind,
            "reference": "test evidence",
            "observed_at": "2026-09-29T09:00:00Z",
        }
    ]


def test_valid_fresh_status(canonical_status: dict, schema: dict) -> None:
    now = status_tool.utc_now()
    canonical_status["generated_at"] = status_tool.timestamp(now)

    status_tool.validate_schema(canonical_status, schema)
    status_tool.validate_semantics(canonical_status)
    status_tool.validate_freshness(canonical_status, now)


def test_schema_document_is_valid(schema: dict) -> None:
    status_tool.Draft202012Validator.check_schema(schema)


def test_status_older_than_24_hours_is_stale(canonical_status: dict) -> None:
    generated_at = status_tool.parse_timestamp(canonical_status["generated_at"])

    with pytest.raises(status_tool.StatusError, match="status is stale"):
        status_tool.validate_freshness(
            canonical_status, generated_at + timedelta(hours=24, seconds=1)
        )


def test_generated_markdown_drift_detected(
    canonical_status: dict, tmp_path
) -> None:
    generated = tmp_path / "PROJECT_STATUS.md"
    generated.write_text("manually edited\n", encoding="utf-8")

    with pytest.raises(status_tool.StatusError, match="Markdown drift"):
        status_tool.check_parity(
            generated, status_tool.render_markdown(canonical_status)
        )


def test_merge_without_deploy_is_allowed(
    canonical_status: dict, schema: dict
) -> None:
    candidate = deepcopy(canonical_status)
    candidate["code"]["capabilities"].append(
        {
            "id": "merged-only-test",
            "name": "Merged only",
            "status": "MERGED",
            "evidence": verified_evidence("git"),
        }
    )
    candidate["production"]["capabilities"] = [
        {
            "id": "merged-only-test",
            "status": "NOT_DEPLOYED",
            "evidence": [
                {
                    "status": "NOT_VERIFIED",
                    "kind": "production_handoff",
                    "reference": "No production deployment evidence",
                    "observed_at": "2026-09-29T09:00:00Z",
                }
            ],
        }
    ]

    status_tool.validate_schema(candidate, schema)
    status_tool.validate_semantics(candidate)


def test_qa_without_deploy_is_allowed(canonical_status: dict, schema: dict) -> None:
    candidate = deepcopy(canonical_status)
    candidate["qa"].update(
        {
            "state": "ACCEPTED",
            "accepted_head_sha": candidate["main_sha"],
            "qa_task_id": "QA-TEST-01",
            "verdict": "PASS",
            "evidence_reference": "QA-TEST-01 report",
            "merge_allowed": True,
            "tested_at": "2026-09-29T09:00:00Z",
            "evidence": verified_evidence("qa_task"),
        }
    )
    candidate["production"]["runtime_sha"] = "UNKNOWN"
    candidate["qa"]["capabilities"] = [
        {
            "id": "qa-only-test",
            "status": "ACCEPTED",
            "evidence": verified_evidence("qa_task"),
        }
    ]
    candidate["production"]["capabilities"] = [
        {
            "id": "qa-only-test",
            "status": "NOT_DEPLOYED",
            "evidence": [
                {
                    "status": "NOT_VERIFIED",
                    "kind": "production_handoff",
                    "reference": "QA acceptance is not deployment evidence",
                    "observed_at": "2026-09-29T09:00:00Z",
                }
            ],
        }
    ]

    status_tool.validate_schema(candidate, schema)
    status_tool.validate_semantics(candidate)


def test_production_without_user_visible_is_allowed(
    canonical_status: dict, schema: dict
) -> None:
    candidate = deepcopy(canonical_status)
    candidate["code"]["capabilities"].append(
        {
            "id": "deployed-not-visible",
            "name": "Deployed but not externally visible",
            "status": "MERGED",
            "evidence": verified_evidence("git"),
        }
    )
    candidate["qa"].update(
        {
            "state": "ACCEPTED",
            "accepted_head_sha": candidate["main_sha"],
            "qa_task_id": "QA-TEST-02",
            "verdict": "PASS",
            "evidence_reference": "QA-TEST-02 report",
            "merge_allowed": True,
            "tested_at": "2026-09-29T09:00:00Z",
            "evidence": verified_evidence("qa_task"),
            "capabilities": [
                {
                    "id": "deployed-not-visible",
                    "status": "ACCEPTED",
                    "evidence": verified_evidence("qa_task"),
                }
            ],
        }
    )
    production = candidate["production"]
    production.update(
        {
            "state": "VERIFIED",
            "runtime_sha": "a" * 40,
            "operational_db_revision": "b9e2c4d6f8a0",
            "public_db_revision": "public_0002",
            "semantic_ready_count": 1,
            "public_ready_count": 1,
            "last_production_acceptance_task": "PROD-ACCEPT-01",
            "deployed_at": "2026-09-29T08:00:00Z",
            "evidence": verified_evidence("production_handoff"),
            "capabilities": [
                {
                    "id": "deployed-not-visible",
                    "status": "DEPLOYED",
                    "evidence": verified_evidence("production_handoff"),
                }
            ],
        }
    )
    for key in (
        "operational_db_revision",
        "public_db_revision",
        "active_release_id",
        "active_release_record_count",
        "active_release_cohort",
        "semantic_ready_count",
        "public_ready_count",
    ):
        candidate["data_state"][key] = production[key]
    candidate["user_visible"].update(
        {
            "state": "NOT_VERIFIED",
            "public_site_state": "NOT_VERIFIED",
            "external_api_state": "NOT_VERIFIED",
            "ssr_state": "NOT_VERIFIED",
            "active_release_id": "UNKNOWN",
            "external_verified_at": "UNKNOWN",
            "known_withheld_or_depublished": {
                "state": "NOT_VERIFIED",
                "companies": [],
                "issues": [],
            },
            "evidence": [
                {
                    "status": "NOT_VERIFIED",
                    "kind": "runtime_observation",
                    "reference": "No independent external observation",
                    "observed_at": "2026-09-29T09:00:00Z",
                }
            ],
            "capabilities": [
                {
                    "id": "deployed-not-visible",
                    "status": "NOT_VISIBLE",
                    "evidence": [
                        {
                            "status": "NOT_VERIFIED",
                            "kind": "runtime_observation",
                            "reference": "No independent external observation",
                            "observed_at": "2026-09-29T09:00:00Z",
                        }
                    ],
                }
            ],
        }
    )

    status_tool.validate_schema(candidate, schema)
    status_tool.validate_semantics(candidate)


def test_inferred_deployment_is_rejected(canonical_status: dict) -> None:
    candidate = deepcopy(canonical_status)
    candidate["production"]["runtime_sha"] = "UNKNOWN"
    candidate["production"]["capabilities"] = [
        {
            "id": "invalid-inference",
            "status": "DEPLOYED",
            "evidence": verified_evidence("git"),
        }
    ]

    errors = status_tool.semantic_errors(candidate)

    assert any("runtime_sha" in error for error in errors)
    assert any("cannot be inferred" in error for error in errors)


def test_code_layer_rejects_deployed_property(
    canonical_status: dict, schema: dict
) -> None:
    candidate = deepcopy(canonical_status)
    candidate["code"]["capabilities"][0]["deployed"] = True

    with pytest.raises(status_tool.StatusError, match="schema validation failed"):
        status_tool.validate_schema(candidate, schema)


@pytest.mark.parametrize(
    "merge_subject",
    (
        "Merge pull request #109 from example/status",
        "Merge PR #109: status update",
    ),
)
def test_repository_collection_preserves_unknown_runtime_evidence(
    canonical_status: dict, monkeypatch, merge_subject: str
) -> None:
    def fake_git(*args: str) -> str:
        if args[:2] == ("rev-parse", "test-main"):
            return "b" * 40
        if args[:2] == ("log", "--first-parent"):
            return (
                f"{'a' * 40}\x1f2026-09-29T10:00:00Z\x1f"
                + merge_subject
            )
        if args == ("remote", "get-url", "origin"):
            return "https://github.com/kuzmvnew/kontragent.git"
        if args[:4] == ("ls-tree", "-r", "--name-only", "test-main"):
            return f"{args[-1]}/base.py"
        if args == ("show", "test-main:migrations/versions/base.py"):
            return 'revision = "main-operational-head"\ndown_revision = None'
        if args == ("show", "test-main:public_migrations/versions/base.py"):
            return 'revision = "main-public-head"\ndown_revision = None'
        raise AssertionError(f"unexpected git call: {args}")

    monkeypatch.setattr(status_tool, "git", fake_git)
    collected = status_tool.collect_repository(canonical_status, "test-main")

    assert collected["main_sha"] == "b" * 40
    assert collected["code"]["latest_relevant_merged_pr"]["number"] == 109
    assert collected["code"]["migration_heads"]["operational"] == "main-operational-head"
    assert collected["code"]["migration_heads"]["public"] == "main-public-head"
    assert collected["production"]["runtime_sha"] == "UNKNOWN"
    assert collected["production"]["operational_db_revision"] == "UNKNOWN"
    assert any(
        item["status"] == "NOT_VERIFIED"
        for item in collected["production"]["evidence"]
    )


def test_public_runtime_collection_updates_observed_fields_and_preserves_unknowns(
    canonical_status: dict, schema: dict
) -> None:
    observation = {
        "base_url": "https://status.example",
        "observed_at": "2026-09-29T10:00:00Z",
        "release_id": "release-test-01",
        "record_count": 7,
    }

    collected = status_tool.apply_public_runtime(canonical_status, observation)

    assert collected["generated_at"] == observation["observed_at"]
    assert collected["production"]["active_release_id"] == "release-test-01"
    assert collected["production"]["active_release_record_count"] == 7
    assert collected["production"]["runtime_sha"] == "UNKNOWN"
    assert collected["data_state"]["active_release_id"] == "release-test-01"
    assert collected["user_visible"]["external_api_state"] == "LIVE"
    assert collected["user_visible"]["external_verified_at"] == observation["observed_at"]
    status_tool.validate_schema(collected, schema)
    status_tool.validate_semantics(collected)


def test_render_is_deterministic(canonical_status: dict) -> None:
    assert status_tool.render_markdown(canonical_status) == status_tool.render_markdown(
        deepcopy(canonical_status)
    )
