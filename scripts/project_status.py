#!/usr/bin/env python3
"""Validate, collect, and render the canonical project status."""

from __future__ import annotations

import argparse
import ast
import hashlib
import json
import re
import subprocess
import sys
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

import httpx
import yaml
from jsonschema import Draft202012Validator, FormatChecker
from jsonschema.exceptions import SchemaError


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_STATUS = ROOT / "project_status.yaml"
DEFAULT_MARKDOWN = ROOT / "PROJECT_STATUS.md"
DEFAULT_SCHEMA = ROOT / "schemas" / "project_status.schema.json"
STALE_AFTER = timedelta(hours=24)
UNKNOWN = "UNKNOWN"
POSITIVE_PRODUCTION_EVIDENCE = {
    "production_handoff",
    "release_manifest",
    "runtime_observation",
}


class StatusLoader(yaml.SafeLoader):
    """Safe YAML loader that keeps ISO timestamps as strings for JSON Schema."""


StatusLoader.yaml_implicit_resolvers = deepcopy(yaml.SafeLoader.yaml_implicit_resolvers)
for first_character, resolvers in list(StatusLoader.yaml_implicit_resolvers.items()):
    StatusLoader.yaml_implicit_resolvers[first_character] = [
        resolver
        for resolver in resolvers
        if resolver[0] != "tag:yaml.org,2002:timestamp"
    ]


class StatusError(ValueError):
    """Raised when canonical status validation fails."""


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace(
        "+00:00", "Z"
    )


def parse_timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise StatusError(f"timestamp must include a timezone: {value}")
    return parsed.astimezone(timezone.utc)


def load_yaml(path: Path) -> dict[str, Any]:
    try:
        loaded = yaml.load(path.read_text(encoding="utf-8"), Loader=StatusLoader)
    except (OSError, yaml.YAMLError) as exc:
        raise StatusError(f"cannot load {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise StatusError(f"{path} must contain a YAML mapping")
    return loaded


def load_schema(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise StatusError(f"cannot load schema {path}: {exc}") from exc


def format_path(parts: Iterable[Any]) -> str:
    result = "$"
    for part in parts:
        result += f"[{part}]" if isinstance(part, int) else f".{part}"
    return result


def validate_schema(status: dict[str, Any], schema: dict[str, Any]) -> None:
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise StatusError(f"project status schema is invalid: {exc.message}") from exc
    schema_validator = Draft202012Validator(
        schema, format_checker=FormatChecker()
    )
    errors = sorted(
        schema_validator.iter_errors(status),
        key=lambda item: tuple(str(part) for part in item.absolute_path),
    )
    if errors:
        details = "\n".join(
            f"- {format_path(error.absolute_path)}: {error.message}"
            for error in errors
        )
        raise StatusError(f"schema validation failed:\n{details}")


def has_verified_evidence(
    evidence: list[dict[str, Any]], kinds: set[str] | None = None
) -> bool:
    return any(
        item["status"] == "VERIFIED"
        and (kinds is None or item["kind"] in kinds)
        for item in evidence
    )


def has_unknown(value: Any) -> bool:
    if isinstance(value, dict):
        return any(has_unknown(item) for item in value.values())
    if isinstance(value, list):
        return any(has_unknown(item) for item in value)
    return value in {UNKNOWN, "NOT_VERIFIED"}


def validate_unique_ids(items: list[dict[str, Any]], label: str) -> list[str]:
    identifiers = [item["id"] for item in items]
    duplicates = sorted({item for item in identifiers if identifiers.count(item) > 1})
    return [f"{label} contains duplicate id {item!r}" for item in duplicates]


def semantic_errors(status: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    main_sha = status["main_sha"]
    code = status["code"]
    qa = status["qa"]
    production = status["production"]
    visible = status["user_visible"]
    data = status["data_state"]

    if code["main_sha"] != main_sha:
        errors.append("code.main_sha must equal top-level main_sha")

    errors.extend(validate_unique_ids(code["capabilities"], "code.capabilities"))
    errors.extend(validate_unique_ids(qa["capabilities"], "qa.capabilities"))
    errors.extend(
        validate_unique_ids(production["capabilities"], "production.capabilities")
    )
    errors.extend(
        validate_unique_ids(visible["capabilities"], "user_visible.capabilities")
    )
    errors.extend(validate_unique_ids(status["services"], "services"))

    accepted_sha = qa["accepted_head_sha"]
    if qa["state"] == "ACCEPTED":
        if qa["verdict"] != "PASS":
            errors.append("qa.state ACCEPTED requires qa.verdict PASS")
        if accepted_sha != main_sha:
            errors.append("QA for a different SHA must be STALE, not ACCEPTED")
        if qa["tested_at"] == UNKNOWN or qa["qa_task_id"] == UNKNOWN:
            errors.append("accepted QA requires a task id and tested_at timestamp")
        if not has_verified_evidence(qa["evidence"], {"qa_task"}):
            errors.append("accepted QA requires verified qa_task evidence")
    if qa["state"] == "STALE" and accepted_sha in {UNKNOWN, main_sha}:
        errors.append("qa.state STALE requires a known accepted SHA different from main")
    if qa["merge_allowed"] and (
        qa["state"] != "ACCEPTED"
        or qa["verdict"] != "PASS"
        or accepted_sha != main_sha
    ):
        errors.append("merge_allowed requires accepted PASS QA for current main_sha")
    if qa["state"] in {"NOT_ACCEPTED", "STALE", "UNKNOWN"} and qa[
        "merge_allowed"
    ]:
        errors.append("non-accepted or stale QA cannot allow merge")

    production_evidence = production["evidence"]
    runtime_known = production["runtime_sha"] != UNKNOWN
    deployed = [
        item for item in production["capabilities"] if item["status"] == "DEPLOYED"
    ]
    if runtime_known and not has_verified_evidence(
        production_evidence, POSITIVE_PRODUCTION_EVIDENCE
    ):
        errors.append("production.runtime_sha requires explicit runtime/production evidence")
    if deployed and not runtime_known:
        errors.append("DEPLOYED capability requires an explicit production.runtime_sha")
    for item in deployed:
        if not has_verified_evidence(item["evidence"], POSITIVE_PRODUCTION_EVIDENCE):
            errors.append(
                f"production capability {item['id']!r} cannot be inferred from Git/QA"
            )
    if production["state"] == "VERIFIED":
        required_verified = (
            "runtime_sha",
            "operational_db_revision",
            "public_db_revision",
            "deployed_at",
            "last_production_acceptance_task",
        )
        missing = [key for key in required_verified if production[key] == UNKNOWN]
        if missing:
            errors.append(
                "verified production requires explicit " + ", ".join(missing)
            )
        if not has_verified_evidence(
            production_evidence, POSITIVE_PRODUCTION_EVIDENCE
        ):
            errors.append("verified production requires production/runtime evidence")
    if has_unknown(
        {
            key: production[key]
            for key in (
                "runtime_sha",
                "operational_db_revision",
                "public_db_revision",
                "semantic_ready_count",
                "public_ready_count",
                "deployed_at",
            )
        }
    ) and not any(item["status"] == "NOT_VERIFIED" for item in production_evidence):
        errors.append("unknown production values require NOT_VERIFIED provenance")

    external_positive = (
        visible["public_site_state"] == "LIVE"
        or visible["external_api_state"] == "LIVE"
        or visible["ssr_state"] == "VERIFIED"
        or any(item["status"] == "VISIBLE" for item in visible["capabilities"])
    )
    if external_positive:
        if visible["external_verified_at"] == UNKNOWN:
            errors.append("user-visible claims require external_verified_at")
        if not has_verified_evidence(visible["evidence"], {"runtime_observation"}):
            errors.append("user-visible claims require independent runtime observation")
    for item in visible["capabilities"]:
        if item["status"] == "VISIBLE" and not has_verified_evidence(
            item["evidence"], {"runtime_observation"}
        ):
            errors.append(
                f"user-visible capability {item['id']!r} lacks runtime observation"
            )

    production_release = production["active_release_id"]
    visible_release = visible["active_release_id"]
    if (
        production_release != UNKNOWN
        and visible_release != UNKNOWN
        and production_release != visible_release
    ):
        errors.append("production and user_visible active_release_id values disagree")

    mirrored_fields = (
        "operational_db_revision",
        "public_db_revision",
        "active_release_id",
        "active_release_record_count",
        "active_release_cohort",
        "semantic_ready_count",
        "public_ready_count",
    )
    for field in mirrored_fields:
        if data[field] != production[field]:
            errors.append(f"data_state.{field} must mirror production.{field}")

    if status["incidents"]["state"] == "CLEAR" and not has_verified_evidence(
        status["incidents"]["evidence"]
    ):
        errors.append("incident state CLEAR requires verified evidence")
    if status["incidents"]["state"] == "ACTIVE" and not status["incidents"]["active"]:
        errors.append("incident state ACTIVE requires at least one active incident")

    for service in status["services"]:
        if service["state"] in {"HEALTHY", "DEGRADED", "DOWN"} and not has_verified_evidence(
            service["evidence"],
            {"production_handoff", "runtime_observation"},
        ):
            errors.append(
                f"service {service['id']!r} state requires runtime/production evidence"
            )
    return errors


def validate_semantics(status: dict[str, Any]) -> None:
    errors = semantic_errors(status)
    if errors:
        raise StatusError(
            "semantic consistency validation failed:\n"
            + "\n".join(f"- {error}" for error in errors)
        )


def git(*args: str) -> str:
    process = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if process.returncode:
        raise StatusError(
            f"git {' '.join(args)} failed: {process.stderr.strip() or process.stdout.strip()}"
        )
    return process.stdout.strip()


def freshness_reference(mode: str) -> datetime | None:
    if mode == "off":
        return None
    if mode == "live":
        return utc_now()
    if mode == "repository":
        return parse_timestamp(git("show", "-s", "--format=%cI", "HEAD"))
    raise StatusError(f"unsupported freshness mode: {mode}")


def validate_freshness(status: dict[str, Any], reference: datetime | None) -> None:
    if reference is None:
        return
    generated = parse_timestamp(status["generated_at"])
    age = reference.astimezone(timezone.utc) - generated
    if age > STALE_AFTER:
        raise StatusError(
            "status is stale: generated_at "
            f"{status['generated_at']} is {age.total_seconds() / 3600:.2f}h old "
            f"(limit {STALE_AFTER.total_seconds() / 3600:.0f}h)"
        )


def markdown_cell(value: Any) -> str:
    if isinstance(value, bool):
        rendered = "true" if value else "false"
    elif isinstance(value, list):
        rendered = ", ".join(str(item) for item in value) if value else "—"
    else:
        rendered = str(value)
    return rendered.replace("|", "\\|").replace("\n", " ")


def table(rows: list[tuple[str, Any]]) -> list[str]:
    result = ["| Field | Value |", "|---|---|"]
    result.extend(
        f"| {markdown_cell(key)} | {markdown_cell(value)} |" for key, value in rows
    )
    return result


def evidence_lines(evidence: list[dict[str, Any]]) -> list[str]:
    return [
        f"- `{item['status']}` / `{item['kind']}` — {item['reference']} "
        f"(observed `{item['observed_at']}`)"
        for item in evidence
    ]


def capability_table(items: list[dict[str, Any]]) -> list[str]:
    if not items:
        return ["No capability assertions recorded."]
    rows = ["| Capability | Status | Evidence |", "|---|---|---|"]
    for item in items:
        evidence = "; ".join(entry["reference"] for entry in item["evidence"])
        rows.append(
            f"| `{markdown_cell(item['id'])}` | `{item['status']}` | "
            f"{markdown_cell(evidence)} |"
        )
    return rows


def render_markdown(status: dict[str, Any]) -> str:
    code = status["code"]
    pr = code["latest_relevant_merged_pr"]
    migrations = code["migration_heads"]
    artifact = code["release_capable_artifact"]
    qa = status["qa"]
    production = status["production"]
    visible = status["user_visible"]
    withheld = visible["known_withheld_or_depublished"]
    lines = [
        "# PROJECT STATUS — GENERATED — DO NOT EDIT MANUALLY",
        "",
        "> Canonical source: [`project_status.yaml`](project_status.yaml).",
        "> Regenerate with `uv run python scripts/project_status.py update`.",
        "",
        f"- Schema version: `{status['schema_version']}`",
        f"- Generated at: `{status['generated_at']}`",
        f"- Canonical main SHA: `{status['main_sha']}`",
        "",
        "Layer states are independent. A merge is not deployment, QA acceptance is",
        "not deployment, and production deployment is not user-visible verification.",
        "",
        "## CODE",
        "",
        *table(
            [
                ("State", code["state"]),
                ("Main SHA", code["main_sha"]),
                ("Latest relevant merged PR", f"#{pr['number']} — {pr['url']}"),
                ("Merge SHA", pr["merge_sha"]),
                ("Merged at", pr["merged_at"]),
                ("Operational migration head in code", migrations["operational"]),
                ("Public migration head in code", migrations["public"]),
                ("Release-capable artifact state", artifact["state"]),
                ("Release-capable artifact", artifact["artifact_id"]),
                ("Artifact path", artifact["path"]),
                ("Artifact SHA-256", artifact["sha256"]),
            ]
        ),
        "",
        "### CODE capabilities",
        "",
        *capability_table(code["capabilities"]),
        "",
        "## QA",
        "",
        *table(
            [
                ("State", qa["state"]),
                ("Accepted head SHA", qa["accepted_head_sha"]),
                ("QA task ID", qa["qa_task_id"]),
                ("Verdict", qa["verdict"]),
                ("Evidence reference", qa["evidence_reference"]),
                ("Merge allowed", qa["merge_allowed"]),
                ("Tested at", qa["tested_at"]),
            ]
        ),
        "",
        "### QA capabilities",
        "",
        *capability_table(qa["capabilities"]),
        "",
        "### QA provenance",
        "",
        *evidence_lines(qa["evidence"]),
        "",
        "## PRODUCTION",
        "",
        *table(
            [
                ("State", production["state"]),
                ("Runtime SHA", production["runtime_sha"]),
                ("Operational DB revision", production["operational_db_revision"]),
                ("Public DB revision", production["public_db_revision"]),
                ("Active release ID", production["active_release_id"]),
                ("Active release record count", production["active_release_record_count"]),
                ("Active release cohort", production["active_release_cohort"]),
                ("Semantic-ready count", production["semantic_ready_count"]),
                ("Public-ready count", production["public_ready_count"]),
                ("Service state", production["service_state"]),
                ("Last production acceptance task", production["last_production_acceptance_task"]),
                ("Deployed at", production["deployed_at"]),
            ]
        ),
        "",
        "### PRODUCTION capabilities",
        "",
        *capability_table(production["capabilities"]),
        "",
        "### PRODUCTION provenance",
        "",
        *evidence_lines(production["evidence"]),
        "",
        "## USER_VISIBLE",
        "",
        *table(
            [
                ("State", visible["state"]),
                ("Public site", visible["public_site_state"]),
                ("External API", visible["external_api_state"]),
                ("SSR", visible["ssr_state"]),
                ("Active release ID", visible["active_release_id"]),
                ("Externally verified at", visible["external_verified_at"]),
                ("Withheld/depublished knowledge", withheld["state"]),
            ]
        ),
        "",
        "### USER_VISIBLE capabilities",
        "",
        *capability_table(visible["capabilities"]),
        "",
        "### Known withheld or depublished companies",
        "",
    ]
    if withheld["companies"]:
        lines.extend(["| Identifier | State | Reason |", "|---|---|---|"])
        lines.extend(
            f"| `{item['identifier']}` | `{item['state']}` | {markdown_cell(item['reason'])} |"
            for item in withheld["companies"]
        )
    else:
        lines.append("None recorded; this is not evidence that none exist.")
    lines.extend(["", "### Known user-visible issues", ""])
    if withheld["issues"]:
        lines.extend(
            f"- `{item['id']}` — {item['description']}" for item in withheld["issues"]
        )
    else:
        lines.append("None recorded; this is not evidence that none exist.")
    lines.extend(
        [
            "",
            "### USER_VISIBLE provenance",
            "",
            *evidence_lines(visible["evidence"]),
            "",
            "## Active task and next gate",
            "",
            *table(
                [
                    ("Active task", status["active_task"]["id"]),
                    ("Active task state", status["active_task"]["state"]),
                    ("Active task title", status["active_task"]["title"]),
                    ("Next gate", status["next_gate"]["id"]),
                    ("Next gate state", status["next_gate"]["state"]),
                    ("Next gate description", status["next_gate"]["description"]),
                ]
            ),
            "",
            "## Incidents",
            "",
            f"State: `{status['incidents']['state']}`.",
            "",
        ]
    )
    if status["incidents"]["active"]:
        lines.extend(["| Incident | Severity | Status | Summary |", "|---|---|---|---|"])
        lines.extend(
            f"| `{item['id']}` | `{item['severity']}` | `{item['status']}` | "
            f"{markdown_cell(item['summary'])} |"
            for item in status["incidents"]["active"]
        )
    else:
        lines.append("No active incidents are listed. The section state controls whether that is verified.")
    lines.extend(["", "## Services", ""])
    if status["services"]:
        lines.extend(["| Service | Environment | State | Evidence |", "|---|---|---|---|"])
        lines.extend(
            f"| `{item['id']}` | `{item['environment']}` | `{item['state']}` | "
            f"{markdown_cell('; '.join(e['reference'] for e in item['evidence']))} |"
            for item in status["services"]
        )
    else:
        lines.append("No services recorded.")
    data = status["data_state"]
    lines.extend(
        [
            "",
            "## Data state",
            "",
            *table(
                [
                    ("Operational DB revision", data["operational_db_revision"]),
                    ("Public DB revision", data["public_db_revision"]),
                    ("Active release ID", data["active_release_id"]),
                    ("Active release record count", data["active_release_record_count"]),
                    ("Active release cohort", data["active_release_cohort"]),
                    ("Semantic-ready count", data["semantic_ready_count"]),
                    ("Public-ready count", data["public_ready_count"]),
                ]
            ),
            "",
            "### Data provenance",
            "",
            *evidence_lines(data["evidence"]),
        ]
    )
    return "\n".join(lines).rstrip() + "\n"


def validate_all(
    status: dict[str, Any], schema_path: Path, freshness_mode: str
) -> None:
    validate_schema(status, load_schema(schema_path))
    validate_semantics(status)
    validate_freshness(status, freshness_reference(freshness_mode))


def check_parity(markdown_path: Path, expected: str) -> None:
    try:
        actual = markdown_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise StatusError(f"cannot read generated Markdown {markdown_path}: {exc}") from exc
    if actual != expected:
        raise StatusError(
            f"generated Markdown drift detected: {markdown_path} does not match project_status.yaml"
        )


def write_markdown(path: Path, rendered: str) -> None:
    path.write_text(rendered, encoding="utf-8")


def run_check(args: argparse.Namespace) -> None:
    status = load_yaml(args.status)
    validate_all(status, args.schema, args.freshness)
    check_parity(args.markdown, render_markdown(status))


def run_update(args: argparse.Namespace) -> None:
    status = load_yaml(args.status)
    validate_all(status, args.schema, args.freshness)
    write_markdown(args.markdown, render_markdown(status))
    check_parity(args.markdown, render_markdown(status))


def migration_heads(directory: Path, *, ref: str | None = None) -> str:
    revisions: dict[str, Any] = {}
    if ref is None:
        sources = (
            (str(path), path.read_text(encoding="utf-8"))
            for path in sorted(directory.glob("*.py"))
        )
    else:
        directory_name = directory.relative_to(ROOT).as_posix()
        paths = git("ls-tree", "-r", "--name-only", ref, "--", directory_name).splitlines()
        sources = (
            (path, git("show", f"{ref}:{path}"))
            for path in paths
            if path.startswith(f"{directory_name}/") and path.endswith(".py")
        )
    for path, source in sources:
        tree = ast.parse(source, filename=path)
        values: dict[str, Any] = {}
        for node in tree.body:
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            value = node.value
            for target in targets:
                if isinstance(target, ast.Name) and target.id in {
                    "revision",
                    "down_revision",
                }:
                    try:
                        values[target.id] = ast.literal_eval(value)
                    except (ValueError, TypeError):
                        pass
        if values.get("revision"):
            revisions[str(values["revision"])] = values.get("down_revision")
    referenced: set[str] = set()
    for down_revision in revisions.values():
        if isinstance(down_revision, str):
            referenced.add(down_revision)
        elif isinstance(down_revision, (list, tuple)):
            referenced.update(str(item) for item in down_revision if item)
    heads = sorted(set(revisions) - referenced)
    if not heads:
        raise StatusError(f"no migration head found in {directory} at {ref or 'working tree'}")
    return ",".join(heads)


def github_remote_url() -> str:
    remote = git("remote", "get-url", "origin")
    match = re.search(r"github\.com[:/](?P<repo>[^/]+/[^/]+?)(?:\.git)?$", remote)
    return f"https://github.com/{match.group('repo')}" if match else remote.removesuffix(".git")


def repository_evidence(reference: str, observed_at: str) -> list[dict[str, str]]:
    return [
        {
            "status": "VERIFIED",
            "kind": "git",
            "reference": reference,
            "observed_at": observed_at,
        }
    ]


def collect_repository(status: dict[str, Any], ref: str) -> dict[str, Any]:
    collected = deepcopy(status)
    observed_at = timestamp(utc_now())
    main_sha = git("rev-parse", ref)
    merge_line = git(
        "log",
        "--first-parent",
        "--merges",
        "-1",
        "--format=%H%x1f%cI%x1f%s",
        ref,
    )
    merge_sha, merged_at, subject = merge_line.split("\x1f", 2)
    match = re.search(r"Merge (?:pull request|PR) #(\d+)", subject)
    if not match:
        raise StatusError(f"latest first-parent merge does not name a pull request: {subject}")
    pr_number = int(match.group(1))
    repo_url = github_remote_url()

    collected["generated_at"] = observed_at
    collected["main_sha"] = main_sha
    collected["code"]["state"] = "VERIFIED"
    collected["code"]["main_sha"] = main_sha
    collected["code"]["latest_relevant_merged_pr"] = {
        "number": pr_number,
        "url": f"{repo_url}/pull/{pr_number}",
        "merge_sha": merge_sha,
        "merged_at": timestamp(parse_timestamp(merged_at)),
        "evidence": repository_evidence(
            f"git log --first-parent --merges -1 {ref}", observed_at
        ),
    }
    collected["code"]["migration_heads"] = {
        "operational": migration_heads(ROOT / "migrations" / "versions", ref=ref),
        "public": migration_heads(ROOT / "public_migrations" / "versions", ref=ref),
        "evidence": repository_evidence(
            f"Static Alembic revision graph in {ref}:migrations/versions and {ref}:public_migrations/versions",
            observed_at,
        ),
    }
    artifact = collected["code"]["release_capable_artifact"]
    artifact_path = ROOT / artifact["path"]
    if artifact_path.is_file():
        artifact["state"] = "PRESENT"
        artifact["sha256"] = hashlib.sha256(artifact_path.read_bytes()).hexdigest()
        artifact["evidence"] = [
            {
                "status": "VERIFIED",
                "kind": "release_manifest",
                "reference": f"sha256({artifact['path']})",
                "observed_at": observed_at,
            }
        ]
    else:
        artifact["state"] = "ABSENT"
        artifact["sha256"] = UNKNOWN
        artifact["evidence"] = [
            {
                "status": "NOT_VERIFIED",
                "kind": "repository_file",
                "reference": f"Missing repository artifact {artifact['path']}",
                "observed_at": observed_at,
            }
        ]
    return collected


def observe_public_runtime(base_url: str) -> dict[str, Any]:
    base = base_url.rstrip("/")
    try:
        with httpx.Client(timeout=15, follow_redirects=True) as client:
            landing = client.get(f"{base}/")
            health = client.get(f"{base}/api/health")
            ready = client.get(f"{base}/api/ready")
    except httpx.HTTPError as exc:
        raise StatusError(f"public runtime observation failed: {exc}") from exc
    if landing.status_code != 200:
        raise StatusError(f"public landing returned HTTP {landing.status_code}")
    if health.status_code != 200:
        raise StatusError(f"public health returned HTTP {health.status_code}")
    if ready.status_code != 200:
        raise StatusError(f"public readiness returned HTTP {ready.status_code}")
    try:
        health_payload = health.json()
        ready_payload = ready.json()
    except ValueError as exc:
        raise StatusError("public health/readiness response is not JSON") from exc
    if health_payload.get("status") != "ok":
        raise StatusError("public health response is not ok")
    if ready_payload.get("status") != "ready":
        raise StatusError("public readiness response is not ready")
    release_id = ready_payload.get("release_id")
    record_count = ready_payload.get("record_count")
    if not isinstance(release_id, str) or not release_id:
        raise StatusError("public readiness response has no release_id")
    if not isinstance(record_count, int) or isinstance(record_count, bool) or record_count < 0:
        raise StatusError("public readiness response has invalid record_count")
    return {
        "base_url": base,
        "observed_at": timestamp(utc_now()),
        "release_id": release_id,
        "record_count": record_count,
    }


def apply_public_runtime(
    status: dict[str, Any], observation: dict[str, Any]
) -> dict[str, Any]:
    collected = deepcopy(status)
    base = observation["base_url"]
    observed_at = observation["observed_at"]
    release_id = observation["release_id"]
    record_count = observation["record_count"]
    collected["generated_at"] = observed_at
    verified_evidence = [
        {
            "status": "VERIFIED",
            "kind": "runtime_observation",
            "reference": f"{base}/api/health returned status=ok",
            "observed_at": observed_at,
        },
        {
            "status": "VERIFIED",
            "kind": "runtime_observation",
            "reference": (
                f"{base}/api/ready returned ready with release_id and "
                f"record_count={record_count}"
            ),
            "observed_at": observed_at,
        },
    ]
    unknown_provenance = [
        item
        for item in collected["production"]["evidence"]
        if item["status"] == "NOT_VERIFIED"
    ]
    production = collected["production"]
    production["state"] = "PARTIALLY_VERIFIED"
    production["active_release_id"] = release_id
    production["active_release_record_count"] = record_count
    production["service_state"] = "PARTIALLY_VERIFIED"
    production["evidence"] = verified_evidence + unknown_provenance

    visible = collected["user_visible"]
    visible["state"] = "PARTIALLY_VERIFIED"
    visible["public_site_state"] = "LIVE"
    visible["external_api_state"] = "LIVE"
    visible["active_release_id"] = release_id
    visible["external_verified_at"] = observed_at
    visible["evidence"] = [
        {
            "status": "VERIFIED",
            "kind": "runtime_observation",
            "reference": f"{base}/, /api/health, and /api/ready returned valid HTTP 200 responses",
            "observed_at": observed_at,
        }
    ]

    data = collected["data_state"]
    data["active_release_id"] = release_id
    data["active_release_record_count"] = record_count
    data["evidence"] = [verified_evidence[1], *unknown_provenance]

    observed_services = {
        "public-web": f"{base}/ returned HTTP 200",
        "public-api": f"{base}/api/health and /api/ready returned valid HTTP 200 responses",
    }
    by_id = {service["id"]: service for service in collected["services"]}
    for service_id, reference in observed_services.items():
        service = by_id.get(service_id)
        if service is None:
            service = {
                "id": service_id,
                "environment": "production",
                "state": "HEALTHY",
                "evidence": [],
            }
            collected["services"].append(service)
        service["state"] = "HEALTHY"
        service["evidence"] = [
            {
                "status": "VERIFIED",
                "kind": "runtime_observation",
                "reference": reference,
                "observed_at": observed_at,
            }
        ]
    return collected


def write_yaml(path: Path, status: dict[str, Any]) -> None:
    rendered = yaml.safe_dump(
        status,
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
        width=100,
    )
    path.write_text(rendered, encoding="utf-8")


def run_collect(args: argparse.Namespace) -> None:
    status = collect_repository(load_yaml(args.status), args.git_ref)
    if args.runtime_base_url:
        status = apply_public_runtime(
            status, observe_public_runtime(args.runtime_base_url)
        )
    validate_all(status, args.schema, "live")
    write_yaml(args.status, status)
    rendered = render_markdown(status)
    write_markdown(args.markdown, rendered)
    check_parity(args.markdown, rendered)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--status", type=Path, default=DEFAULT_STATUS)
    parser.add_argument("--markdown", type=Path, default=DEFAULT_MARKDOWN)
    parser.add_argument("--schema", type=Path, default=DEFAULT_SCHEMA)
    subparsers = parser.add_subparsers(dest="command", required=True)

    for command in ("check", "update"):
        subparser = subparsers.add_parser(command)
        subparser.add_argument(
            "--freshness",
            choices=("live", "repository", "off"),
            default="live",
        )
        subparser.set_defaults(handler=run_check if command == "check" else run_update)

    collect_parser = subparsers.add_parser("collect")
    collect_parser.add_argument("--git-ref", default="origin/main")
    collect_parser.add_argument(
        "--runtime-base-url",
        help="optionally refresh public runtime observations from health/readiness endpoints",
    )
    collect_parser.set_defaults(handler=run_collect)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.handler(args)
    except StatusError as exc:
        print(f"PROJECT STATUS: FAIL\n{exc}", file=sys.stderr)
        return 1
    print("PROJECT STATUS: PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
