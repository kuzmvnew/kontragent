"""Shared deterministic bundle validation for public release tools."""

from __future__ import annotations

import gzip
import hashlib
import json
from pathlib import Path

from public_app.contracts import PublicProjection, ReleaseManifest, scan_forbidden


EXPECTED_FILES = {"manifest.json", "companies.jsonl.gz"}


def canonical_json(value) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def payload_sha256(projection: PublicProjection) -> str:
    return hashlib.sha256(
        canonical_json(projection.model_dump(mode="json"))
    ).hexdigest()


def write_checksums(bundle_dir: Path) -> None:
    lines = [f"{sha256_file(bundle_dir / name)}  {name}" for name in sorted(EXPECTED_FILES)]
    (bundle_dir / "checksums.sha256").write_text("\n".join(lines) + "\n", encoding="utf-8")


def verify_checksums(bundle_dir: Path) -> None:
    checksum_path = bundle_dir / "checksums.sha256"
    if not checksum_path.is_file():
        raise ValueError("checksums.sha256 is missing")
    observed: dict[str, str] = {}
    for line in checksum_path.read_text(encoding="utf-8").splitlines():
        parts = line.split("  ", 1)
        if len(parts) != 2 or len(parts[0]) != 64:
            raise ValueError("invalid checksum line")
        digest, name = parts
        if name in observed or Path(name).name != name:
            raise ValueError("unsafe or duplicate checksum filename")
        observed[name] = digest
    if set(observed) != EXPECTED_FILES:
        raise ValueError("checksum inventory must contain exactly manifest and companies")
    for name, expected in observed.items():
        path = bundle_dir / name
        if not path.is_file() or sha256_file(path) != expected:
            raise ValueError(f"checksum mismatch: {name}")


def load_bundle(bundle_dir: Path) -> tuple[ReleaseManifest, list[PublicProjection], str]:
    verify_checksums(bundle_dir)
    manifest_path = bundle_dir / "manifest.json"
    manifest_bytes = manifest_path.read_bytes()
    manifest = ReleaseManifest.model_validate_json(manifest_bytes)
    if manifest.companies_file != "companies.jsonl.gz":
        raise ValueError("unexpected companies file")
    projections: list[PublicProjection] = []
    with gzip.open(bundle_dir / manifest.companies_file, "rt", encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            if not line.strip():
                raise ValueError(f"empty JSONL record at line {number}")
            raw = json.loads(line)
            scan_forbidden(raw)
            projections.append(PublicProjection.model_validate(raw))
    if len(projections) != manifest.record_count or len(projections) != 40:
        raise ValueError("bundle must contain exactly 40 projections")
    inns = [projection.company.inn for projection in projections]
    if len(set(inns)) != 40:
        raise ValueError("bundle contains duplicate INNs")
    if any(projection.publication.release_id != manifest.release_id for projection in projections):
        raise ValueError("projection release_id mismatch")
    if any(projection.publication.schema_version != manifest.schema_version for projection in projections):
        raise ValueError("projection schema_version mismatch")
    return manifest, projections, hashlib.sha256(manifest_bytes).hexdigest()
