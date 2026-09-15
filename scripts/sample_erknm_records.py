from __future__ import annotations

import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


DEFAULT_RECORDS = 2
DEFAULT_VALUE_LIMIT = 220


def local_name(tag: str) -> str:
    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]
    if ":" in tag:
        tag = tag.rsplit(":", 1)[-1]
    return tag


def short_text(value: str, limit: int = DEFAULT_VALUE_LIMIT) -> str:
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[: limit - 1] + "…"


def flatten_element(
    element: ET.Element,
    *,
    prefix: str | None = None,
    value_limit: int = DEFAULT_VALUE_LIMIT,
) -> list[tuple[str, str]]:
    """Возвращает leaf/attribute paths одной записи INSPECTION."""

    root_name = local_name(element.tag)
    current = root_name if prefix is None else f"{prefix}/{root_name}"
    rows: list[tuple[str, str]] = []

    for raw_name, raw_value in element.attrib.items():
        value = short_text(raw_value, value_limit)
        if value:
            rows.append(
                (
                    f"{current}/@{local_name(raw_name)}",
                    value,
                )
            )

    children = list(element)

    if not children:
        value = short_text(element.text or "", value_limit)
        if value:
            rows.append((current, value))
        return rows

    for child in children:
        rows.extend(
            flatten_element(
                child,
                prefix=current,
                value_limit=value_limit,
            )
        )

    return rows


def select_xml_member(archive: ZipFile) -> str:
    xml_members = [
        info
        for info in archive.infolist()
        if not info.is_dir()
        and info.filename.lower().endswith(".xml")
    ]

    if not xml_members:
        raise ValueError("XML внутри ZIP не найден")

    selected = max(
        xml_members,
        key=lambda info: info.file_size,
    )
    return selected.filename


def sample_records_from_stream(
    stream,
    *,
    limit: int = DEFAULT_RECORDS,
    value_limit: int = DEFAULT_VALUE_LIMIT,
) -> list[list[tuple[str, str]]]:
    if limit < 1:
        raise ValueError("limit должен быть больше нуля")

    result: list[list[tuple[str, str]]] = []

    for event, element in ET.iterparse(stream, events=("end",)):
        if local_name(element.tag) != "INSPECTION":
            continue

        result.append(
            flatten_element(
                element,
                value_limit=value_limit,
            )
        )
        element.clear()

        if len(result) >= limit:
            break

    return result


def sample_records_from_zip(
    file_path: str | Path,
    *,
    limit: int = DEFAULT_RECORDS,
    value_limit: int = DEFAULT_VALUE_LIMIT,
) -> tuple[str, list[list[tuple[str, str]]]]:
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    with ZipFile(path) as archive:
        member = select_xml_member(archive)
        with archive.open(member) as stream:
            records = sample_records_from_stream(
                stream,
                limit=limit,
                value_limit=value_limit,
            )

    return member, records


def print_records(
    file_path: str | Path,
    member: str,
    records: list[list[tuple[str, str]]],
) -> None:
    print("=" * 78)
    print("ЕРКНМ — SAMPLE RECORDS")
    print("=" * 78)
    print("ZIP:", Path(file_path).expanduser().resolve())
    print("XML:", member)
    print("Записей:", len(records))

    for index, rows in enumerate(records, start=1):
        print()
        print("=" * 78)
        print(f"INSPECTION #{index}")
        print("=" * 78)

        for path, value in rows:
            print(f"{path} = {value}")


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Потоково печатает несколько полных записей INSPECTION "
            "из официального ZIP ФГИС ЕРКНМ для фиксации parser contract"
        )
    )
    parser.add_argument("file", help="Путь к официальному ERKNM ZIP")
    parser.add_argument(
        "--records",
        type=int,
        default=DEFAULT_RECORDS,
    )
    parser.add_argument(
        "--value-limit",
        type=int,
        default=DEFAULT_VALUE_LIMIT,
    )

    args = parser.parse_args()

    member, records = sample_records_from_zip(
        args.file,
        limit=args.records,
        value_limit=args.value_limit,
    )

    print_records(
        args.file,
        member,
        records,
    )


if __name__ == "__main__":
    main()
