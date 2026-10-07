"""Pure, fail-closed C6 publication lifecycle and admission evidence rules."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from enum import Enum
from hashlib import sha256
import json
import re
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import unquote, urlparse
from uuid import UUID

from app.services.fns_tax_regime_readiness import FAMILY_CODE, IP_CODE, LEGAL_CODE


MEMBER_CODES = {"legal": LEGAL_CODE, "ip": IP_CODE}
SHA256_HEX = re.compile(r"[0-9a-f]{64}\Z")


class PublicationLifecycle(str, Enum):
    NO_PUBLICATION = "NO_PUBLICATION"
    ACCEPTED_VALID = "ACCEPTED_VALID"
    CORRUPT = "CORRUPT"


def canonical_sha256(value: Any) -> bool:
    return type(value) is str and SHA256_HEX.fullmatch(value) is not None


def family_identity(member_identities: Mapping[str, str]) -> str:
    """The same sorted member-name composition as FnsReleaseBundle.identity."""

    return sha256(
        "\n".join(
            f"{name}:{identity}" for name, identity in sorted(member_identities.items())
        ).encode("utf-8")
    ).hexdigest()


def _field(row: Any, name: str) -> Any:
    return row.get(name) if isinstance(row, Mapping) else getattr(row, name, None)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _iso_date(value: Any) -> date | None:
    if type(value) is not str:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _dataset_date(value: Any) -> date | None:
    if type(value) is date:
        return value
    return _iso_date(value)


def _as_of_date(value: Any) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    return _dataset_date(value)


def _canonical_file_pointer(pointer: Any, identity: str) -> bool:
    if type(pointer) is not str:
        return False
    parsed = urlparse(pointer)
    if parsed.scheme != "file" or parsed.netloc or parsed.query or parsed.fragment:
        return False
    path = Path(unquote(parsed.path))
    return (
        path.is_absolute()
        and ".." not in path.parts
        and path.parts[-4:] == ("fns_tax_regime", "bundles", identity, "normalized-bundle.json")
        and path.as_uri() == pointer
    )


def _has_prior_publication_evidence(
    datasets: Mapping[str, Any], *, published_fact_count: int
) -> bool:
    if published_fact_count > 0:
        return True
    for code in (FAMILY_CODE, LEGAL_CODE, IP_CODE):
        row = datasets.get(code)
        if row is None:
            continue
        coverage = _field(row, "coverage")
        if not isinstance(coverage, Mapping) and coverage is not None:
            return True
        if isinstance(coverage, Mapping) and any(
            key in coverage for key in ("release_identity", "family_bundle_identity", "members")
        ):
            return True
        if any(
            _field(row, key) is not None
            for key in ("last_success_at", "published_at", "source_as_of", "last_data_date")
        ):
            return True
        count = _field(row, "record_count")
        if type(count) is int and count > 0:
            return True
    return False


@dataclass(frozen=True)
class AcceptedPublicationProof:
    source_id: str
    release_identity: str
    generation: int
    active_pointer: str
    checksum: str
    source_data_date: date
    member_release_identities: dict[str, str]
    last_fencing_token: int
    published_by_run_id: str | None
    updated_at: str | None

    def as_evidence(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "release_identity": self.release_identity,
            "generation": self.generation,
            "active_pointer": self.active_pointer,
            "checksum": self.checksum,
            "source_data_date": self.source_data_date.isoformat(),
            "member_release_identities": self.member_release_identities,
            "last_fencing_token": self.last_fencing_token,
            "published_by_run_id": self.published_by_run_id,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class PublicationProofResult:
    lifecycle: PublicationLifecycle
    reasons: tuple[str, ...]
    proof: AcceptedPublicationProof | None

    @property
    def valid(self) -> bool:
        return self.lifecycle != PublicationLifecycle.CORRUPT


def validate_publication_lifecycle(
    state: Any | None,
    datasets: Mapping[str, Any],
    *,
    published_fact_count: int = 0,
) -> PublicationProofResult:
    """Classify only after validating the entire old accepted publication graph."""

    if state is None:
        if _has_prior_publication_evidence(datasets, published_fact_count=published_fact_count):
            return PublicationProofResult(
                PublicationLifecycle.CORRUPT, ("lost_accepted_publication_state",), None
            )
        return PublicationProofResult(PublicationLifecycle.NO_PUBLICATION, (), None)

    reasons: list[str] = []

    def fail(code: str) -> None:
        if code not in reasons:
            reasons.append(code)

    metadata = _field(state, "validation_metadata")
    validation = metadata.get("validation") if isinstance(metadata, Mapping) else None
    if not isinstance(metadata, Mapping) or not isinstance(validation, Mapping):
        fail("accepted_validation_metadata_invalid")
    validation = _mapping(validation)

    identity = validation.get("release_identity")
    if not canonical_sha256(identity):
        fail("accepted_family_identity_invalid")
    raw_members = validation.get("member_release_identities")
    if not isinstance(raw_members, Mapping) or set(raw_members) != set(MEMBER_CODES):
        fail("accepted_member_map_invalid")
    members = _mapping(raw_members)
    member_ids = {name: members.get(name) for name in MEMBER_CODES}
    for name, value in member_ids.items():
        if not canonical_sha256(value):
            fail(f"accepted_{name}_identity_invalid")
    if canonical_sha256(identity) and all(canonical_sha256(value) for value in member_ids.values()):
        if family_identity(member_ids) != identity:
            fail("accepted_family_composition_mismatch")

    source_date = _iso_date(validation.get("source_data_date"))
    if source_date is None:
        fail("accepted_source_date_invalid")
    checksum = metadata.get("checksum") if isinstance(metadata, Mapping) else None
    if not canonical_sha256(checksum):
        fail("accepted_checksum_invalid")
    generation = _field(state, "generation")
    if type(generation) is not int or generation < 1:
        fail("accepted_generation_invalid")
    fencing = _field(state, "last_fencing_token")
    if type(fencing) is not int or fencing < 0:
        fail("accepted_fencing_token_invalid")
    pointer = _field(state, "active_pointer")
    if not canonical_sha256(identity) or not _canonical_file_pointer(pointer, identity):
        fail("accepted_pointer_invalid")
    if _field(state, "source_id") != FAMILY_CODE:
        fail("accepted_source_id_invalid")

    family = datasets.get(FAMILY_CODE)
    family_coverage = _field(family, "coverage")
    if not isinstance(family_coverage, Mapping):
        fail("accepted_family_coverage_invalid")
    family_coverage = _mapping(family_coverage)
    if family_coverage.get("release_identity") != identity:
        fail("accepted_family_dataset_identity_mismatch")
    if "family_bundle_identity" in family_coverage and family_coverage["family_bundle_identity"] != identity:
        fail("accepted_family_bundle_identity_mismatch")
    family_members = family_coverage.get("members")
    if not isinstance(family_members, Mapping) or set(family_members) != set(MEMBER_CODES):
        fail("accepted_family_member_map_invalid")
    family_members = _mapping(family_members)

    for name, code in MEMBER_CODES.items():
        member = family_members.get(name)
        if not isinstance(member, Mapping):
            fail(f"accepted_{name}_family_member_invalid")
        member = _mapping(member)
        if member.get("release_identity") != member_ids[name]:
            fail(f"accepted_{name}_family_member_mismatch")
        if "source_data_date" in member and _iso_date(member["source_data_date"]) != source_date:
            fail(f"accepted_{name}_family_member_date_mismatch")
        child = datasets.get(code)
        child_coverage = _field(child, "coverage")
        if not isinstance(child_coverage, Mapping):
            fail(f"accepted_{name}_child_coverage_invalid")
        child_coverage = _mapping(child_coverage)
        if child_coverage.get("release_identity") != member_ids[name]:
            fail(f"accepted_{name}_child_identity_mismatch")
        if "source_data_date" in child_coverage and _iso_date(child_coverage["source_data_date"]) != source_date:
            fail(f"accepted_{name}_child_coverage_date_mismatch")
        if source_date is None or _dataset_date(_field(child, "last_data_date")) != source_date:
            fail(f"accepted_{name}_child_date_mismatch")
        as_of = _field(child, "source_as_of")
        if as_of is not None and _as_of_date(as_of) != source_date:
            fail(f"accepted_{name}_child_as_of_mismatch")

    if source_date is None or _dataset_date(_field(family, "last_data_date")) != source_date:
        fail("accepted_family_date_mismatch")
    as_of = _field(family, "source_as_of")
    if as_of is not None and _as_of_date(as_of) != source_date:
        fail("accepted_family_as_of_mismatch")
    if "source_data_date" in family_coverage and _iso_date(family_coverage["source_data_date"]) != source_date:
        fail("accepted_family_coverage_date_mismatch")

    if reasons:
        return PublicationProofResult(PublicationLifecycle.CORRUPT, tuple(reasons), None)
    published_by = _field(state, "published_by_run_id")
    updated_at = _field(state, "updated_at")
    proof = AcceptedPublicationProof(
        source_id=FAMILY_CODE,
        release_identity=identity,
        generation=generation,
        active_pointer=pointer,
        checksum=checksum,
        source_data_date=source_date,
        member_release_identities=dict(member_ids),
        last_fencing_token=fencing,
        published_by_run_id=str(published_by) if published_by is not None else None,
        updated_at=updated_at.isoformat() if isinstance(updated_at, datetime) else None,
    )
    return PublicationProofResult(PublicationLifecycle.ACCEPTED_VALID, (), proof)


def canonical_fingerprint(value: Any) -> str:
    """Hash deterministic JSON admission evidence; never use repr()."""

    def normalize(item: Any) -> Any:
        if isinstance(item, Mapping):
            return {str(key): normalize(value) for key, value in item.items()}
        if isinstance(item, (list, tuple)):
            return [normalize(value) for value in item]
        if isinstance(item, (date, datetime)):
            return item.isoformat()
        if isinstance(item, UUID):
            return str(item)
        if isinstance(item, Enum):
            return item.value
        if item is None or type(item) in (str, bool, int, float):
            return item
        raise TypeError(f"unsupported admission evidence type: {type(item).__name__}")

    payload = json.dumps(
        normalize(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")
    return sha256(payload).hexdigest()
