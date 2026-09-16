from __future__ import annotations

import argparse
from datetime import date
import hashlib
from pathlib import Path
import zipfile

from app.ingestion.roszdrav import (
    parse_clinical_csv,
    parse_license_xml,
    publish_snapshots,
    validate_complete_snapshot,
)
from app.models.roszdrav import RoszdravClinicalOrganizationEntry, RoszdravLicenseEntry
from app.services.roszdrav_registry_service import (
    CLINICAL_ORG_DATASET,
    LICENSE_DATASETS,
    ensure_roszdrav_datasets,
)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _read_single_xml_zip(path: Path) -> tuple[bytes, dict]:
    with zipfile.ZipFile(path) as archive:
        bad = archive.testzip()
        if bad is not None:
            raise ValueError(f"ZIP CRC error: {bad}")
        members = [item for item in archive.infolist() if not item.is_dir()]
        if len(members) != 1 or not members[0].filename.lower().endswith(".xml"):
            raise ValueError("Ожидался ZIP ровно с одним XML")
        content = archive.read(members[0])
        if len(content) != members[0].file_size:
            raise ValueError("XML прочитан не полностью")
        return content, {"files_count": 1, "xml_name": members[0].filename, "xml_size": len(content)}


def main():
    parser = argparse.ArgumentParser(description="W1-004 bulk sync Росздравнадзора")
    parser.add_argument("--pharma", type=Path, required=True)
    parser.add_argument("--narcotics", type=Path, required=True)
    parser.add_argument("--medical-device-maintenance", type=Path, required=True)
    parser.add_argument("--clinical", type=Path, required=True)
    parser.add_argument("--license-data-date", type=date.fromisoformat, required=True)
    parser.add_argument("--license-metadata-data-date", type=date.fromisoformat, required=True)
    parser.add_argument("--clinical-data-date", type=date.fromisoformat, required=True)
    parser.add_argument("--clinical-file-date", type=date.fromisoformat, required=True)
    args = parser.parse_args()

    ensure_roszdrav_datasets()
    specs = [
        ("pharma", args.pharma),
        ("narcotics", args.narcotics),
        ("medical_device_maintenance", args.medical_device_maintenance),
    ]
    prepared = []
    for category, path in specs:
        archive_content = path.read_bytes()
        xml_content, archive_info = _read_single_xml_zip(path)
        parsed = parse_license_xml(xml_content, category=category, data_date=args.license_data_date)
        validate_complete_snapshot(parsed)
        prepared.append((category, path, archive_content, parsed, archive_info))

    clinical_content = args.clinical.read_bytes()
    clinical_parsed = parse_clinical_csv(clinical_content, data_date=args.clinical_data_date)
    validate_complete_snapshot(clinical_parsed)

    publications = []
    for category, path, archive_content, parsed, _ in prepared:
        publications.append({
            "dataset_code": LICENSE_DATASETS[category],
            "parsed": parsed,
            "model": RoszdravLicenseEntry,
            "data_date": args.license_data_date,
            "checksum": _sha256(archive_content),
            "source_file_name": path.name,
            "details": {
                "source_file_date": str(args.license_data_date),
                "official_metadata_data_date": str(args.license_metadata_data_date),
                "metadata_date_after_file_date": (
                    args.license_metadata_data_date > args.license_data_date
                ),
            },
        })
    publications.append({
        "dataset_code": CLINICAL_ORG_DATASET,
        "parsed": clinical_parsed,
        "model": RoszdravClinicalOrganizationEntry,
        "data_date": args.clinical_data_date,
        "checksum": _sha256(clinical_content),
        "source_file_name": args.clinical.name,
        "details": {
            "source_file_date": str(args.clinical_file_date),
            "official_metadata_data_date": str(args.clinical_data_date),
        },
    })
    publication_results = publish_snapshots(publications)

    report = {"licenses": {}, "clinical": {}}
    for category, path, archive_content, parsed, archive_info in prepared:
        result = publication_results[LICENSE_DATASETS[category]]
        report["licenses"][category] = {**result, **{key: value for key, value in parsed.items() if key != "records"}, **archive_info, "sha256": _sha256(archive_content)}

    result = publication_results[CLINICAL_ORG_DATASET]
    report["clinical"] = {**result, **{key: value for key, value in clinical_parsed.items() if key != "records"}, "sha256": _sha256(clinical_content)}
    print(report)


if __name__ == "__main__":
    main()
