import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def local_name(tag):
    """
    Убирает XML namespace из имени тега.
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def print_element(
    element,
    depth=0,
    max_depth=10,
):
    """
    Печатает XML-структуру документа.
    """

    indent = "    " * depth

    print(
        f"{indent}<{local_name(element.tag)}>"
    )

    for key, value in element.attrib.items():
        print(
            f"{indent}    @{key} = {value}"
        )

    text = (
        element.text.strip()
        if element.text
        else ""
    )

    if text:
        print(
            f"{indent}    TEXT = {text}"
        )

    if depth >= max_depth:
        return

    for child in element:
        print_element(
            child,
            depth=depth + 1,
            max_depth=max_depth,
        )


def copy_element(
    element,
):
    """
    Делает независимую копию XML-элемента.
    """

    return ET.fromstring(
        ET.tostring(
            element,
            encoding="utf-8",
        )
    )


def find_documents(
    archive,
    xml_files,
    max_documents=5,
):
    """
    Находит несколько первых документов
    в ZIP без распаковки архива.
    """

    results = []

    files_scanned = 0

    for info in xml_files:

        files_scanned += 1

        with archive.open(
            info,
            "r",
        ) as xml_file:

            context = ET.iterparse(
                xml_file,
                events=(
                    "start",
                    "end",
                ),
            )

            try:
                _event, root = next(
                    context
                )

            except StopIteration:
                continue

            for event, element in context:

                if event != "end":
                    continue

                if (
                    local_name(
                        element.tag
                    )
                    != "Документ"
                ):
                    continue

                results.append(
                    {
                        "file_name": (
                            info.filename
                        ),
                        "document": (
                            copy_element(
                                element
                            )
                        ),
                    }
                )

                element.clear()
                root.clear()

                if (
                    len(results)
                    >= max_documents
                ):
                    return {
                        "documents": results,
                        "files_scanned": (
                            files_scanned
                        ),
                    }

    return {
        "documents": results,
        "files_scanned": (
            files_scanned
        ),
    }


def collect_attributes(
    document,
):
    """
    Собирает все теги с атрибутами,
    чтобы увидеть реальные названия полей.
    """

    rows = []

    for element in document.iter():

        if not element.attrib:
            continue

        rows.append(
            {
                "tag": local_name(
                    element.tag
                ),
                "attributes": dict(
                    element.attrib
                ),
            }
        )

    return rows


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Инспектор официального "
            "XML-набора taxoffence ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к ZIP-файлу "
            "taxoffence ФНС"
        ),
    )

    args = parser.parse_args()

    path = Path(
        args.zip_path
    )

    if not path.exists():
        raise FileNotFoundError(
            f"Файл не найден: {path}"
        )

    print()
    print(
        "=========================================="
    )
    print(
        "FNS TAX OFFENCE ZIP INSPECTOR"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "ZIP:",
        path,
    )

    print()

    with ZipFile(
        path,
        "r",
    ) as archive:

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
            "XML-файлов:",
            len(xml_files),
        )

        if not xml_files:
            raise RuntimeError(
                "В архиве нет XML-файлов"
            )

        print()
        print(
            "Первые XML-файлы:"
        )

        for info in xml_files[:10]:

            print(
                " -",
                info.filename,
                "|",
                info.file_size,
                "bytes",
            )

        result = find_documents(
            archive=archive,
            xml_files=xml_files,
            max_documents=5,
        )

        print()
        print(
            "Просмотрено XML-файлов:",
            result[
                "files_scanned"
            ],
        )

        print(
            "Найдено документов:",
            len(
                result[
                    "documents"
                ]
            ),
        )

        if not result[
            "documents"
        ]:
            raise RuntimeError(
                "Тег <Документ> не найден"
            )

        for number, item in enumerate(
            result[
                "documents"
            ],
            start=1,
        ):

            document = (
                item[
                    "document"
                ]
            )

            print()
            print(
                "=========================================="
            )
            print(
                "ДОКУМЕНТ",
                number,
            )
            print(
                "=========================================="
            )

            print(
                "XML:",
                item[
                    "file_name"
                ],
            )

            print()
            print(
                "Атрибуты <Документ>:"
            )

            if document.attrib:

                for key, value in (
                    document.attrib.items()
                ):

                    print(
                        f"  {key} = {value}"
                    )

            else:

                print(
                    "  нет"
                )

            print()
            print(
                "Полная структура:"
            )
            print()

            print_element(
                document,
                max_depth=10,
            )

            print()
            print(
                "Все теги с атрибутами:"
            )
            print()

            rows = collect_attributes(
                document
            )

            for row in rows:

                print(
                    row["tag"],
                    "=>",
                    row[
                        "attributes"
                    ],
                )

            print()


if __name__ == "__main__":
    main()