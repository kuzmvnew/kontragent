"""Validate SHA-bound QA attestations posted as GitHub PR comments.

This module deliberately uses only the Python standard library.  The trusted
GitHub workflow checks it out from the default branch; it never imports or
executes code from the pull request under test.
"""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


BEGIN_MARKER = "<!-- QA-ATTESTATION:BEGIN v1 -->"
END_MARKER = "<!-- QA-ATTESTATION:END v1 -->"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
REPOSITORY_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
LOGIN_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")

ATTESTATION_KEYS = {
    "schema_version",
    "task_id",
    "repository",
    "pr_number",
    "base_sha",
    "head_sha",
    "qa_verdict",
    "tested_at",
    "tests",
    "evidence",
    "merge_allowed",
    "deploy_allowed",
    "issuer",
    "reviewer",
}
TEST_KEYS = {"name", "command", "result", "counts"}
COUNT_KEYS = {"passed", "failed", "skipped", "total"}
EVIDENCE_REQUIRED_KEYS = {"kind", "id"}
EVIDENCE_ALLOWED_KEYS = EVIDENCE_REQUIRED_KEYS | {"sha256", "url"}
ISSUER_KEYS = {"login", "identity_source", "repository_role"}
REVIEWER_KEYS = {"login", "repository_role", "independence"}

VERDICTS = {"PASS", "FAIL"}
EVIDENCE_KINDS = {"ARTIFACT", "HASH", "LOG", "REPORT", "RUN", "URL", "OTHER"}
REPOSITORY_ROLES = {"OWNER", "MEMBER", "COLLABORATOR", "EXTERNAL", "UNKNOWN"}
INDEPENDENCE_STATES = {
    "INDEPENDENT_REVIEWER_ESTABLISHED",
    "INDEPENDENT_REVIEWER_NOT_ESTABLISHED",
}


class AttestationError(ValueError):
    """A fail-closed contract or binding error."""


def _object_without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AttestationError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def parse_comment_block(body: str) -> dict[str, Any] | None:
    """Return the single structured payload, None, or raise on malformed blocks."""

    if BEGIN_MARKER not in body and END_MARKER not in body:
        return None
    if body.count(BEGIN_MARKER) != 1 or body.count(END_MARKER) != 1:
        raise AttestationError("comment must contain exactly one QA attestation block")

    start = body.index(BEGIN_MARKER) + len(BEGIN_MARKER)
    end = body.index(END_MARKER)
    if end <= start:
        raise AttestationError("QA attestation markers are out of order")

    block = body[start:end].strip()
    match = re.fullmatch(r"```json\s*\n(?P<payload>.*)\n```", block, re.DOTALL)
    if not match:
        raise AttestationError("QA attestation block must contain one fenced JSON payload")
    try:
        payload = json.loads(
            match.group("payload"),
            object_pairs_hook=_object_without_duplicate_keys,
        )
    except (json.JSONDecodeError, AttestationError) as exc:
        raise AttestationError(f"invalid QA attestation JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise AttestationError("QA attestation payload must be a JSON object")
    return payload


def _require_exact_keys(value: dict[str, Any], expected: set[str], path: str) -> None:
    missing = expected - value.keys()
    extra = value.keys() - expected
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing={sorted(missing)}")
        if extra:
            details.append(f"extra={sorted(extra)}")
        raise AttestationError(f"{path} has invalid fields ({', '.join(details)})")


def _require_string(value: Any, path: str, *, max_length: int) -> str:
    if not isinstance(value, str) or not value or len(value) > max_length:
        raise AttestationError(f"{path} must be a non-empty string up to {max_length} characters")
    return value


def _require_enum(value: Any, allowed: set[str], path: str) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise AttestationError(f"{path} must be one of {sorted(allowed)}")
    return value


def _require_nonnegative_integer(value: Any, path: str) -> int:
    if type(value) is not int or value < 0:
        raise AttestationError(f"{path} must be a non-negative integer")
    return value


def _comment_role(author_association: Any) -> str:
    if author_association in {"OWNER", "MEMBER", "COLLABORATOR"}:
        return str(author_association)
    if isinstance(author_association, str) and author_association:
        return "EXTERNAL"
    return "UNKNOWN"


def _validate_policy(policy: dict[str, Any]) -> None:
    expected = {
        "policy_version",
        "allowed_issuer_logins",
        "repository_owner_logins",
        "require_current_base_sha",
        "require_independent_reviewer",
    }
    _require_exact_keys(policy, expected, "policy")
    if policy["policy_version"] != "1.0":
        raise AttestationError("unsupported policy_version")
    for key in ("allowed_issuer_logins", "repository_owner_logins"):
        values = policy[key]
        if not isinstance(values, list) or not values:
            raise AttestationError(f"policy.{key} must be a non-empty list")
        if any(not isinstance(value, str) or not LOGIN_RE.fullmatch(value) for value in values):
            raise AttestationError(f"policy.{key} contains an invalid GitHub login")
    for key in ("require_current_base_sha", "require_independent_reviewer"):
        if type(policy[key]) is not bool:
            raise AttestationError(f"policy.{key} must be boolean")


def validate_attestation(
    payload: dict[str, Any],
    *,
    comment_author_login: str,
    comment_author_association: str | None,
    policy: dict[str, Any],
) -> None:
    """Validate the v1 contract and authenticated comment-author identity."""

    _require_exact_keys(payload, ATTESTATION_KEYS, "attestation")
    if payload["schema_version"] != "1.0":
        raise AttestationError("unsupported attestation schema_version")
    _require_string(payload["task_id"], "attestation.task_id", max_length=200)
    repository = _require_string(payload["repository"], "attestation.repository", max_length=200)
    if not REPOSITORY_RE.fullmatch(repository):
        raise AttestationError("attestation.repository must be owner/name")
    if type(payload["pr_number"]) is not int or payload["pr_number"] < 1:
        raise AttestationError("attestation.pr_number must be a positive integer")
    for key in ("base_sha", "head_sha"):
        if not isinstance(payload[key], str) or not SHA_RE.fullmatch(payload[key]):
            raise AttestationError(f"attestation.{key} must be a lowercase 40-character Git SHA")
    verdict = _require_enum(payload["qa_verdict"], VERDICTS, "attestation.qa_verdict")

    tested_at = _require_string(payload["tested_at"], "attestation.tested_at", max_length=100)
    try:
        parsed_tested_at = datetime.fromisoformat(tested_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AttestationError("attestation.tested_at must be an ISO 8601 date-time") from exc
    if parsed_tested_at.tzinfo is None:
        raise AttestationError("attestation.tested_at must include a timezone")

    tests = payload["tests"]
    if not isinstance(tests, list) or not tests:
        raise AttestationError("attestation.tests must be a non-empty list")
    for index, test in enumerate(tests):
        path = f"attestation.tests[{index}]"
        if not isinstance(test, dict):
            raise AttestationError(f"{path} must be an object")
        _require_exact_keys(test, TEST_KEYS, path)
        _require_string(test["name"], f"{path}.name", max_length=200)
        _require_string(test["command"], f"{path}.command", max_length=2000)
        result = _require_enum(test["result"], VERDICTS, f"{path}.result")
        counts = test["counts"]
        if not isinstance(counts, dict):
            raise AttestationError(f"{path}.counts must be an object")
        _require_exact_keys(counts, COUNT_KEYS, f"{path}.counts")
        values = {
            key: _require_nonnegative_integer(counts[key], f"{path}.counts.{key}")
            for key in COUNT_KEYS
        }
        if values["total"] != values["passed"] + values["failed"] + values["skipped"]:
            raise AttestationError(f"{path}.counts.total must equal passed + failed + skipped")
        if result == "PASS" and values["failed"] != 0:
            raise AttestationError(f"{path} cannot PASS with failed tests")

    if verdict == "PASS" and any(test["result"] != "PASS" for test in tests):
        raise AttestationError("qa_verdict PASS requires every listed test to PASS")

    evidence = payload["evidence"]
    if not isinstance(evidence, list) or not evidence:
        raise AttestationError("attestation.evidence must be a non-empty list")
    for index, item in enumerate(evidence):
        path = f"attestation.evidence[{index}]"
        if not isinstance(item, dict):
            raise AttestationError(f"{path} must be an object")
        missing = EVIDENCE_REQUIRED_KEYS - item.keys()
        extra = item.keys() - EVIDENCE_ALLOWED_KEYS
        if missing or extra:
            raise AttestationError(f"{path} has invalid fields")
        _require_enum(item["kind"], EVIDENCE_KINDS, f"{path}.kind")
        _require_string(item["id"], f"{path}.id", max_length=500)
        if "sha256" in item and (
            not isinstance(item["sha256"], str) or not SHA256_RE.fullmatch(item["sha256"])
        ):
            raise AttestationError(f"{path}.sha256 must be a lowercase SHA-256 digest")
        if "url" in item:
            url = _require_string(item["url"], f"{path}.url", max_length=2000)
            parsed_url = urlparse(url)
            if parsed_url.scheme != "https" or not parsed_url.netloc:
                raise AttestationError(f"{path}.url must be an absolute HTTPS URL")

    for key in ("merge_allowed", "deploy_allowed"):
        if type(payload[key]) is not bool:
            raise AttestationError(f"attestation.{key} must be boolean")

    issuer = payload["issuer"]
    if not isinstance(issuer, dict):
        raise AttestationError("attestation.issuer must be an object")
    _require_exact_keys(issuer, ISSUER_KEYS, "attestation.issuer")
    issuer_login = _require_string(issuer["login"], "attestation.issuer.login", max_length=100)
    if issuer_login.casefold() != comment_author_login.casefold():
        raise AttestationError("issuer.login must equal the authenticated GitHub comment author")
    if issuer["identity_source"] != "GITHUB_COMMENT_AUTHOR":
        raise AttestationError("issuer.identity_source must be GITHUB_COMMENT_AUTHOR")
    issuer_role = _require_enum(
        issuer["repository_role"], REPOSITORY_ROLES, "attestation.issuer.repository_role"
    )
    authenticated_role = _comment_role(comment_author_association)
    if issuer_role != authenticated_role:
        raise AttestationError(
            "issuer.repository_role must match GitHub's authenticated author_association"
        )

    reviewer = payload["reviewer"]
    if not isinstance(reviewer, dict):
        raise AttestationError("attestation.reviewer must be an object")
    _require_exact_keys(reviewer, REVIEWER_KEYS, "attestation.reviewer")
    reviewer_login = reviewer["login"]
    if reviewer_login is not None:
        _require_string(reviewer_login, "attestation.reviewer.login", max_length=100)
        if not LOGIN_RE.fullmatch(reviewer_login):
            raise AttestationError("attestation.reviewer.login is not a valid GitHub login")
    reviewer_role = _require_enum(
        reviewer["repository_role"], REPOSITORY_ROLES, "attestation.reviewer.repository_role"
    )
    independence = _require_enum(
        reviewer["independence"], INDEPENDENCE_STATES, "attestation.reviewer.independence"
    )
    if reviewer_login is None and (
        reviewer_role != "UNKNOWN" or independence != "INDEPENDENT_REVIEWER_NOT_ESTABLISHED"
    ):
        raise AttestationError(
            "an unidentified reviewer must be UNKNOWN and not independently established"
        )

    owner_logins = {login.casefold() for login in policy["repository_owner_logins"]}
    same_as_issuer = (
        reviewer_login is not None
        and reviewer_login.casefold() == comment_author_login.casefold()
    )
    reviewer_is_owner = (
        reviewer_role == "OWNER"
        or (reviewer_login is not None and reviewer_login.casefold() in owner_logins)
    )
    if independence == "INDEPENDENT_REVIEWER_ESTABLISHED" and (
        same_as_issuer or reviewer_is_owner
    ):
        raise AttestationError(
            "the issuer or a configured repository owner cannot be represented as independent"
        )


def _base_result(snapshot: dict[str, Any]) -> dict[str, Any]:
    return {
        "state": "MISSING",
        "reason": "NO_TRUSTED_ATTESTATION",
        "repository": snapshot.get("repository"),
        "pr_number": snapshot.get("pr_number"),
        "current_base_sha": snapshot.get("current_base_sha"),
        "current_head_sha": snapshot.get("current_head_sha"),
        "attestation_comment_id": None,
        "attestation_comment_url": None,
        "attested_base_sha": None,
        "attested_head_sha": None,
        "task_id": None,
        "qa_verdict": None,
        "merge_allowed": None,
        "deploy_allowed": None,
        "reviewer_independence": None,
        "warnings": [],
        "diagnostics": [],
    }


def _validate_snapshot(snapshot: dict[str, Any]) -> None:
    expected = {
        "repository",
        "pr_number",
        "current_base_sha",
        "current_head_sha",
        "comments",
    }
    _require_exact_keys(snapshot, expected, "snapshot")
    if not isinstance(snapshot["repository"], str) or not REPOSITORY_RE.fullmatch(
        snapshot["repository"]
    ):
        raise AttestationError("snapshot.repository must be owner/name")
    if type(snapshot["pr_number"]) is not int or snapshot["pr_number"] < 1:
        raise AttestationError("snapshot.pr_number must be a positive integer")
    for key in ("current_base_sha", "current_head_sha"):
        if not isinstance(snapshot[key], str) or not SHA_RE.fullmatch(snapshot[key]):
            raise AttestationError(f"snapshot.{key} must be a lowercase 40-character Git SHA")
    if not isinstance(snapshot["comments"], list):
        raise AttestationError("snapshot.comments must be a list")


def evaluate_attestations(
    snapshot: dict[str, Any], policy: dict[str, Any]
) -> dict[str, Any]:
    """Deterministically evaluate all trusted comment attestations for a PR snapshot."""

    _validate_snapshot(snapshot)
    _validate_policy(policy)
    result = _base_result(snapshot)
    allowed_issuers = {login.casefold() for login in policy["allowed_issuer_logins"]}

    invalid: list[str] = []
    foreign: list[tuple[dict[str, Any], dict[str, Any]]] = []
    stale: list[tuple[dict[str, Any], dict[str, Any], str]] = []
    current: list[tuple[dict[str, Any], dict[str, Any]]] = []

    for comment in snapshot["comments"]:
        if not isinstance(comment, dict):
            invalid.append("comment entry is not an object")
            continue
        author_login = comment.get("author_login")
        if not isinstance(author_login, str) or author_login.casefold() not in allowed_issuers:
            continue
        body = comment.get("body")
        if not isinstance(body, str):
            invalid.append(f"trusted comment {comment.get('id')} has no string body")
            continue
        try:
            payload = parse_comment_block(body)
            if payload is None:
                continue
            validate_attestation(
                payload,
                comment_author_login=author_login,
                comment_author_association=comment.get("author_association"),
                policy=policy,
            )
        except AttestationError as exc:
            invalid.append(f"trusted comment {comment.get('id')}: {exc}")
            continue

        pair = (payload, comment)
        if (
            payload["repository"].casefold() != snapshot["repository"].casefold()
            or payload["pr_number"] != snapshot["pr_number"]
        ):
            foreign.append(pair)
        elif payload["head_sha"] != snapshot["current_head_sha"]:
            stale.append((payload, comment, "HEAD_SHA_MISMATCH"))
        elif (
            policy["require_current_base_sha"]
            and payload["base_sha"] != snapshot["current_base_sha"]
        ):
            stale.append((payload, comment, "BASE_SHA_MISMATCH"))
        else:
            current.append(pair)

    if invalid:
        result["state"] = "FAIL"
        result["reason"] = "MALFORMED_TRUSTED_ATTESTATION"
        result["diagnostics"] = invalid
        return result
    if foreign:
        result["state"] = "FAIL"
        result["reason"] = "ATTESTATION_CONTEXT_MISMATCH"
        result["diagnostics"] = [
            f"trusted comment {comment.get('id')} targets "
            f"{payload['repository']}#{payload['pr_number']}"
            for payload, comment in foreign
        ]
        return result
    if len(current) > 1:
        result["state"] = "FAIL"
        result["reason"] = "DUPLICATE_CURRENT_ATTESTATIONS"
        result["diagnostics"] = [
            f"trusted comment {comment.get('id')} attests current HEAD"
            for _, comment in current
        ]
        return result
    if not current:
        if stale:
            result["state"] = "STALE"
            result["reason"] = stale[-1][2]
            result["diagnostics"] = [
                f"trusted comment {comment.get('id')} attests head={payload['head_sha']} "
                f"base={payload['base_sha']}"
                for payload, comment, _ in stale
            ]
        return result

    payload, comment = current[0]
    result.update(
        {
            "attestation_comment_id": comment.get("id"),
            "attestation_comment_url": comment.get("html_url"),
            "attested_base_sha": payload["base_sha"],
            "attested_head_sha": payload["head_sha"],
            "task_id": payload["task_id"],
            "qa_verdict": payload["qa_verdict"],
            "merge_allowed": payload["merge_allowed"],
            "deploy_allowed": payload["deploy_allowed"],
            "reviewer_independence": payload["reviewer"]["independence"],
        }
    )
    if payload["base_sha"] != snapshot["current_base_sha"]:
        result["warnings"].append("TESTED_BASE_DIFFERS_FROM_CURRENT_BASE")

    if policy["require_independent_reviewer"] and (
        payload["reviewer"]["independence"] != "INDEPENDENT_REVIEWER_ESTABLISHED"
    ):
        result["state"] = "FAIL"
        result["reason"] = "INDEPENDENT_REVIEWER_REQUIRED"
    elif payload["qa_verdict"] != "PASS":
        result["state"] = "FAIL"
        result["reason"] = "QA_VERDICT_NOT_PASS"
    elif not payload["merge_allowed"]:
        result["state"] = "FAIL"
        result["reason"] = "MERGE_NOT_ALLOWED"
    else:
        result["state"] = "PASS"
        result["reason"] = "CURRENT_HEAD_ACCEPTED"
    return result


def _write_result(path: Path, result: dict[str, Any]) -> None:
    path.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    evaluate = subparsers.add_parser("evaluate", help="evaluate a GitHub PR snapshot")
    evaluate.add_argument("--input", type=Path, required=True)
    evaluate.add_argument("--policy", type=Path, required=True)
    evaluate.add_argument("--output", type=Path, required=True)
    args = parser.parse_args(argv)

    if args.command == "evaluate":
        output: Path = args.output
        try:
            snapshot = json.loads(args.input.read_text(encoding="utf-8"))
            policy = json.loads(args.policy.read_text(encoding="utf-8"))
            result = evaluate_attestations(snapshot, policy)
        except (OSError, json.JSONDecodeError, AttestationError) as exc:
            result = {
                "state": "FAIL",
                "reason": "VALIDATOR_ERROR",
                "diagnostics": [str(exc)],
            }
        _write_result(output, result)
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0 if result.get("state") == "PASS" else 1
    raise AssertionError("unreachable")


if __name__ == "__main__":
    raise SystemExit(main())
