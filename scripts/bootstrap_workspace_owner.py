#!/usr/bin/env python3
"""Create a controlled Workspace P0 owner.

This command is intentionally explicit and create-only. It is not a public
registration endpoint and it does not create or infer a commercial tariff.
"""

from __future__ import annotations

import argparse
from getpass import getpass

from app.database.postgres import SessionLocal
from workspace_app.auth import hash_password
from workspace_app.service import bootstrap_workspace_owner


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--workspace-name", required=True)
    parser.add_argument(
        "--saved-limit",
        type=int,
        required=True,
        help="Explicit P0 saved-company quota for this controlled workspace.",
    )
    parser.add_argument(
        "--password",
        help="Avoid this flag in shared shell history; omit it to use a hidden prompt.",
    )
    args = parser.parse_args()

    password = args.password or getpass("Workspace password: ")
    password_hash = hash_password(password)
    with SessionLocal() as session:
        user, workspace = bootstrap_workspace_owner(
            session,
            email=args.email,
            password_hash=password_hash,
            workspace_name=args.workspace_name,
            saved_company_limit=args.saved_limit,
        )
        session.commit()
        print(f"created user={user.email} workspace={workspace.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
