from __future__ import annotations

import copy
import json

import pytest

from scripts.qa_attestation import (
    AttestationError,
    BEGIN_MARKER,
    END_MARKER,
    evaluate_attestations,
    main,
    parse_comment_block,
)


BASE_A = "a" * 40
BASE_B = "b" * 40
HEAD_A = "c" * 40
HEAD_B = "d" * 40


def policy(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "policy_version": "1.0",
        "allowed_issuer_logins": ["kuzmvnew"],
        "repository_owner_logins": ["kuzmvnew"],
        "require_current_base_sha": False,
        "require_independent_reviewer": False,
    }
    value.update(overrides)
    return value


def attestation(**overrides: object) -> dict[str, object]:
    value: dict[str, object] = {
        "schema_version": "1.0",
        "task_id": "GITHUB-QA-ATTESTATION-01",
        "repository": "kuzmvnew/kontragent",
        "pr_number": 123,
        "base_sha": BASE_A,
        "head_sha": HEAD_A,
        "qa_verdict": "PASS",
        "tested_at": "2026-09-29T08:30:00Z",
        "tests": [
            {
                "name": "focused",
                "command": "uv run python -m pytest tests/test_qa_attestation.py -q",
                "result": "PASS",
                "counts": {"passed": 10, "failed": 0, "skipped": 0, "total": 10},
            }
        ],
        "evidence": [
            {
                "kind": "RUN",
                "id": "github-actions:123456789",
                "url": "https://github.com/kuzmvnew/kontragent/actions/runs/123456789",
            }
        ],
        "merge_allowed": True,
        "deploy_allowed": False,
        "issuer": {
            "login": "kuzmvnew",
            "identity_source": "GITHUB_COMMENT_AUTHOR",
            "repository_role": "OWNER",
        },
        "reviewer": {
            "login": "kuzmvnew",
            "repository_role": "OWNER",
            "independence": "INDEPENDENT_REVIEWER_NOT_ESTABLISHED",
        },
    }
    value.update(overrides)
    return value


def comment(payload: dict[str, object], *, comment_id: int = 1) -> dict[str, object]:
    return {
        "id": comment_id,
        "body": (
            "## QA attestation\n\n"
            "The JSON block is authoritative.\n\n"
            f"{BEGIN_MARKER}\n"
            "```json\n"
            f"{json.dumps(payload, indent=2, sort_keys=True)}\n"
            "```\n"
            f"{END_MARKER}"
        ),
        "author_login": "kuzmvnew",
        "author_association": "OWNER",
        "html_url": f"https://github.com/kuzmvnew/kontragent/pull/123#issuecomment-{comment_id}",
    }


def snapshot(
    comments: list[dict[str, object]] | None = None,
    *,
    base_sha: str = BASE_A,
    head_sha: str = HEAD_A,
) -> dict[str, object]:
    return {
        "repository": "kuzmvnew/kontragent",
        "pr_number": 123,
        "current_base_sha": base_sha,
        "current_head_sha": head_sha,
        "comments": comments or [],
    }


def test_matching_exact_head_passes() -> None:
    result = evaluate_attestations(snapshot([comment(attestation())]), policy())

    assert result["state"] == "PASS"
    assert result["reason"] == "CURRENT_HEAD_ACCEPTED"
    assert result["attested_head_sha"] == HEAD_A
    assert result["attested_base_sha"] == BASE_A


def test_missing_attestation_is_missing() -> None:
    result = evaluate_attestations(snapshot(), policy())

    assert result["state"] == "MISSING"
    assert result["reason"] == "NO_TRUSTED_ATTESTATION"


def test_fail_verdict_fails() -> None:
    payload = attestation(qa_verdict="FAIL")

    result = evaluate_attestations(snapshot([comment(payload)]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "QA_VERDICT_NOT_PASS"


def test_merge_allowed_false_fails() -> None:
    payload = attestation(merge_allowed=False)

    result = evaluate_attestations(snapshot([comment(payload)]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "MERGE_NOT_ALLOWED"


def test_new_head_invalidates_old_pass() -> None:
    old_pass = comment(attestation(head_sha=HEAD_A))

    first = evaluate_attestations(snapshot([old_pass], head_sha=HEAD_A), policy())
    after_synchronize = evaluate_attestations(snapshot([old_pass], head_sha=HEAD_B), policy())

    assert first["state"] == "PASS"
    assert after_synchronize["state"] == "STALE"
    assert after_synchronize["reason"] == "HEAD_SHA_MISMATCH"


def test_different_pr_cannot_reuse_attestation() -> None:
    wrong_pr = comment(attestation(pr_number=999))

    result = evaluate_attestations(snapshot([wrong_pr]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "ATTESTATION_CONTEXT_MISMATCH"


def test_different_sha_cannot_reuse_attestation() -> None:
    wrong_sha = comment(attestation(head_sha=HEAD_B))

    result = evaluate_attestations(snapshot([wrong_sha]), policy())

    assert result["state"] == "STALE"
    assert result["reason"] == "HEAD_SHA_MISMATCH"


def test_deploy_allowed_is_independent_of_merge_acceptance() -> None:
    payload = attestation(merge_allowed=True, deploy_allowed=False)

    result = evaluate_attestations(snapshot([comment(payload)]), policy())

    assert result["state"] == "PASS"
    assert result["merge_allowed"] is True
    assert result["deploy_allowed"] is False


def test_malformed_block_is_rejected_fail_closed() -> None:
    malformed = comment(attestation())
    malformed["body"] = f"{BEGIN_MARKER}\nnot-json\n{END_MARKER}"

    result = evaluate_attestations(snapshot([malformed]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "MALFORMED_TRUSTED_ATTESTATION"


def test_duplicate_current_attestations_fail_closed() -> None:
    first = comment(attestation(), comment_id=1)
    second = comment(attestation(), comment_id=2)

    result = evaluate_attestations(snapshot([first, second]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "DUPLICATE_CURRENT_ATTESTATIONS"


def test_conflicting_current_attestations_fail_closed() -> None:
    accepted = comment(attestation(), comment_id=1)
    rejected = comment(attestation(qa_verdict="FAIL"), comment_id=2)

    result = evaluate_attestations(snapshot([accepted, rejected]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "DUPLICATE_CURRENT_ATTESTATIONS"


def test_base_sha_is_preserved_and_can_be_strictly_bound() -> None:
    tested_against_old_base = comment(attestation(base_sha=BASE_A))

    advisory = evaluate_attestations(
        snapshot([tested_against_old_base], base_sha=BASE_B),
        policy(require_current_base_sha=False),
    )
    strict = evaluate_attestations(
        snapshot([tested_against_old_base], base_sha=BASE_B),
        policy(require_current_base_sha=True),
    )

    assert advisory["state"] == "PASS"
    assert advisory["attested_base_sha"] == BASE_A
    assert advisory["current_base_sha"] == BASE_B
    assert advisory["warnings"] == ["TESTED_BASE_DIFFERS_FROM_CURRENT_BASE"]
    assert strict["state"] == "STALE"
    assert strict["reason"] == "BASE_SHA_MISMATCH"


def test_same_owner_cannot_claim_independent_review() -> None:
    reviewer = copy.deepcopy(attestation()["reviewer"])
    assert isinstance(reviewer, dict)
    reviewer["independence"] = "INDEPENDENT_REVIEWER_ESTABLISHED"
    payload = attestation(reviewer=reviewer)

    result = evaluate_attestations(snapshot([comment(payload)]), policy())

    assert result["state"] == "FAIL"
    assert result["reason"] == "MALFORMED_TRUSTED_ATTESTATION"
    assert "cannot be represented as independent" in result["diagnostics"][0]


def test_independence_can_be_configured_as_required() -> None:
    result = evaluate_attestations(
        snapshot([comment(attestation())]),
        policy(require_independent_reviewer=True),
    )

    assert result["state"] == "FAIL"
    assert result["reason"] == "INDEPENDENT_REVIEWER_REQUIRED"


def test_untrusted_comment_cannot_forge_acceptance_or_block() -> None:
    forged = comment(attestation())
    forged["author_login"] = "untrusted-contributor"
    forged["author_association"] = "CONTRIBUTOR"

    result = evaluate_attestations(snapshot([forged]), policy())

    assert result["state"] == "MISSING"


def test_duplicate_json_key_is_rejected() -> None:
    body = (
        f"{BEGIN_MARKER}\n"
        "```json\n"
        '{"schema_version":"1.0","schema_version":"1.0"}\n'
        "```\n"
        f"{END_MARKER}"
    )

    with pytest.raises(AttestationError, match="duplicate JSON key"):
        parse_comment_block(body)


@pytest.mark.parametrize(
    ("comments", "expected_exit", "expected_state"),
    [
        ([], 1, "MISSING"),
        ([comment(attestation())], 0, "PASS"),
    ],
)
def test_cli_exit_code_enforces_the_decision(
    tmp_path, comments, expected_exit: int, expected_state: str
) -> None:
    input_path = tmp_path / "snapshot.json"
    policy_path = tmp_path / "policy.json"
    output_path = tmp_path / "result.json"
    input_path.write_text(json.dumps(snapshot(comments)), encoding="utf-8")
    policy_path.write_text(json.dumps(policy()), encoding="utf-8")

    exit_code = main(
        [
            "evaluate",
            "--input",
            str(input_path),
            "--policy",
            str(policy_path),
            "--output",
            str(output_path),
        ]
    )

    assert exit_code == expected_exit
    assert json.loads(output_path.read_text(encoding="utf-8"))["state"] == expected_state
