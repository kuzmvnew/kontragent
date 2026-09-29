#!/usr/bin/env python3
"""Render staging systemd units from the checked-in production definitions.

Only environment-specific paths, ports, identities and unit names may differ.
Every generated unit records its production source hash so drift is detectable.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SOURCE_DIR = ROOT / "deploy" / "systemd"
CONTRACT_VERSION = 1
UNIT_SUFFIXES = {".service", ".timer"}
TEXT_REPLACEMENTS = (
    ("/home/mikhail/nextcompany-runtime", "/opt/nextcompany-staging"),
    ("/home/mikhail/nextcompany-operational", "/var/lib/nextcompany-staging"),
    ("/opt/nextcompany", "/opt/nextcompany-staging"),
    ("/etc/nextcompany", "/etc/nextcompany-staging"),
    ("/var/backups/nextcompany-operational", "/var/backups/nextcompany-staging-operational"),
    ("/var/backups/nextcompany", "/var/backups/nextcompany-staging"),
    ("/var/lib/nextcompany", "/var/lib/nextcompany-staging"),
    ("--port 8000", "--port 18000"),
    ("--port 8081", "--port 18081"),
)


class UnitParityError(RuntimeError):
    """Generated unit content has drifted from its production source."""


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def production_units(source_dir: Path = SOURCE_DIR) -> list[Path]:
    return [
        path
        for path in sorted(source_dir.iterdir())
        if path.is_file() and path.suffix in UNIT_SUFFIXES
    ]


def staging_name(name: str) -> str:
    if not name.startswith("nextcompany-"):
        raise UnitParityError(f"unexpected production unit name: {name}")
    return name.replace("nextcompany-", "nextcompany-staging-", 1)


def render_text(source: str) -> str:
    rendered = source
    for old, new in TEXT_REPLACEMENTS:
        rendered = rendered.replace(old, new)
    rendered = re.sub(
        r"(?<![a-z0-9_-])nextcompany-([a-z0-9-]+\.(?:service|timer))",
        r"nextcompany-staging-\1",
        rendered,
    )
    rendered = rendered.replace(
        "Description=NEXT Company ",
        "Description=NEXT Company staging ",
    )
    if "[Service]" in rendered:
        marker = "[Service]\n"
        rendered = rendered.replace(
            marker,
            marker
            + "Environment=NEXTCOMPANY_ENVIRONMENT=staging\n"
            + "Environment=PUBLIC_FORCE_NOINDEX=1\n",
            1,
        )
    return rendered


def render_units(
    output_dir: Path,
    *,
    source_dir: Path = SOURCE_DIR,
) -> dict:
    output_dir.mkdir(parents=True, exist_ok=True)
    records = []
    expected_names = set()
    for source_path in production_units(source_dir):
        source = source_path.read_text(encoding="utf-8")
        rendered = render_text(source)
        destination = output_dir / staging_name(source_path.name)
        destination.write_text(rendered, encoding="utf-8")
        expected_names.add(destination.name)
        records.append(
            {
                "production_unit": source_path.name,
                "production_sha256": _sha256(source.encode("utf-8")),
                "staging_unit": destination.name,
                "staging_sha256": _sha256(rendered.encode("utf-8")),
            }
        )
    for stale in output_dir.iterdir():
        if stale.suffix in UNIT_SUFFIXES and stale.name not in expected_names:
            stale.unlink()
    manifest = {
        "contract_version": CONTRACT_VERSION,
        "canonical_source": "deploy/systemd",
        "allowed_differences": [
            "unit name",
            "description",
            "environment values",
            "ports",
            "paths",
            "credentials/identities",
        ],
        "units": records,
    }
    (output_dir / "unit-parity.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def check_units(
    output_dir: Path,
    *,
    source_dir: Path = SOURCE_DIR,
) -> dict:
    expected_dir = output_dir.parent / (output_dir.name + ".expected")
    if expected_dir.exists():
        for path in expected_dir.iterdir():
            path.unlink()
        expected_dir.rmdir()
    try:
        expected = render_units(expected_dir, source_dir=source_dir)
        differences = []
        for record in expected["units"]:
            name = record["staging_unit"]
            observed = output_dir / name
            wanted = expected_dir / name
            if not observed.is_file():
                differences.append({"unit": name, "reason": "missing"})
            elif observed.read_bytes() != wanted.read_bytes():
                differences.append({"unit": name, "reason": "content drift"})
        observed_names = {
            path.name
            for path in output_dir.iterdir()
            if path.suffix in UNIT_SUFFIXES
        }
        expected_names = {record["staging_unit"] for record in expected["units"]}
        for name in sorted(observed_names - expected_names):
            differences.append({"unit": name, "reason": "unexpected"})
        if differences:
            raise UnitParityError(json.dumps(differences, sort_keys=True))
        return {"status": "PASS", "unit_count": len(expected_names)}
    finally:
        if expected_dir.exists():
            for path in expected_dir.iterdir():
                path.unlink()
            expected_dir.rmdir()


def parse_args(arguments: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("render", "check"))
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--source-dir", type=Path, default=SOURCE_DIR)
    return parser.parse_args(arguments)


def main(arguments: list[str] | None = None) -> int:
    args = parse_args(arguments)
    try:
        result = (
            render_units(args.output_dir, source_dir=args.source_dir)
            if args.command == "render"
            else check_units(args.output_dir, source_dir=args.source_dir)
        )
    except (OSError, UnitParityError) as error:
        print(json.dumps({"status": "FAIL", "error": str(error)}))
        return 1
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
