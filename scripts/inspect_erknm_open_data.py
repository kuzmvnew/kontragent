from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from pathlib import Path
from typing import BinaryIO
from xml.etree import ElementTree as ET
from zipfile import ZipFile


DEFAULT_MAX_ELEMENTS = 50_000
DEFAULT_TOP_PATHS = 80
DEFAULT_SAMPLE_VALUES = 3


def local_name(tag: str) -> str:
    """Возвращает имя XML-тега/атрибута без namespace."""

    if "}" in tag:
        tag = tag.rsplit("}", 1)[-1]

    if ":" in tag:
        tag = tag.rsplit(":", 1)[-1]

    return tag


def _short_text(value: str, limit: int = 180) -> str:
    value = " ".join(value.split())

    if len(value) <= limit:
        return value

    return value[: limit - 1] + "…"


def inspect_xml_stream(
    stream: BinaryIO,
    *,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    sample_values: int = DEFAULT_SAMPLE_VALUES,
) -> dict:
    """
    Потоково инспектирует XML без загрузки всего документа в память.

    Цель — увидеть реальную структуру официального набора до того,
    как мы зафиксируем parser/model contract.
    """

    if max_elements < 1:
        raise ValueError("max_elements должен быть больше нуля")

    if sample_values < 0:
        raise ValueError("sample_values не может быть отрицательным")

    path_counts: Counter[str] = Counter()
    attribute_counts: Counter[str] = Counter()
    value_samples: dict[str, list[str]] = defaultdict(list)
    attribute_samples: dict[str, list[str]] = defaultdict(list)

    stack: list[str] = []
    root_tag: str | None = None
    ended_elements = 0

    context = ET.iterparse(
        stream,
        events=("start", "end"),
    )

    for event, element in context:
        name = local_name(element.tag)

        if event == "start":
            stack.append(name)

            if root_tag is None:
                root_tag = name

            current_path = "/".join(stack)

            for raw_attr_name, raw_attr_value in element.attrib.items():
                attr_name = local_name(raw_attr_name)
                attr_path = f"{current_path}/@{attr_name}"
                attribute_counts[attr_path] += 1

                value = _short_text(str(raw_attr_value))

                if (
                    value
                    and len(attribute_samples[attr_path]) < sample_values
                    and value not in attribute_samples[attr_path]
                ):
                    attribute_samples[attr_path].append(value)

            continue

        current_path = "/".join(stack)
        path_counts[current_path] += 1
        ended_elements += 1

        text = (element.text or "").strip()

        if text:
            value = _short_text(text)

            if (
                len(value_samples[current_path]) < sample_values
                and value not in value_samples[current_path]
            ):
                value_samples[current_path].append(value)

        element.clear()

        if stack:
            stack.pop()

        if ended_elements >= max_elements:
            break

    return {
        "root_tag": root_tag,
        "elements_inspected": ended_elements,
        "path_counts": dict(path_counts),
        "attribute_counts": dict(attribute_counts),
        "value_samples": dict(value_samples),
        "attribute_samples": dict(attribute_samples),
    }


def inspect_xsd_stream(stream: BinaryIO) -> dict:
    """Возвращает компактную сводку XSD."""

    tree = ET.parse(stream)
    root = tree.getroot()

    element_names: list[str] = []
    complex_types: list[str] = []
    simple_types: list[str] = []

    for element in root.iter():
        name = local_name(element.tag)
        declared_name = element.attrib.get("name")

        if not declared_name:
            continue

        if name == "element":
            element_names.append(declared_name)
        elif name == "complexType":
            complex_types.append(declared_name)
        elif name == "simpleType":
            simple_types.append(declared_name)

    return {
        "root_tag": local_name(root.tag),
        "elements": element_names,
        "complex_types": complex_types,
        "simple_types": simple_types,
    }


def inspect_path(
    file_path: str | Path,
    *,
    member: str | None = None,
    max_elements: int = DEFAULT_MAX_ELEMENTS,
    sample_values: int = DEFAULT_SAMPLE_VALUES,
) -> dict:
    path = Path(file_path).expanduser().resolve()

    if not path.exists():
        raise FileNotFoundError(f"Файл не найден: {path}")

    suffix = path.suffix.lower()

    if suffix == ".zip":
        with ZipFile(path) as archive:
            members = [
                info
                for info in archive.infolist()
                if not info.is_dir()
            ]

            xml_members = [
                info.filename
                for info in members
                if info.filename.lower().endswith(".xml")
            ]

            xsd_members = [
                info.filename
                for info in members
                if info.filename.lower().endswith(".xsd")
            ]

            if member is not None:
                if member not in {info.filename for info in members}:
                    raise ValueError(
                        f"Файл {member!r} не найден внутри архива"
                    )

                selected_member = member
            elif xml_members:
                selected_member = max(
                    xml_members,
                    key=lambda name: archive.getinfo(name).file_size,
                )
            else:
                selected_member = None

            report = {
                "kind": "zip",
                "path": str(path),
                "members": [
                    {
                        "name": info.filename,
                        "size": info.file_size,
                    }
                    for info in members
                ],
                "xml_members": xml_members,
                "xsd_members": xsd_members,
                "selected_member": selected_member,
                "xml": None,
            }

            if selected_member is not None:
                with archive.open(selected_member) as stream:
                    report["xml"] = inspect_xml_stream(
                        stream,
                        max_elements=max_elements,
                        sample_values=sample_values,
                    )

            return report

    if suffix == ".xml":
        with path.open("rb") as stream:
            return {
                "kind": "xml",
                "path": str(path),
                "xml": inspect_xml_stream(
                    stream,
                    max_elements=max_elements,
                    sample_values=sample_values,
                ),
            }

    if suffix == ".xsd":
        with path.open("rb") as stream:
            return {
                "kind": "xsd",
                "path": str(path),
                "xsd": inspect_xsd_stream(stream),
            }

    raise ValueError(
        "Поддерживаются только .zip, .xml и .xsd"
    )


def _print_xsd(report: dict) -> None:
    print("XSD root:", report.get("root_tag"))

    print()
    print("Объявленные xs:element:")

    for name in report.get("elements", [])[:200]:
        print(" -", name)

    print()
    print("complexType:")

    for name in report.get("complex_types", [])[:200]:
        print(" -", name)

    print()
    print("simpleType:")

    for name in report.get("simple_types", [])[:200]:
        print(" -", name)


def _print_xml(
    report: dict,
    *,
    top_paths: int,
) -> None:
    print("XML root:", report.get("root_tag"))
    print(
        "Элементов проинспектировано:",
        f"{report.get('elements_inspected', 0):,}".replace(",", " "),
    )

    path_counts = report.get("path_counts") or {}
    value_samples = report.get("value_samples") or {}

    print()
    print("Наиболее частые XML paths:")

    sorted_paths = sorted(
        path_counts.items(),
        key=lambda item: (-item[1], item[0]),
    )

    for path, count in sorted_paths[:top_paths]:
        print(
            f"{count:>8}  {path}"
        )

        samples = value_samples.get(path) or []

        for sample in samples:
            print("          example:", sample)

    attribute_counts = report.get("attribute_counts") or {}
    attribute_samples = report.get("attribute_samples") or {}

    if attribute_counts:
        print()
        print("XML attributes:")

        sorted_attributes = sorted(
            attribute_counts.items(),
            key=lambda item: (-item[1], item[0]),
        )

        for path, count in sorted_attributes[:top_paths]:
            print(
                f"{count:>8}  {path}"
            )

            for sample in attribute_samples.get(path) or []:
                print("          example:", sample)


def print_report(
    report: dict,
    *,
    top_paths: int = DEFAULT_TOP_PATHS,
) -> None:
    print("=" * 78)
    print("ЕРКНМ — STRUCTURE INSPECTOR")
    print("=" * 78)
    print("Файл:", report.get("path"))
    print("Тип:", report.get("kind"))

    if report.get("kind") == "zip":
        print()
        print("Содержимое архива:")

        for item in report.get("members", []):
            print(
                " -",
                item["name"],
                f"({item['size']:,} bytes)".replace(",", " "),
            )

        print()
        print("Выбран XML:", report.get("selected_member"))

        xml_report = report.get("xml")

        if xml_report is None:
            print("XML внутри архива не найден.")
            return

        print()
        _print_xml(
            xml_report,
            top_paths=top_paths,
        )
        return

    if report.get("kind") == "xml":
        print()
        _print_xml(
            report["xml"],
            top_paths=top_paths,
        )
        return

    if report.get("kind") == "xsd":
        print()
        _print_xsd(report["xsd"])


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Потоково инспектирует официальный ZIP/XML/XSD ФГИС ЕРКНМ "
            "до фиксации parser/model contract"
        )
    )

    parser.add_argument(
        "file",
        help="Путь к официальному .zip, .xml или .xsd",
    )
    parser.add_argument(
        "--member",
        default=None,
        help=(
            "Конкретный XML member внутри ZIP. Если не указан, "
            "выбирается самый большой XML-файл."
        ),
    )
    parser.add_argument(
        "--max-elements",
        type=int,
        default=DEFAULT_MAX_ELEMENTS,
        help="Сколько XML-элементов проинспектировать, по умолчанию 50000",
    )
    parser.add_argument(
        "--top-paths",
        type=int,
        default=DEFAULT_TOP_PATHS,
        help="Сколько наиболее частых XML paths показать",
    )
    parser.add_argument(
        "--sample-values",
        type=int,
        default=DEFAULT_SAMPLE_VALUES,
        help="Сколько примеров значений хранить на XML path",
    )

    args = parser.parse_args()

    if args.top_paths < 1:
        raise ValueError("top_paths должен быть больше нуля")

    report = inspect_path(
        args.file,
        member=args.member,
        max_elements=args.max_elements,
        sample_values=args.sample_values,
    )

    print_report(
        report,
        top_paths=args.top_paths,
    )


if __name__ == "__main__":
    main()
