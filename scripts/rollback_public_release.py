#!/usr/bin/env python3
"""Atomically switch the public pointer to the exact previous release."""

from __future__ import annotations

import argparse
import json
import os

import psycopg
from psycopg.rows import dict_row


def rollback_release(connection, previous_release_id: str) -> dict:
    with connection.transaction(), connection.cursor(row_factory=dict_row) as cursor:
        cursor.execute(
            "SELECT active_release_id FROM public_publication_state WHERE singleton=TRUE FOR UPDATE"
        )
        state = cursor.fetchone()
        if not state or not state["active_release_id"]:
            raise ValueError("no active public release")
        current = state["active_release_id"]
        cursor.execute(
            "SELECT previous_release_id FROM public_releases WHERE release_id=%s FOR UPDATE",
            (current,),
        )
        release = cursor.fetchone()
        if not release or release["previous_release_id"] != previous_release_id:
            raise ValueError("target is not the exact previous release")
        cursor.execute(
            "SELECT release_id FROM public_releases WHERE release_id=%s FOR UPDATE",
            (previous_release_id,),
        )
        if not cursor.fetchone():
            raise ValueError("previous release does not exist")
        cursor.execute(
            "UPDATE public_releases SET status='rolled_back' WHERE release_id=%s",
            (current,),
        )
        cursor.execute(
            "UPDATE public_releases SET status='active' WHERE release_id=%s",
            (previous_release_id,),
        )
        cursor.execute(
            "UPDATE public_publication_state SET active_release_id=%s, updated_at=now() WHERE singleton=TRUE",
            (previous_release_id,),
        )
    return {"rolled_back_release_id": current, "active_release_id": previous_release_id}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("previous_release_id")
    parser.add_argument("--database-url", default=os.getenv("PUBLIC_IMPORT_DATABASE_URL"))
    args = parser.parse_args()
    if not args.database_url:
        parser.error("PUBLIC_IMPORT_DATABASE_URL or --database-url is required")
    url = args.database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    with psycopg.connect(url) as connection:
        result = rollback_release(connection, args.previous_release_id)
    print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
