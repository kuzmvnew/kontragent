"""Conservative C6 family and per-company frozen-release evidence rules.

Both the legacy check/Card and Company View use these pure decisions.  A
projected snapshot proves a positive; a negative needs a terminal company
coverage row from the currently accepted, two-member release.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Mapping

from app.contracts.company_view_v1 import DataState
from app.contracts.data_readiness import is_dataset_stale


FAMILY_CODE = "fns_tax_regime"
LEGAL_CODE = "fns_snr"
IP_CODE = "fns_snrip"
REQUIRED_CODES = (FAMILY_CODE, LEGAL_CODE, IP_CODE)


def _field(row: Any, key: str, default: Any = None) -> Any:
    return row.get(key, default) if isinstance(row, Mapping) else getattr(row, key, default)


def _date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if value:
        try:
            return date.fromisoformat(str(value))
        except ValueError:
            pass
    return None


def _datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if value:
        try:
            parsed = datetime.fromisoformat(str(value))
            return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            pass
    return None


@dataclass(frozen=True)
class FamilyReadiness:
    state: DataState
    reason: str | None
    source_data_date: date | None
    release_identity: str | None


def evaluate_family_readiness(
    datasets: Mapping[str, Any], *, now: datetime
) -> FamilyReadiness:
    """Require the family and both pinned members to be current together."""

    now = _datetime(now) or datetime.now(timezone.utc)
    family = datasets.get(FAMILY_CODE)
    family_date = _date(_field(family, "last_data_date")) if family else None
    raw_coverage = _field(family, "coverage") if family else None
    family_coverage = raw_coverage if isinstance(raw_coverage, Mapping) else {}
    release_identity = family_coverage.get("release_identity")
    failures: list[tuple[int, DataState, str]] = []
    for code in REQUIRED_CODES:
        dataset = datasets.get(code)
        if dataset is None:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_not_registered"))
            continue
        status = str(_field(dataset, "operational_status") or "").lower()
        error = str(_field(dataset, "last_error") or "").lower()
        if status == "error" and any(
            token in error for token in ("schema", "xsd", "xml", "pars")
        ):
            failures.append((4, DataState.PARSING_ERROR, f"{code}:parsing_error"))
        elif status in {"error", "unavailable", "source_blocked", "access_pending"}:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_{status}"))
        elif not _field(dataset, "last_success_at") or not _date(_field(dataset, "last_data_date")):
            failures.append((1, DataState.NOT_CHECKED, f"{code}:not_checked"))
        elif not _field(dataset, "enabled"):
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:dataset_disabled"))
        elif status == "stale" or (
            _date(_field(dataset, "official_actual_until")) is not None
            and now.date() > _date(_field(dataset, "official_actual_until"))
        ):
            failures.append((2, DataState.STALE_DATA, f"{code}:dataset_stale"))
        elif status != "current" or _date(_field(dataset, "official_actual_until")) is None:
            failures.append((3, DataState.SOURCE_UNAVAILABLE, f"{code}:currentness_unavailable"))
        elif is_dataset_stale(
            now=now,
            freshness_policy=str(_field(dataset, "freshness_policy") or "irregular"),
            source_as_of=_datetime(_field(dataset, "source_as_of")),
            last_success_at=_datetime(_field(dataset, "last_success_at")),
            threshold_seconds=_field(dataset, "freshness_threshold_seconds"),
        ):
            failures.append((2, DataState.STALE_DATA, f"{code}:dataset_stale"))

    if failures:
        _priority, state, reason = max(failures, key=lambda failure: failure[0])
        return FamilyReadiness(state, reason, family_date, release_identity)

    dates = {_date(_field(datasets[code], "last_data_date")) for code in REQUIRED_CODES}
    members = family_coverage.get("members")
    expected = {LEGAL_CODE: "legal", IP_CODE: "ip"}
    compatible = (
        len(dates) == 1
        and bool(release_identity)
        and isinstance(members, Mapping)
        and all(
            isinstance(members.get(name), Mapping)
            and isinstance(_field(datasets[code], "coverage"), Mapping)
            and members[name].get("release_identity")
            and members[name].get("release_identity")
            == _field(datasets[code], "coverage").get("release_identity")
            for code, name in expected.items()
        )
    )
    if not compatible:
        return FamilyReadiness(
            DataState.NOT_CHECKED, "family_release_unverified", family_date, release_identity
        )
    return FamilyReadiness(DataState.FOUND, None, family_date, release_identity)


@dataclass(frozen=True)
class SameReleaseIdentityChain:
    """Audit-safe verdict against a freshly discovered, authoritative bundle."""

    valid: bool
    reasons: tuple[str, ...]
    expected_family_identity: str
    persisted_publication_identity: str | None
    persisted_publication_members: dict[str, str | None]
    persisted_family_identity: str | None
    expected_members: dict[str, str]
    persisted_family_members: dict[str, str | None]
    persisted_child_members: dict[str, str | None]
    member_verification: dict[str, dict[str, Any]]
    source_dates: dict[str, dict[str, str | None]]


def publication_identity(state: Any | None) -> tuple[str | None, str | None]:
    """Read accepted identity without interpreting corrupt state as an initial release."""

    if state is None:
        return None, None
    metadata = _field(state, "validation_metadata")
    if not isinstance(metadata, Mapping):
        return None, "publication_metadata_invalid"
    validation = metadata.get("validation")
    if not isinstance(validation, Mapping):
        if not metadata and not _field(state, "active_pointer") and not _field(state, "generation"):
            return None, None
        return None, "publication_metadata_invalid"
    identity = validation.get("release_identity")
    if not isinstance(identity, str) or not identity:
        return None, "publication_identity_missing"
    return identity, None


def validate_same_release_identity_chain(
    bundle: Any, datasets: Mapping[str, Any], publication: Any | None
) -> SameReleaseIdentityChain:
    """Validate every persisted family/member edge against fresh official discovery.

    The fixed legal→fns_snr and ip→fns_snrip binding is intentional: neither
    database order nor a persisted map may supply the expected identity.
    """

    reasons: list[str] = []

    def fail(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    if not isinstance(bundle.releases, Mapping) or set(bundle.releases) != {"legal", "ip"}:
        accepted, _error = publication_identity(publication)
        return SameReleaseIdentityChain(
            valid=False,
            reasons=("same_release_discovered_member_set_invalid",),
            expected_family_identity=bundle.identity,
            persisted_publication_identity=accepted,
            persisted_publication_members={},
            persisted_family_identity=None,
            expected_members={},
            persisted_family_members={},
            persisted_child_members={},
            member_verification={},
            source_dates={},
        )
    expected = {name: bundle.releases[name] for name in ("legal", "ip")}
    expected_ids = {name: release.identity for name, release in expected.items()}
    metadata = _field(publication, "validation_metadata")
    validation = metadata.get("validation") if isinstance(metadata, Mapping) else None
    publication_members: dict[str, str | None] = {}
    if isinstance(validation, Mapping) and "member_release_identities" in validation:
        raw_members = validation["member_release_identities"]
        if not isinstance(raw_members, Mapping) or set(raw_members) != {"legal", "ip"}:
            fail("same_release_publication_member_map_invalid")
        else:
            publication_members = {
                name: value if isinstance(value, str) else None
                for name, value in raw_members.items()
            }
            if any(publication_members[name] != expected_ids[name] for name in ("legal", "ip")):
                fail("same_release_publication_member_identity_mismatch")
    family = datasets.get(FAMILY_CODE)
    family_raw = _field(family, "coverage")
    family_coverage = family_raw if isinstance(family_raw, Mapping) else {}
    if not isinstance(family_raw, Mapping):
        fail("same_release_family_coverage_invalid")

    accepted, metadata_error = publication_identity(publication)
    if metadata_error or publication is None:
        fail("same_release_publication_invalid")
    if accepted != bundle.identity:
        fail("same_release_publication_identity_mismatch")
    if publication is not None and (
        not isinstance(_field(publication, "active_pointer"), str)
        or not _field(publication, "active_pointer")
        or not isinstance(_field(publication, "generation"), int)
        or _field(publication, "generation") < 1
        or not isinstance(_field(publication, "validation_metadata"), Mapping)
        or not isinstance(_field(publication, "validation_metadata").get("checksum"), str)
        or not _field(publication, "validation_metadata").get("checksum")
    ):
        fail("same_release_publication_generation_invalid")

    family_id = family_coverage.get("release_identity")
    if not isinstance(family_id, str) or not family_id:
        fail("same_release_family_identity_missing")
    elif family_id != bundle.identity:
        fail("same_release_family_identity_mismatch")
    if "family_bundle_identity" in family_coverage and family_coverage["family_bundle_identity"] != bundle.identity:
        fail("same_release_family_bundle_identity_mismatch")

    date_evidence: dict[str, dict[str, str | None]] = {}

    def check_date(label: str, raw: Any, expected_date: date, code: str, *, required: bool) -> None:
        parsed = _date(raw)
        date_evidence[label] = {
            "expected": expected_date.isoformat(),
            "persisted": parsed.isoformat() if parsed else None,
        }
        if (required or raw is not None) and parsed != expected_date:
            fail(code)

    check_date("family.last_data_date", _field(family, "last_data_date"), bundle.source_data_date,
               "same_release_family_date_mismatch", required=True)
    check_date("family.source_as_of", _field(family, "source_as_of"), bundle.source_data_date,
               "same_release_family_date_mismatch", required=False)
    if "source_data_date" in family_coverage:
        check_date("family.coverage.source_data_date", family_coverage["source_data_date"],
                   bundle.source_data_date, "same_release_family_date_mismatch", required=True)
    if isinstance(validation, Mapping) and "source_data_date" in validation:
        check_date("publication.source_data_date", validation["source_data_date"],
                   bundle.source_data_date, "same_release_publication_date_mismatch", required=True)

    members_raw = family_coverage.get("members")
    members = members_raw if isinstance(members_raw, Mapping) else {}
    if not isinstance(members_raw, Mapping):
        fail("same_release_member_map_invalid")
    elif set(members) != {"legal", "ip"}:
        fail("same_release_member_map_invalid")
    family_member_ids: dict[str, str | None] = {}
    child_ids: dict[str, str | None] = {}
    for name, code in (("legal", LEGAL_CODE), ("ip", IP_CODE)):
        member_raw = members.get(name)
        member = member_raw if isinstance(member_raw, Mapping) else {}
        if not isinstance(member_raw, Mapping):
            fail("same_release_member_map_invalid")
        member_id = member.get("release_identity")
        family_member_ids[name] = member_id if isinstance(member_id, str) else None
        if not isinstance(member_id, str) or not member_id:
            fail("same_release_member_identity_missing")
        elif member_id != expected_ids[name]:
            fail("same_release_member_identity_mismatch")
        if "source_data_date" in member:
            check_date(f"members.{name}.source_data_date", member["source_data_date"],
                       expected[name].source_data_date, "same_release_member_date_mismatch", required=True)

        child = datasets.get(code)
        child_raw = _field(child, "coverage")
        child_coverage = child_raw if isinstance(child_raw, Mapping) else {}
        if not isinstance(child_raw, Mapping):
            fail("same_release_child_coverage_invalid")
        child_id = child_coverage.get("release_identity")
        child_ids[code] = child_id if isinstance(child_id, str) else None
        if not isinstance(child_id, str) or not child_id:
            fail("same_release_child_identity_missing")
        elif child_id != expected_ids[name]:
            fail("same_release_child_identity_mismatch")
        if member_id != child_id:
            fail("same_release_cross_representation_mismatch")
        check_date(f"{code}.last_data_date", _field(child, "last_data_date"),
                   expected[name].source_data_date, "same_release_child_date_mismatch", required=True)
        check_date(f"{code}.source_as_of", _field(child, "source_as_of"),
                   expected[name].source_data_date, "same_release_child_date_mismatch", required=False)
        if "source_data_date" in child_coverage:
            check_date(f"{code}.coverage.source_data_date", child_coverage["source_data_date"],
                       expected[name].source_data_date, "same_release_child_date_mismatch", required=True)

    member_verification: dict[str, dict[str, Any]] = {}
    for name, code in (("legal", LEGAL_CODE), ("ip", IP_CODE)):
        member_id = family_member_ids[name]
        child_id = child_ids[code]
        date_labels = (
            f"members.{name}.source_data_date",
            f"{code}.last_data_date",
            f"{code}.source_as_of",
            f"{code}.coverage.source_data_date",
        )
        dates_match = all(
            date_evidence[label]["persisted"] == date_evidence[label]["expected"]
            for label in date_labels if label in date_evidence
        )
        member_reasons = []
        if member_id != expected_ids[name]:
            member_reasons.append("family_member_identity_mismatch")
        if child_id != expected_ids[name]:
            member_reasons.append("child_dataset_identity_mismatch")
        if not dates_match:
            member_reasons.append("source_date_mismatch")
        member_verification[name] = {
            "expected_identity": expected_ids[name],
            "family_member_identity": member_id,
            "child_dataset_code": code,
            "child_identity": child_id,
            "valid": not member_reasons,
            "mismatch_reasons": member_reasons,
        }

    return SameReleaseIdentityChain(
        valid=not reasons, reasons=tuple(reasons),
        expected_family_identity=bundle.identity,
        persisted_publication_identity=accepted,
        persisted_publication_members=publication_members,
        persisted_family_identity=family_id if isinstance(family_id, str) else None,
        expected_members=expected_ids,
        persisted_family_members=family_member_ids,
        persisted_child_members=child_ids,
        member_verification=member_verification,
        source_dates=date_evidence,
    )


@dataclass(frozen=True)
class CoverageDecision:
    state: DataState
    reason: str
    coverage: Any | None = None


def resolve_current_company_coverage(
    rows: Iterable[Any],
    publication: Any | None,
    *,
    company_id: int,
    source_data_date: date | None,
    release_identity: str | None,
) -> CoverageDecision:
    """Accept only coverage frozen to the current pointer, checksum and release."""

    validation = dict(_field(publication, "validation_metadata") or {}) if publication else {}
    accepted_release = (validation.get("validation") or {}).get("release_identity")
    pointer = _field(publication, "active_pointer") if publication else None
    checksum = validation.get("checksum")
    generation = _field(publication, "generation") if publication else None
    if not (pointer and checksum and generation and source_data_date and release_identity):
        return CoverageDecision(DataState.NOT_CHECKED, "current_coverage_unavailable")
    if accepted_release != release_identity:
        return CoverageDecision(DataState.NOT_CHECKED, "current_release_unverified")

    current = []
    for row in rows:
        snapshot = dict(_field(row, "source_snapshot") or {})
        if (
            _field(row, "company_id") == company_id
            and _field(row, "source_id") == FAMILY_CODE
            and _field(row, "worker_source_id") == FAMILY_CODE
            and _field(row, "mode") == "local_bulk_replay"
            and _field(row, "publication_generation") == generation
            and _field(row, "replay_pointer") == pointer
            and _field(row, "replay_checksum") == checksum
            and _date(_field(row, "source_data_date")) == source_data_date
            and snapshot.get("release_identity") == release_identity
        ):
            current.append(row)
    if not current:
        return CoverageDecision(DataState.NOT_CHECKED, "company_not_checked_on_current_release")

    current.sort(
        key=lambda row: (
            _datetime(_field(row, "updated_at")) or datetime.min.replace(tzinfo=timezone.utc),
            str(_field(row, "id") or ""),
        ),
        reverse=True,
    )
    row = current[0]
    status = str(_field(row, "status") or "")
    execution = str(_field(row, "execution_status") or "")
    if execution == "succeeded" and _field(row, "checked_at"):
        if status == "NOT_FOUND" and _field(row, "fact_count") == 0:
            return CoverageDecision(DataState.NOT_FOUND, "checked_current_release", row)
        if status == "FOUND":
            return CoverageDecision(DataState.FOUND, "checked_current_release", row)
        if status in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "STALE_DATA"}:
            return CoverageDecision(DataState(status), f"coverage_{status.lower()}", row)
    if status in {"SOURCE_UNAVAILABLE", "TIMEOUT", "PARSING_ERROR", "STALE_DATA"}:
        return CoverageDecision(DataState(status), f"coverage_{status.lower()}", row)
    if status == "ACCESS_REQUIRED":
        return CoverageDecision(DataState.SOURCE_UNAVAILABLE, "coverage_access_required", row)
    return CoverageDecision(DataState.NOT_CHECKED, "company_check_pending", row)
