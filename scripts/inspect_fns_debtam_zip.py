import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def local_name(tag):
    """
    Убирает XML namespace.

    Например:

    {namespace}Документ
        ↓
    Документ
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
    Печатает XML-элемент вместе
    со всеми атрибутами и дочерними
    элементами до заданной глубины.
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


def find_first_documents(
    archive,
    xml_files,
    max_documents=3,
):
    """
    Ищет первые несколько <Документ>
    во всём архиве.

    Не загружает весь ZIP/XML
    в оперативную память.
    """

    results = []

    files_scanned = 0
    documents_scanned = 0

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

                documents_scanned += 1

                # Создаём независимую копию,
                # потому что ниже элемент
                # будет очищен из памяти.
                copy_element = ET.fromstring(
                    ET.tostring(
                        element,
                        encoding="utf-8",
                    )
                )

                results.append(
                    {
                        "file_name": (
                            info.filename
                        ),
                        "document": (
                            copy_element
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
                        "documents_scanned": (
                            documents_scanned
                        ),
                    }

    return {
        "documents": results,
        "files_scanned": (
            files_scanned
        ),
        "documents_scanned": (
            documents_scanned
        ),
    }


def collect_attribute_names(
    document,
):
    """
    Показывает, какие атрибуты
    вообще встретились внутри
    одного документа.

    Это удобно для поиска:
    ИННЮЛ, НаимОрг, СумНедоим,
    Пени, Штраф и т.п.
    """

    rows = []

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        if not element.attrib:
            continue

        rows.append(
            {
                "tag": tag,
                "attributes": (
                    dict(
                        element.attrib
                    )
                ),
            }
        )

    return rows


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Инспектор официального "
            "ZIP/XML набора задолженности ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к ZIP-файлу "
            "debtam ФНС"
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
        "FNS DEBTAM ZIP INSPECTOR"
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
            for info
            in archive.infolist()
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
                "В ZIP не найдено "
                "ни одного XML-файла"
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

        result = (
            find_first_documents(
                archive=archive,
                xml_files=xml_files,
                max_documents=3,
            )
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
                "Тег <Документ> "
                "не найден"
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
                "Атрибуты самого <Документ>:"
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
                    "  нет атрибутов"
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

            attributes = (
                collect_attribute_names(
                    document
                )
            )

            for row in attributes:

                print(
                    row[
                        "tag"
                    ],
                    "=>",
                    row[
                        "attributes"
                    ],
                )

            print()


if __name__ == "__main__":
    main()