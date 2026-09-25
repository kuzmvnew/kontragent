"""Constrained engineering-repair package and coding-agent boundary."""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Iterable

from app.incidents.safe import safe_value


DEFAULT_INCIDENT_ROOT = Path("/home/mikhail/nextcompany-operational/incidents")
DEFAULT_REPOSITORY = Path("/home/mikhail/nextcompany-runtime/current")
SOURCE_PATHS = {
    "fns_tax_regime": ("app/ingestion/fns_tax_regime.py", "tests/test_fns_tax_regime.py"),
    "cbr_warning_list": ("app/ingestion/cbr_warning_worker.py", "tests/test_cbr_warning_worker.py"),
    "S02": ("app/ingestion/fns_tax_debt_pipeline.py", "tests/test_worker_foundation.py"),
}
BRANCH_PART = re.compile(r"[^a-z0-9-]+")
HIGH_RISK_PREFIXES = ("migrations/", "public_app/", "deploy/nginx/", "app/security/", "app/auth/")
HIGH_RISK_PARTS = ("secret", "credential", "allowlist", "validation")


@dataclass(frozen=True)
class AgentRuntime:
    available: bool
    agent_type: str | None
    status: str


def detect_agent_runtime() -> AgentRuntime:
    # Presence alone is not authorization.  An owner-managed deployment must
    # explicitly enable the adapter after installing and authenticating it.
    enabled = os.getenv("INCIDENT_CODING_AGENT_ENABLED") == "1"
    executable = shutil.which("codex")
    if enabled and executable:
        return AgentRuntime(True, "codex-cli", "AVAILABLE")
    return AgentRuntime(False, None, "AGENT_UNAVAILABLE")


def branch_name(incident_code: str, source_id: str) -> str:
    incident = BRANCH_PART.sub("-", incident_code.lower()).strip("-")[:40]
    source = BRANCH_PART.sub("-", source_id.lower().replace("_", "-")).strip("-")[:50]
    if not incident or not source:
        raise ValueError("incident and source identifiers are required")
    return f"auto/incident-{incident.removeprefix('inc-')}-{source}"


def assess_diff_risk(paths: Iterable[str], *, intended_source_paths: Iterable[str] = ()) -> str:
    allowed = set(intended_source_paths)
    for raw in paths:
        path = raw.replace("\\", "/").lstrip("./")
        lowered = path.lower()
        if path.startswith(HIGH_RISK_PREFIXES) or any(part in lowered for part in HIGH_RISK_PARTS):
            return "REVIEW_REQUIRED"
        if allowed and path not in allowed and not path.startswith("tests/"):
            return "AWAITING_ENGINEERING_REVIEW"
    return "AUTO_MERGE_ELIGIBLE"


def _safe_target(root: Path, incident_code: str) -> Path:
    if not re.fullmatch(r"INC-[A-Z0-9-]{6,36}", incident_code):
        raise ValueError("invalid incident code")
    root = root.resolve()
    target = (root / incident_code).resolve()
    if target.parent != root:
        raise ValueError("incident path escaped the configured root")
    return target


def _git_sha(repository: Path) -> str | None:
    try:
        result = subprocess.run(
            ["git", "-C", str(repository), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    value = result.stdout.strip()
    return value if re.fullmatch(r"[0-9a-f]{40}", value) else None


def create_repair_package(
    *,
    incident,
    root: Path = DEFAULT_INCIDENT_ROOT,
    repository: Path = DEFAULT_REPOSITORY,
    artifact_identity: str | None = None,
    artifact_checksum: str | None = None,
    validation_summary: object | None = None,
) -> Path:
    target = _safe_target(root, incident.incident_code)
    if not re.fullmatch(r"[A-Za-z0-9_.-]{1,120}", incident.source_id):
        raise ValueError("invalid source identifier")
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    source_paths = SOURCE_PATHS.get(incident.source_id, ())
    payload = safe_value({
        "incident_code": incident.incident_code,
        "source_id": incident.source_id,
        "dataset_code": incident.dataset_code,
        "category": incident.category,
        "owner_domain": incident.owner_domain,
        "severity": incident.severity,
        "error_code": incident.error_code,
        "safe_error_message": incident.safe_error_message,
        "run_id": str(incident.run_id) if incident.run_id else None,
        "job_id": str(incident.job_id) if incident.job_id else None,
        "failure_fingerprint": incident.failure_fingerprint,
        "main_sha": _git_sha(repository),
        "parser_handler_paths": list(source_paths),
        "source_contract_identifiers": [incident.dataset_code, incident.source_id],
        "official_artifact": {
            "identity": artifact_identity or "not available",
            "checksum": artifact_checksum or "not available",
            "payload": "omitted",
        },
        "validation_summary": validation_summary or incident.safe_error_message,
        "safe_sample": "OMITTED unless an explicitly reviewed non-private fixture exists",
    })
    files = {
        "incident.json": json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        "source_metadata.json": json.dumps(safe_value({"source_id": incident.source_id, "dataset_code": incident.dataset_code, "paths": list(source_paths)}), ensure_ascii=False, indent=2, sort_keys=True),
        "validation_summary.json": json.dumps(safe_value({"error_code": incident.error_code, "message": incident.safe_error_message, "category": incident.category}), ensure_ascii=False, indent=2, sort_keys=True),
        "reproduction.txt": f"PYTHONPATH=. .venv/bin/python -m scripts.run_incident_controller --once --source {incident.source_id}\n",
        "required_tests.txt": "focused source tests\nincident autopilot tests\nschema completeness\nfull pytest regression\nrequired GitHub CI/W1 checks\n",
    }
    for name, content in files.items():
        path = target / name
        path.write_text(content, encoding="utf-8")
        path.chmod(0o600)
    return target


class EngineeringAdapter:
    """Interface implemented fail-closed until an authorized runtime exists."""

    def __init__(self, runtime: AgentRuntime | None = None) -> None:
        self.runtime = runtime or detect_agent_runtime()

    def start(self, *, incident, package: Path) -> None:
        if not self.runtime.available:
            raise RuntimeError("AGENT_UNAVAILABLE")
        # A future authorized adapter may implement the fixed worktree/PR
        # lifecycle here.  Never accept command strings from incident data.
        raise RuntimeError("authorized engineering adapter is not configured")
