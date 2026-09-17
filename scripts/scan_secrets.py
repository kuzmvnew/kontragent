#!/usr/bin/env python3
"""Small fail-closed secret scan for tracked repository text files."""

from __future__ import annotations

import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EXCLUDED_PREFIXES = ("certs/", ".venv/", "uv.lock")
PATTERNS = (
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)(?:api[_-]?key|access[_-]?token|secret|password)\s*[:=]\s*['\"](?!test|example|changeme)[^'\"\s]{20,}['\"]"),
    re.compile(r"\bgh[opsu]_[A-Za-z0-9]{30,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{24,}\b"),
)


def tracked_files() -> tuple[str, ...]:
    output = subprocess.check_output(
        ["git", "ls-files", "-z"], cwd=ROOT,
    )
    return tuple(item.decode() for item in output.split(b"\0") if item)


def main() -> int:
    findings: list[str] = []
    for relative in tracked_files():
        if relative.startswith(EXCLUDED_PREFIXES):
            continue
        path = ROOT / relative
        try:
            content = path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for number, line in enumerate(content.splitlines(), 1):
            if any(pattern.search(line) for pattern in PATTERNS):
                findings.append(f"{relative}:{number}")
    if findings:
        print("Potential secret material found:")
        print("\n".join(findings))
        return 1
    print(f"Secret scan passed: {len(tracked_files())} tracked files checked")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
