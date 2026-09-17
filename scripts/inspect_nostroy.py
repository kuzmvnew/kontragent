from __future__ import annotations

import argparse
import json

from app.providers.nostroy_provider import NostroyMemberProvider


def main():
    parser = argparse.ArgumentParser(description="Low-load exact-INN inspection of the official NOSTROY registry")
    parser.add_argument("inn")
    args = parser.parse_args()
    print(json.dumps(NostroyMemberProvider().check_inn(args.inn), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
