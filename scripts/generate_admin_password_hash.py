"""Interactively create an ADMIN_PASSWORD_HASH without echoing plaintext."""

from __future__ import annotations

from getpass import getpass

from admin_app.auth import hash_password


def main() -> None:
    password = getpass("New owner password (minimum 14 characters): ")
    repeated = getpass("Repeat owner password: ")
    if password != repeated:
        raise SystemExit("Passwords do not match")
    print(hash_password(password))


if __name__ == "__main__":
    main()
