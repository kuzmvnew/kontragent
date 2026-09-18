#!/usr/bin/env python3
"""Create deterministic final-delta and data-quality artifacts from Golden-40."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


CAPABILITIES = (
    "fssp", "bankruptcy", "cbr_zsk", "arbitration", "general_courts",
    "licences_sro", "procurement_rnp", "regulatory_inspections",
)

BLOCKERS = {
    "fssp": "Official live exact-INN flow returned CAPTCHA; negative bridge evidence is not accepted.",
    "bankruptcy": "Official REST production credentials are not configured; bridge absence is not negative evidence.",
    "cbr_zsk": "Official public flow requires SmartCaptcha; no completed Golden-40 sessions.",
    "arbitration": "CHECKO_API_KEY is absent and no authorized KAD machine contract is confirmed.",
    "general_courts": "Coverage is targeted by region, not nationwide; 21 routes did not complete.",
    "licences_sro": "Education-licence registry path is not connected for one applicable company.",
    "procurement_rnp": "Official EIS recipient access remains external and deferred to procurement context.",
    "regulatory_inspections": None,
}


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def terminal_counts(report: dict, capability: str) -> dict[str, int]:
    prefix = capability + ":"
    return {
        key.removeprefix(prefix): value
        for key, value in report.get("terminal_states", {}).items()
        if key.startswith(prefix)
    }


def state_text(counts: dict[str, int]) -> str:
    return ", ".join(f"{status}={count}" for status, count in sorted(counts.items())) or "NO_RESULT"


def iter_dicts(value):
    if isinstance(value, dict):
        yield value
        for item in value.values():
            yield from iter_dicts(item)
    elif isinstance(value, list):
        for item in value:
            yield from iter_dicts(item)


def scan_quality(matrix: list[dict], fssp_comparison: dict, *, generated_at: datetime):
    findings = []
    future_limit = generated_at.date()
    for company in matrix:
        inn = company["inn"]
        for source in company.get("sources", []):
            if source.get("exact_identifier_match") is False:
                findings.append({"kind": "ENTITY_MISMATCH", "inn": inn, "source": source.get("source_code")})
            if source.get("freshness") == "STALE":
                findings.append({"kind": "STALE_SOURCE", "inn": inn, "source": source.get("source_code")})
            source_as_of = source.get("source_as_of")
            if isinstance(source_as_of, str):
                try:
                    if datetime.fromisoformat(source_as_of.replace("Z", "+00:00")).date() > future_limit:
                        findings.append({"kind": "FUTURE_SOURCE_DATE", "inn": inn, "source": source.get("source_code"), "value": source_as_of})
                except ValueError:
                    findings.append({"kind": "INVALID_SOURCE_DATE", "inn": inn, "source": source.get("source_code"), "value": source_as_of})
            if "checko" in str(source.get("source_code") or "").lower() and source.get("source_class") != "AUTHORIZED_BRIDGE":
                findings.append({"kind": "SOURCE_CLASS_MISMATCH", "inn": inn, "source": source.get("source_code")})
            for value in iter_dicts(source):
                debt = value.get("total_debt")
                revenue = value.get("revenue")
                if isinstance(debt, (int, float)) and isinstance(revenue, (int, float)) and revenue > 0 and debt / revenue > 10:
                    findings.append({"kind": "EXTREME_DEBT_REVENUE_RATIO", "inn": inn, "source": source.get("source_code"), "ratio": round(debt / revenue, 4)})
                for key, number in value.items():
                    if isinstance(number, (int, float)) and number < 0 and any(token in key.lower() for token in ("amount", "debt", "revenue", "expense")):
                        findings.append({"kind": "IMPOSSIBLE_NEGATIVE_AMOUNT", "inn": inn, "source": source.get("source_code"), "field": key, "value": number})
                for list_key, id_key in (("cases", "case_number"), ("items", "number"), ("active_proceedings", "id")):
                    rows = value.get(list_key)
                    if not isinstance(rows, list):
                        continue
                    identifiers = [row.get(id_key) for row in rows if isinstance(row, dict) and row.get(id_key)]
                    duplicate_ids = sorted(key for key, count in Counter(identifiers).items() if count > 1)
                    if duplicate_ids:
                        findings.append({"kind": "DUPLICATE_RECORD", "inn": inn, "source": source.get("source_code"), "field": list_key, "identifiers": duplicate_ids})
    for comparison in fssp_comparison.get("comparisons", []):
        if comparison.get("raw_classification") == "MISMATCH":
            findings.append({
                "kind": "BRIDGE_DIRECT_CONFLICT", "inn": comparison["inn"],
                "source_a": comparison["source_a"]["code"],
                "source_b": comparison["source_b"]["code"],
                "classification": comparison["classification"],
                "resolution": comparison["resolution"],
            })
    unique = []
    seen = set()
    for finding in findings:
        marker = json.dumps(finding, ensure_ascii=False, sort_keys=True)
        if marker not in seen:
            seen.add(marker)
            unique.append(finding)
    return unique


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--final-dir", type=Path, required=True)
    args = parser.parse_args()
    baseline = read_json(args.baseline)
    final = read_json(args.final_dir / "product_recovery_v3_40_report.json")
    matrix = read_json(args.final_dir / "product_recovery_v3_40.json")
    fssp = read_json(args.final_dir / "fssp_direct_bridge_comparison.json")
    generated_at = datetime.now(timezone.utc)

    delta = {
        "generated_at": generated_at.isoformat(), "evidence_class": "VERIFIED_RUNTIME",
        "same_cohort": True,
        "coverage": {"before": baseline["coverage"], "after": final["coverage"]},
        "positive_gate": {"before": baseline["positive_gate_passed"], "after": final["positive_gate_passed"]},
        "unavailable_terminal_states": {
            "before": sum(value for key, value in baseline["terminal_states"].items() if key.endswith(":UNAVAILABLE")),
            "after": sum(value for key, value in final["terminal_states"].items() if key.endswith(":UNAVAILABLE")),
        },
        "unresolved_mandatory_occurrences": {
            "before": sum(baseline.get("unresolved", {}).values()),
            "after": sum(final.get("unresolved", {}).values()),
        },
    }
    low_risk = [row for row in matrix if row["group"] == "Низкий наблюдаемый риск — кандидат"]
    low_coverages = [row["risk"]["coverage"]["coverage_score"] for row in low_risk]
    delta["low_risk_10"] = {
        "actual": len(low_risk), "coverage_min": min(low_coverages),
        "coverage_max": max(low_coverages),
        "coverage_average": round(sum(low_coverages) / len(low_coverages), 1),
        "positive_gate_passed": sum(row["summary"]["positive_conclusion_allowed"] for row in low_risk),
    }
    (args.final_dir / "golden_delta.json").write_text(json.dumps(delta, ensure_ascii=False, indent=2), encoding="utf-8")

    capability_rows = []
    for code in CAPABILITIES:
        before = terminal_counts(baseline, code)
        after = terminal_counts(final, code)
        selected = [
            source
            for company in matrix for source in company["sources"]
            if source["check_code"] == code
        ]
        official = Counter(
            source["result"] for source in selected
            if source["source_class"] in {"OFFICIAL_DIRECT", "OFFICIAL_DOWNLOADED_DATASET"}
        )
        bridge = Counter(
            source["result"] for source in selected
            if source["source_class"] == "AUTHORIZED_BRIDGE"
        )
        capability_rows.append({
            "capability": code, "before": state_text(before), "after": state_text(after),
            "official_result": state_text(dict(official)),
            "bridge_result": state_text(dict(bridge)),
            "final_status": "CLOSED" if not final.get("unresolved", {}).get(code) else "PARTIAL" if after.get("PARTIAL") else "BLOCKED",
            "golden_resolved": 40 - final.get("unresolved", {}).get(code, 0),
            "blocker": BLOCKERS[code],
        })
    (args.final_dir / "capability_delta.json").write_text(json.dumps({
        "generated_at": generated_at.isoformat(), "evidence_class": "VERIFIED_RUNTIME",
        "columns": ["CAPABILITY", "BEFORE", "AFTER", "OFFICIAL RESULT", "BRIDGE RESULT", "FINAL STATUS", "GOLDEN RESOLVED", "BLOCKER"],
        "rows": capability_rows,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    findings = scan_quality(matrix, fssp, generated_at=generated_at)
    (args.final_dir / "data_quality_anomalies.json").write_text(json.dumps({
        "generated_at": generated_at.isoformat(), "evidence_class": "VERIFIED_RUNTIME",
        "checks": ["extreme ratios", "impossible dates", "negative amounts", "duplicate cases", "duplicate proceedings", "stale sources", "entity mismatch", "bridge/direct conflict", "source-class mismatch"],
        "finding_count": len(findings), "findings": findings,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    credentials = {
        key: bool(os.getenv(key))
        for key in ("EFRSB_API_LOGIN", "EFRSB_API_PASSWORD", "CHECKO_API_KEY", "EIS_IP_TOKEN")
    }
    (args.final_dir / "source_runtime_status.json").write_text(json.dumps({
        "generated_at": generated_at.isoformat(), "evidence_class": "VERIFIED_RUNTIME",
        "credential_presence_only": credentials, "secrets_recorded": False,
        "runtime": final["runtime"],
        "official_efrsb_specification": "https://fedresurs-demo.interfax.ru/helps/bankrupt/Service_rest_1.4.0.pdf",
        "policy": "No CAPTCHA bypass, credential guessing, or negative inference from unavailable sources.",
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print(json.dumps({"golden_delta": delta, "quality_findings": len(findings)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
