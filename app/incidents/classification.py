"""Deterministic and conservative incident classification."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re

from app.incidents.safe import safe_text


@dataclass(frozen=True)
class Classification:
    category: str
    owner_domain: str
    severity: str
    remediation_level: int


def classify_failure(
    *,
    source_id: str,
    error_code: str | None,
    message: str | None,
    operational_status: str | None = None,
) -> Classification:
    code = (error_code or "").lower()
    text = safe_text(message or "").lower()
    combined = f"{code} {text}"

    if operational_status == "stale" or "stale" in code:
        return Classification("SOURCE_STALE", "SOURCE_OWNED", "MEDIUM", 2)
    if "access" in combined or "credential" in combined or "authorization" in combined:
        return Classification("SOURCE_ACCESS_REQUIRED", "ACCESS_REQUIRED", "HIGH", 0)
    if "rate" in combined or "429" in combined or "retry-after" in combined:
        return Classification("RATE_LIMIT", "SOURCE_OWNED", "MEDIUM", 1)
    if "dns" in combined or "name resolution" in combined:
        return Classification("DNS_FAILURE", "OUR_INFRASTRUCTURE", "MEDIUM", 1)
    if "timeout" in combined or code == "timeout":
        return Classification("TIMEOUT", "OUR_INFRASTRUCTURE", "MEDIUM", 1)
    if re.search(r"\b5\d\d\b", combined) or "http_5" in combined:
        return Classification("HTTP_5XX", "SOURCE_OWNED", "MEDIUM", 1)
    if "network" in combined or "connection" in combined:
        return Classification("TEMPORARY_NETWORK", "OUR_INFRASTRUCTURE", "MEDIUM", 1)
    if "checksum" in combined:
        return Classification("CHECKSUM_MISMATCH", "OUR_INFRASTRUCTURE", "HIGH", 2)
    if "disk" in combined or "no space" in combined or "raw storage" in combined:
        return Classification("DISK_PRESSURE" if "space" in combined else "RAW_STORAGE_ERROR", "OUR_INFRASTRUCTURE", "HIGH", 0)
    if "database" in combined and ("lock" in combined or "deadlock" in combined):
        return Classification("DATABASE_LOCK", "OUR_INFRASTRUCTURE", "HIGH", 1)
    if "database" in combined or "operationalerror" in combined:
        return Classification("DATABASE_UNAVAILABLE", "OUR_INFRASTRUCTURE", "CRITICAL", 2)
    if "lease" in combined:
        return Classification("LEASE_STUCK", "OUR_INFRASTRUCTURE", "HIGH", 2)
    if "xsd" in combined or "schema_mismatch" in code or "schema violation" in combined:
        # The current SNRIP failure is validation of the official bytes.  Never
        # weaken the parser or XSD gate for this classification.
        if source_id == "fns_tax_regime" or "official" in combined or "artifact" in combined:
            return Classification("SOURCE_SCHEMA_VIOLATION", "SOURCE_OWNED", "HIGH", 2)
        return Classification("PARSER_ERROR", "OUR_CODE", "HIGH", 3)
    if "invalid artifact" in combined or code == "invalid_data":
        return Classification("SOURCE_INVALID_ARTIFACT", "SOURCE_OWNED", "HIGH", 2)
    if "normaliz" in combined:
        return Classification("NORMALIZATION_ERROR", "OUR_CODE", "HIGH", 3)
    if "match" in combined:
        return Classification("MATCHING_ERROR", "OUR_CODE", "HIGH", 3)
    if "publish" in combined:
        return Classification("PUBLICATION_ERROR", "OUR_CODE", "CRITICAL", 3)
    if "parser" in combined or "traceback" in combined or code == "handler_failure":
        return Classification("PARSER_ERROR", "OUR_CODE", "HIGH", 3)
    if "config" in combined or "handler_missing" in code:
        return Classification("CONFIGURATION_ERROR", "OUR_CODE", "HIGH", 3)
    return Classification("UNKNOWN", "UNKNOWN", "MEDIUM", 0)


def fingerprint(*, source_id: str, category: str, error_code: str | None, message: str | None) -> str:
    normalized = re.sub(r"\b[0-9a-f]{8,}\b|\b\d{4,}\b", "#", safe_text(message or "").lower())
    payload = "\0".join((source_id, category, (error_code or "").lower(), normalized))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

