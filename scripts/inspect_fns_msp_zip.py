import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from zipfile import ZipFile


def local_name(tag):
    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def print_element(
    element,
    depth=0,
    max_depth=6,
):
    indent = "    " * depth

    print(
        f"{indent}<{local_name(element.tag)}>"
    )

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


def inspect_xml_file(
    archive,
    info,
    max_documents=3,
):
    print()
    print(
        "=========================================="
    )
    print(
        "XML-ФАЙЛ:"
    )
    print(
        info.filename
    )
    print(
        "=========================================="
    )
    print()

    documents_found = 0

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
            print(
                "XML пустой."
            )
            return

        print(
            "Корневой тег:",
            local_name(
                root.tag
            ),
        )

        print()

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

            documents_found += 1

            print(
                "------------------------------------------"
            )
            print(
                "ДОКУМЕНТ",
                documents_found,
            )
            print(
                "------------------------------------------"
            )

            print_element(
                element,
                max_depth=6,
            )

            print()

            if (
                documents_found
                >= max_documents
            ):
                break

            element.clear()

        if documents_found == 0:

            print(
                "Тег <Документ> "
                "в этом XML не найден."
            )

            print()
            print(
                "Показываю начало структуры:"
            )
            print()

            with archive.open(
                info,
                "r",
            ) as retry_file:

                tree = ET.parse(
                    retry_file
                )

            print_element(
                tree.getroot(),
                max_depth=5,
            )


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Инспектор официального "
            "ZIP/XML Реестра МСП ФНС"
        )
    )

    parser.add_argument(
        "zip_path",
        help=(
            "Путь к ZIP-файлу "
            "Реестра МСП"
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
        "FNS MSP ZIP INSPECTOR"
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
                "В ZIP отсутствуют XML-файлы"
            )

        print()
        print(
            "Первые XML:"
        )

        for info in xml_files[:10]:

            print(
                " -",
                info.filename,
                "|",
                info.file_size,
                "bytes",
            )

        inspect_xml_file(
            archive=archive,
            info=xml_files[0],
            max_documents=3,
        )


if __name__ == "__main__":
    main()