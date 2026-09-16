"""Import verified W1-005 snapshots supplied from official RKN URLs.

The downloader is intentionally external to this command: a partial network
response must never be confused with an importable snapshot. This command
validates the complete file, computes SHA-256 and publishes atomically.
"""
from __future__ import annotations

import argparse
from datetime import date
import hashlib
from pathlib import Path

from app.ingestion.roskomnadzor import parse_hosting_xlsx, parse_xml_snapshot, publish_snapshot
from app.services.roskomnadzor_registry_service import DATASETS, ensure_roskomnadzor_datasets

XML_TAGS = {
    "communications": {"license", "licenses", "record"},
    "broadcast": {"license", "licenses", "record"},
    "media": {"media", "massmedia", "record", "resolution"},
    "information_distributors": {"record"},
}


def main():
    parser = argparse.ArgumentParser(description="Publish a complete official Roskomnadzor snapshot")
    parser.add_argument("channel", choices=[*XML_TAGS, "hosting"])
    parser.add_argument("file", type=Path)
    parser.add_argument("--data-date", type=date.fromisoformat, required=True)
    parser.add_argument("--official-metadata-date", type=date.fromisoformat)
    args = parser.parse_args()
    content = args.file.read_bytes()
    checksum = hashlib.sha256(content).hexdigest()
    if args.channel == "hosting":
        parsed = parse_hosting_xlsx(content, data_date=args.data_date)
    else:
        parsed = parse_xml_snapshot(content, channel=args.channel, data_date=args.data_date, record_tags=XML_TAGS[args.channel])
    ensure_roskomnadzor_datasets()
    details = {"source_file_date": args.data_date.isoformat(), "official_metadata_data_date": args.official_metadata_date.isoformat() if args.official_metadata_date else None, "metadata_date_after_file_date": bool(args.official_metadata_date and args.official_metadata_date > args.data_date), "auto_update": "NOT_CONFIGURED"}
    result = publish_snapshot(dataset_code=DATASETS[args.channel], parsed=parsed, data_date=args.data_date, checksum=checksum, source_file_name=args.file.name, details=details)
    print({"channel": args.channel, "sha256": checksum, "source_records": parsed["source_records"], **result})


if __name__ == "__main__":
    main()
