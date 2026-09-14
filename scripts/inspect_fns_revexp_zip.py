import argparse
import hashlib
from collections import Counter, defaultdict
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import BadZipFile, ZipFile, is_zipfile


DOCUMENT_TAG = "Документ"


def local_name(tag: str) -> str:
    """
    Убирает XML namespace из имени тега.
    """

    return tag.rsplit("}", 1)[-1]


def sha256(path: Path) -> str:
    """
    Вычисляет SHA-256 архива,
    не загружая весь файл в память.
    """

    digest = hashlib.sha256()

    with path.open("rb") as source:
        for chunk in iter(
            lambda: source.read(1024 * 1024),
            b"",
        ):
            digest.update(chunk)

    return digest.hexdigest()


def short(
    value: str | None,
    limit: int = 200,
) -> str:
    """
    Ограничивает длину диагностического вывода.
    """

    if value is None:
        return ""

    clean = " ".join(
        value.split()
    )

    if len(clean) <= limit:
        return clean

    return clean[: limit - 3] + "..."


def print_element(
    element: ET.Element,
    depth: int = 0,
    max_depth: int = 4,
) -> None:
    """
    Печатает ветку XML:
    теги, атрибуты и текст.
    """

    indent = "    " * depth
    tag = local_name(
        element.tag
    )

    print(
        f"{indent}<{tag}>"
    )

    for key, value in sorted(
        element.attrib.items()
    ):
        attribute_name = local_name(
            key
        )

        print(
            f"{indent}    "
            f"@{attribute_name} = "
            f"{short(value)}"
        )

    text = short(
        element.text
    )

    if text:
        print(
            f"{indent}    "
            f"TEXT = {text}"
        )

    if depth >= max_depth:
        return

    for child in element:
        print_element(
            child,
            depth=depth + 1,
            max_depth=max_depth,
        )


def inspect_document(
    document: ET.Element,
    tag_counts: Counter,
    attributes_by_tag: dict[str, Counter],
) -> None:
    """
    Собирает фактические теги и атрибуты
    одного документа.
    """

    for element in document.iter():
        tag = local_name(
            element.tag
        )

        tag_counts[tag] += 1

        for attribute in element.attrib:
            attribute_name = local_name(
                attribute
            )

            attributes_by_tag[
                tag
            ][
                attribute_name
            ] += 1


def read_sample(
    archive: ZipFile,
    xml_files,
    sample_files: int,
    sample_documents: int,
) -> dict:
    """
    Читает небольшую выборку прямо из ZIP.

    Архив на диск не распаковывается.
    База данных не используется.
    """

    documents = []
    tag_counts = Counter()

    attributes_by_tag = defaultdict(
        Counter
    )

    roots = []
    parse_errors = []

    for info in xml_files[
        :sample_files
    ]:
        try:
            with archive.open(
                info,
                "r",
            ) as xml_file:
                tree = ET.parse(
                    xml_file
                )

        except ET.ParseError as error:
            parse_errors.append(
                (
                    info.filename,
                    str(error),
                )
            )
            continue

        root = tree.getroot()

        roots.append(
            {
                "file": info.filename,
                "tag": local_name(
                    root.tag
                ),
                "attributes": dict(
                    root.attrib
                ),
            }
        )

        for element in root.iter():
            if (
                local_name(
                    element.tag
                )
                != DOCUMENT_TAG
            ):
                continue

            inspect_document(
                element,
                tag_counts,
                attributes_by_tag,
            )

            if (
                len(documents)
                < sample_documents
            ):
                documents.append(
                    (
                        info.filename,
                        element,
                    )
                )

    return {
        "documents": documents,
        "tag_counts": tag_counts,
        "attributes_by_tag": (
            attributes_by_tag
        ),
        "roots": roots,
        "parse_errors": parse_errors,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Проверка официального "
            "ZIP/XML ФНС REVEXP "
            "без импорта в базу."
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к ZIP-архиву REVEXP"
        ),
    )

    parser.add_argument(
        "--sample-files",
        type=int,
        default=5,
        help=(
            "Количество XML для проверки "
            "(по умолчанию: 5)"
        ),
    )

    parser.add_argument(
        "--sample-documents",
        type=int,
        default=3,
        help=(
            "Количество документов для печати "
            "(по умолчанию: 3)"
        ),
    )

    args = parser.parse_args()

    path = Path(
        args.zip_path
    )

    if not path.is_file():
        raise SystemExit(
            f"ОШИБКА: файл не найден: {path}"
        )

    if (
        args.sample_files < 1
        or args.sample_documents < 1
    ):
        raise SystemExit(
            "ОШИБКА: размеры выборки "
            "должны быть больше нуля"
        )

    if not is_zipfile(
        path
    ):
        raise SystemExit(
            "ОШИБКА: файл не является "
            f"ZIP-архивом: {path}"
        )

    print(
        "=" * 90
    )

    print(
        "FNS REVEXP ZIP INSPECTOR "
        "— БЕЗ ИМПОРТА В БАЗУ"
    )

    print(
        "=" * 90
    )

    print(
        f"ZIP: {path}"
    )

    print(
        f"Размер: "
        f"{path.stat().st_size} bytes"
    )

    print(
        f"SHA-256: {sha256(path)}"
    )

    try:
        with ZipFile(
            path,
            "r",
        ) as archive:
            bad_member = archive.testzip()

            if bad_member is not None:
                raise SystemExit(
                    "ОШИБКА ZIP: повреждён файл "
                    f"{bad_member}"
                )

            xml_files = [
                info
                for info in archive.infolist()
                if (
                    not info.is_dir()
                    and info.filename
                    .lower()
                    .endswith(".xml")
                )
            ]

            print(
                f"XML-файлов: "
                f"{len(xml_files)}"
            )

            if not xml_files:
                raise SystemExit(
                    "ОШИБКА: XML-файлы "
                    "в архиве не найдены"
                )

            sample_files = min(
                args.sample_files,
                len(xml_files),
            )

            print(
                f"XML в выборке: "
                f"{sample_files}"
            )

            print(
                "Документов для печати: "
                f"до {args.sample_documents}"
            )

            result = read_sample(
                archive=archive,
                xml_files=xml_files,
                sample_files=sample_files,
                sample_documents=(
                    args.sample_documents
                ),
            )

    except BadZipFile as error:
        raise SystemExit(
            f"ОШИБКА чтения ZIP: {error}"
        ) from error

    print()
    print(
        "КОРНЕВЫЕ ЭЛЕМЕНТЫ "
        "В ВЫБОРКЕ"
    )
    print(
        "-" * 90
    )

    for root in result[
        "roots"
    ]:
        print(
            f"Файл: {root['file']}"
        )

        print(
            f"Тег: <{root['tag']}>"
        )

        for key, value in sorted(
            root["attributes"].items()
        ):
            attribute_name = local_name(
                key
            )

            print(
                f"  @{attribute_name} = "
                f"{short(value)}"
            )

    print()
    print(
        "ТЕГИ И АТРИБУТЫ "
        "В НАЙДЕННЫХ <Документ>"
    )
    print(
        "-" * 90
    )

    if not result[
        "tag_counts"
    ]:
        print(
            "Тег <Документ> "
            "в выборке не найден."
        )

    else:
        for tag in sorted(
            result["tag_counts"]
        ):
            count = result[
                "tag_counts"
            ][
                tag
            ]

            attributes = ", ".join(
                sorted(
                    result[
                        "attributes_by_tag"
                    ][
                        tag
                    ]
                )
            )

            if not attributes:
                attributes = "нет"

            print(
                f"<{tag}>: "
                f"элементов={count}; "
                f"атрибуты={attributes}"
            )

    for number, item in enumerate(
        result["documents"],
        start=1,
    ):
        file_name, document = item

        print()
        print(
            f"ДОКУМЕНТ {number}: "
            f"{file_name}"
        )
        print(
            "-" * 90
        )

        print_element(
            document
        )

    if result[
        "parse_errors"
    ]:
        print()
        print(
            "ОШИБКИ XML В ВЫБОРКЕ"
        )
        print(
            "-" * 90
        )

        for file_name, error in result[
            "parse_errors"
        ]:
            print(
                f"{file_name}: {error}"
            )

    print()
    print(
        "ИТОГ"
    )
    print(
        "-" * 90
    )

    print(
        "Разобрано XML: "
        f"{len(result['roots'])}"
    )

    print(
        "Найдено документов "
        "в выборке: "
        f"{result['tag_counts'][DOCUMENT_TAG]}"
    )

    print(
        "Ошибок XML в выборке: "
        f"{len(result['parse_errors'])}"
    )

    print(
        "База данных не изменялась. "
        "Импорт не запускался."
    )


if __name__ == "__main__":
    main()