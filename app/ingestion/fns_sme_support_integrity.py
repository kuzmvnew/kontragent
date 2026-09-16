"""Independent full-archive control, separate from the business normalizer.

Completeness means ALL members of this downloaded ZIP, not FNS's internal DB.
A known open-data 4.04 KolDok=1 mismatch is reported, never called a match.
CRC/EOF, structural counts and a SHA-256 manifest replace that faulty header
as the processing-completeness evidence. Other mismatches remain fatal.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile

POLICY = "fns-open-data-4.04-full-archive-v1"


def sha256_file(path):
    digest = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class StructuralCounter:
    """No normalizer, no recipient values, no full XML tree in memory."""
    def __init__(self):
        self.stack = []
        self.header = {}
        self.documents = self.facts = self.eligible = self.excluded = 0
        self.kind = None
        self.recipient_count = self.document_facts = 0
        self.dates = set()
        self.date_documents = 0
        self.closed = False

    def start(self, tag, attributes):
        tag = str(tag).rsplit("}", 1)[-1]
        self.stack.append(tag)
        path = tuple(self.stack)
        if len(path) == 1:
            if tag != "Файл":
                raise ValueError("Expected Файл root")
            self.header = dict(attributes)
        elif path == ("Файл", "Документ"):
            self.documents += 1
            self.kind = None
            self.recipient_count = self.document_facts = 0
            raw_date = attributes.get("ДатаСост")
            if raw_date:
                self.dates.add(datetime.strptime(raw_date, "%d.%m.%Y").date().isoformat())
                self.date_documents += 1
            if len(self.dates) > 1:
                raise ValueError("Mixed ДатаСост dates in one XML")
        elif len(path) == 3 and path[:2] == ("Файл", "Документ"):
            if tag in {"СвЮЛ", "СвФЛ"}:
                self.recipient_count += 1
                self.kind = "eligible" if tag == "СвЮЛ" or attributes.get("ОГРНИП") else "excluded"
            elif tag == "СвПредПод":
                self.document_facts += 1
        elif tag in {"Документ", "СвПредПод", "СвЮЛ", "СвФЛ"}:
            raise ValueError("Unexpected nested source record; cannot prove complete processing")

    def data(self, text):
        pass

    def end(self, tag):
        path = tuple(self.stack)
        if path == ("Файл", "Документ"):
            if self.document_facts:
                if self.recipient_count != 1 or self.kind is None:
                    raise ValueError("Independent audit: ambiguous recipient")
                self.facts += self.document_facts
                if self.kind == "eligible":
                    self.eligible += self.document_facts
                else:
                    self.excluded += self.document_facts
            elif self.header.get("ТипИнф") == "РЕЕСТРМСП-ПП":
                raise ValueError("Official document has no СвПредПод")
            else:
                # Legacy flat fixtures: their eligibility is checked by normalizer.
                self.facts += 1
        self.stack.pop()

    def close(self):
        self.closed = True
        if self.stack or not self.documents:
            raise ValueError("Empty or unfinished XML")
        declared = int(self.header.get("КолДок", "0"))
        if declared < 1:
            raise ValueError("Missing/invalid КолДок")
        is_official_shape = (self.header.get("ВерсФорм") == "4.04"
                             and self.header.get("ТипИнф") == "РЕЕСТРМСП-ПП")
        mismatch = declared != self.documents
        if mismatch and not (is_official_shape and declared == 1 and self.documents > 1):
            raise ValueError(f"Unrecognized КолДок mismatch: {declared} != {self.documents}")
        if is_official_shape and self.date_documents != self.documents:
            raise ValueError("Missing ДатаСост on official recipient document")
        return {
            "declared_documents": declared, "source_documents": self.documents,
            "source_records": self.facts, "eligible_records": self.eligible,
            "excluded_npd_records": self.excluded, "official_shape": is_official_shape,
            "header_count_check": "MISMATCH" if mismatch else "PASS",
            "data_date": next(iter(self.dates), None), "xml_eof_valid": True,
            "version": self.header.get("ВерсФорм"), "information_type": self.header.get("ТипИнф"),
        }


def count_xml(stream):
    target = StructuralCounter()
    parser = ET.XMLParser(target=target)
    for chunk in iter(lambda: stream.read(1024 * 1024), b""):
        parser.feed(chunk)
    return parser.close()


def audit_archive(path):
    """Read each member to EOF (ZipExtFile verifies CRC), count independently."""
    path = Path(path)
    print("W1-003: независимый контроль всех файлов ZIP, CRC и XML", flush=True)
    archive_hash = sha256_file(path)
    entries = []
    total = dict.fromkeys(("source_documents", "source_records", "declared_documents"), 0)
    dates = set()
    try:
        with ZipFile(path) as archive:
            members = [x for x in archive.infolist() if not x.is_dir()]
            names = [x.filename for x in members]
            if len(names) != len(set(names)):
                raise ValueError("Duplicate ZIP member names")
            for index, member in enumerate(members, 1):
                with archive.open(member) as stream:
                    if member.filename.lower().endswith(".xml"):
                        stats = count_xml(stream)
                        entry = {"filename": member.filename, "crc32": member.CRC,
                                 "file_size": member.file_size, **stats}
                        entries.append(entry)
                        for key in total:
                            total[key] += stats[key]
                        if stats["data_date"]:
                            dates.add(stats["data_date"])
                    else:
                        for _ in iter(lambda: stream.read(1024 * 1024), b""):
                            pass
                if index == 1 or index % 500 == 0 or index == len(members):
                    print(f"W1-003: проверено ZIP-файлов {index}/{len(members)}", flush=True)
    except (ET.ParseError, BadZipFile) as error:
        raise ValueError(f"Archive/XML integrity failure: {error}") from error
    if not entries or len(dates) > 1:
        raise ValueError("No XML or inconsistent snapshot dates")
    anomalies = [x for x in entries if x["header_count_check"] == "MISMATCH"]
    manifest = {"policy": POLICY, "archive_sha256": archive_hash,
                "archive_bytes": path.stat().st_size, "members": entries}
    manifest_path = path.with_suffix(path.suffix + ".integrity.json")
    raw = json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2).encode("utf-8")
    manifest_path.write_bytes(raw)
    return {
        "members": {x["filename"]: x for x in entries},
        "summary": {"policy": POLICY, "archive_sha256": archive_hash,
                    "archive_bytes": path.stat().st_size, "xml_files": len(entries),
                    "archive_members": len(members), "crc_eof_all_members": True,
                    "independent_counts": total,
                    "data_date": next(iter(dates), None),
                    "header_count_check": "MISMATCH" if anomalies else "PASS",
                    "header_count_mismatch_files": len(anomalies),
                    "header_count_examples": [{k: x[k] for k in ("filename", "declared_documents", "source_documents")}
                                              for x in anomalies[:3]],
                    "manifest_path": str(manifest_path),
                    "manifest_sha256": hashlib.sha256(raw).hexdigest(),
                    "completeness_scope": "all members of downloaded official ZIP; not internal FNS database"},
    }


def verify_counts(stats, proof):
    if stats.get("source_documents", stats["source_records"]) != proof["source_documents"]:
        raise ValueError("Independent document count != parser count")
    if stats["source_records"] != proof["source_records"]:
        raise ValueError("Independent support fact count != parser count")
    if proof["official_shape"]:
        for key in ("eligible_records", "excluded_npd_records"):
            if stats[key] != proof[key]:
                raise ValueError(f"Independent {key} != parser count")
