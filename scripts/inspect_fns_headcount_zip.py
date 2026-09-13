import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile

from app.ingestion.fns_headcount import (
    extract_document,
    local_name,
)


def print_element(
    element,
    depth=0,
    max_depth=3,
):
    indent = "    " * depth

    print(
        f"{indent}<{local_name(element.tag)}>"
    )

    if element.attrib:
        for key, value in element.attrib.items():
            print(
                f"{indent}    @{key} = {value}"
            )

    if depth >= max_depth:
        return

    for child in element:
        print_element(
            child,
            depth=depth + 1,
            max_depth=max_depth,
        )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Проверка структуры "
            "ZIP/XML ФНС без импорта"
        )
    )

    parser.add_argument(
        "zip_path",
        help="Путь к ZIP ФНС",
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
        "FNS HEADCOUNT ZIP INSPECTOR"
    )
    print(
        "=========================================="
    )
    print()

    print(
        "ZIP:",
        path,
    )

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
                "XML-файлы не найдены"
            )

        print()
        print(
            "Первые XML:"
        )

        for info in xml_files[:5]:
            print(
                " -",
                info.filename,
                "|",
                info.file_size,
                "bytes",
            )

        first_file = (
            xml_files[0]
        )

        print()
        print(
            "=========================================="
        )
        print(
            "ПРОВЕРЯЕМ:"
        )
        print(
            first_file.filename
        )
        print(
            "=========================================="
        )
        print()

        with archive.open(
            first_file,
            "r",
        ) as xml_file:

            tree = ET.parse(
                xml_file
            )

        root = tree.getroot()

        print(
            "Корневой тег:",
            local_name(
                root.tag
            ),
        )

        document = None

        for element in root.iter():

            if (
                local_name(
                    element.tag
                )
                == "Документ"
            ):
                document = element
                break

        if document is None:

            print()
            print(
                "ОШИБКА:"
            )
            print(
                "Тег <Документ> "
                "не найден."
            )

            print()
            print(
                "Структура XML:"
            )

            print_element(
                root,
                max_depth=4,
            )

            return

        print()
        print(
            "Первый <Документ>:"
        )
        print()

        print_element(
            document,
            max_depth=4,
        )

        print()
        print(
            "=========================================="
        )
        print(
            "РЕЗУЛЬТАТ НАШЕГО PARSER"
        )
        print(
            "=========================================="
        )
        print()

        result = (
            extract_document(
                document
            )
        )

        print(
            result
        )

        print()

        if result is None:

            print(
                "PARSER НЕ РАСПОЗНАЛ ЗАПИСЬ."
            )
            print(
                "Полный импорт пока "
                "НЕ запускаем."
            )

        else:

            print(
                "PARSER OK"
            )

            print()
            print(
                "ИНН:",
                result.get(
                    "inn"
                ),
            )

            print(
                "Сотрудников:",
                result.get(
                    "employee_count"
                ),
            )

            print(
                "ID документа:",
                result.get(
                    "document_id"
                ),
            )

            print(
                "Дата документа:",
                result.get(
                    "document_date"
                ),
            )


if __name__ == "__main__":
    main()