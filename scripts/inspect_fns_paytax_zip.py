import argparse
from collections import Counter
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


DOCUMENT_TAG = "Документ"


def local_name(tag):
    """
    Убирает XML namespace из имени тега.

    Пример:
    {http://example.ru}Документ
    ->
    Документ
    """

    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def copy_element(element):
    """
    Создаёт независимую копию XML-элемента.

    Это нужно, потому что при потоковом разборе
    исходные элементы очищаются из памяти.
    """

    return ET.fromstring(
        ET.tostring(
            element,
            encoding="utf-8",
        )
    )


def format_attributes(attributes):
    """
    Красиво форматирует XML-атрибуты.
    """

    if not attributes:
        return "{}"

    parts = []

    for key, value in attributes.items():

        parts.append(
            f"{key}={value!r}"
        )

    return "{ " + ", ".join(parts) + " }"


def print_element(
    element,
    depth=0,
    max_depth=12,
):
    """
    Печатает XML-дерево элемента
    вместе с атрибутами и текстом.
    """

    indent = "    " * depth

    tag_name = local_name(
        element.tag
    )

    print(
        f"{indent}<{tag_name}>"
    )

    if element.attrib:

        for key, value in (
            element.attrib.items()
        ):

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


def collect_paths(
    element,
    parent_path="",
    result=None,
):
    """
    Собирает пути XML-тегов.

    Например:
    Документ
    Документ/СведНП
    Документ/СвУплСумНал
    """

    if result is None:
        result = Counter()

    current_name = local_name(
        element.tag
    )

    if parent_path:

        current_path = (
            f"{parent_path}/{current_name}"
        )

    else:

        current_path = current_name

    result[
        current_path
    ] += 1

    for child in element:

        collect_paths(
            child,
            parent_path=current_path,
            result=result,
        )

    return result


def collect_attribute_names(
    element,
    result=None,
):
    """
    Собирает имена атрибутов
    для каждого XML-тега.
    """

    if result is None:
        result = {}

    tag_name = local_name(
        element.tag
    )

    if tag_name not in result:
        result[tag_name] = Counter()

    for attribute_name in (
        element.attrib.keys()
    ):

        result[
            tag_name
        ][
            attribute_name
        ] += 1

    for child in element:

        collect_attribute_names(
            child,
            result=result,
        )

    return result


def find_documents(
    archive,
    xml_files,
    max_documents=5,
):
    """
    Потоково ищет первые N документов
    непосредственно внутри ZIP.

    Архив целиком не распаковывается.
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
                    != DOCUMENT_TAG
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

            root.clear()

    return {
        "documents": results,
        "files_scanned": files_scanned,
    }


def inspect_xml_root(
    archive,
    xml_file_info,
):
    """
    Показывает корневой элемент
    первого XML-файла.
    """

    with archive.open(
        xml_file_info,
        "r",
    ) as xml_file:

        context = ET.iterparse(
            xml_file,
            events=("start",),
        )

        try:

            _event, root = next(
                context
            )

        except StopIteration:

            print(
                "XML-файл пустой."
            )

            return

        print()
        print(
            "=========================================="
        )
        print(
            "КОРНЕВОЙ ЭЛЕМЕНТ XML"
        )
        print(
            "=========================================="
        )
        print()

        print(
            "Тег:",
            local_name(
                root.tag
            ),
        )

        print(
            "Атрибуты:",
            format_attributes(
                root.attrib
            ),
        )


def inspect_xsd(
    xsd_path,
):
    """
    Показывает основные сведения
    из официальной XSD-схемы.
    """

    if xsd_path is None:
        return

    path = Path(
        xsd_path
    )

    if not path.exists():

        print()
        print(
            "XSD не найден:",
            path,
        )

        return

    print()
    print(
        "=========================================="
    )
    print(
        "XSD"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "Файл:",
        path,
    )

    try:

        tree = ET.parse(
            path
        )

    except ET.ParseError as error:

        print(
            "Ошибка чтения XSD:",
            error,
        )

        return

    root = tree.getroot()

    print(
        "Корневой тег:",
        local_name(
            root.tag
        ),
    )

    named_elements = []

    for element in root.iter():

        if (
            local_name(
                element.tag
            )
            != "element"
        ):
            continue

        name = element.attrib.get(
            "name"
        )

        if name:

            named_elements.append(
                name
            )

    print(
        "Именованных xs:element:",
        len(named_elements),
    )

    if named_elements:

        print()

        for name in named_elements[:100]:

            print(
                " -",
                name,
            )


def print_document(
    number,
    item,
):
    """
    Печатает один найденный документ.
    """

    document = item[
        "document"
    ]

    print()
    print(
        "=========================================="
    )
    print(
        f"ДОКУМЕНТ {number}"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "XML-файл:",
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
        "СТРУКТУРА ДОКУМЕНТА"
    )
    print()

    print_element(
        document,
        max_depth=12,
    )

    print()
    print(
        "ПУТИ ТЕГОВ"
    )
    print()

    paths = collect_paths(
        document
    )

    for path, count in (
        paths.items()
    ):

        print(
            f"{path}: {count}"
        )

    print()
    print(
        "АТРИБУТЫ ПО ТЕГАМ"
    )
    print()

    attributes = (
        collect_attribute_names(
            document
        )
    )

    for tag_name in sorted(
        attributes
    ):

        attribute_counter = (
            attributes[
                tag_name
            ]
        )

        if not attribute_counter:
            continue

        print(
            tag_name
        )

        for (
            attribute_name,
            count,
        ) in (
            attribute_counter.items()
        ):

            print(
                f"    @{attribute_name}: "
                f"{count}"
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Инспектор официального "
            "набора PAYTAX ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к ZIP-архиву "
            "PAYTAX ФНС"
        ),
    )

    parser.add_argument(
        "--xsd",
        default=None,
        help=(
            "Путь к официальной "
            "XSD-схеме"
        ),
    )

    parser.add_argument(
        "--documents",
        type=int,
        default=3,
        help=(
            "Сколько первых "
            "<Документ> показать"
        ),
    )

    parser.add_argument(
        "--list-files",
        type=int,
        default=10,
        help=(
            "Сколько XML-файлов "
            "показать из архива"
        ),
    )

    args = parser.parse_args()

    zip_path = Path(
        args.zip_path
    )

    if not zip_path.exists():

        raise FileNotFoundError(
            f"ZIP не найден: {zip_path}"
        )

    if args.documents < 1:

        raise ValueError(
            "--documents должен быть >= 1"
        )

    print()
    print(
        "=========================================="
    )
    print(
        "ФНС — PAYTAX"
    )
    print(
        "ИНСПЕКТОР АРХИВА"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "ZIP:",
        zip_path,
    )

    print(
        "Размер:",
        zip_path.stat().st_size,
        "bytes",
    )

    with ZipFile(
        zip_path,
        "r",
    ) as archive:

        xml_files = [
            info
            for info in archive.infolist()
            if (
                not info.is_dir()
                and info.filename.lower().endswith(
                    ".xml"
                )
            )
        ]

        print()
        print(
            "XML-файлов:",
            len(xml_files),
        )

        if not xml_files:

            raise RuntimeError(
                "В ZIP нет XML-файлов."
            )

        print()
        print(
            "ПЕРВЫЕ XML-ФАЙЛЫ"
        )
        print()

        for info in (
            xml_files[
                :args.list_files
            ]
        ):

            print(
                info.filename
            )

            print(
                "    compressed:",
                info.compress_size,
            )

            print(
                "    uncompressed:",
                info.file_size,
            )

        inspect_xml_root(
            archive=archive,
            xml_file_info=(
                xml_files[0]
            ),
        )

        result = find_documents(
            archive=archive,
            xml_files=xml_files,
            max_documents=(
                args.documents
            ),
        )

        print()
        print(
            "=========================================="
        )
        print(
            "РЕЗУЛЬТАТ ПОИСКА"
        )
        print(
            "=========================================="
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

            print()
            print(
                "Тег <Документ> не найден."
            )

            print(
                "Нужно посмотреть "
                "фактическую структуру XML."
            )

        else:

            for number, item in enumerate(
                result[
                    "documents"
                ],
                start=1,
            ):

                print_document(
                    number=number,
                    item=item,
                )

    inspect_xsd(
        args.xsd
    )

    print()
    print(
        "=========================================="
    )
    print(
        "ГОТОВО"
    )
    print(
        "=========================================="
    )
    print()


if __name__ == "__main__":
    main()