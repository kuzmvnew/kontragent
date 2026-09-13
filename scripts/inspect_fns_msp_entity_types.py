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
    max_depth=8,
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


def detect_entity_type(document):
    """
    Определяем тип сущности
    не по имени XML-тега,
    а по реальным атрибутам.

    ИННЮЛ -> юридическое лицо
    ИННФЛ -> ИП
    """

    has_legal = False
    has_ip = False

    for element in document.iter():

        if "ИННЮЛ" in element.attrib:
            has_legal = True

        if "ИННФЛ" in element.attrib:
            has_ip = True

    if has_legal:
        return "legal"

    if has_ip:
        return "individual_entrepreneur"

    return None


def document_summary(
    document,
):
    result = {}

    result[
        "document_id"
    ] = document.attrib.get(
        "ИдДок"
    )

    result[
        "data_date"
    ] = document.attrib.get(
        "ДатаСост"
    )

    result[
        "msp_inclusion_date"
    ] = document.attrib.get(
        "ДатаВклМСП"
    )

    result[
        "msp_subject_type"
    ] = document.attrib.get(
        "ВидСубМСП"
    )

    result[
        "msp_category"
    ] = document.attrib.get(
        "КатСубМСП"
    )

    result[
        "is_new_msp"
    ] = document.attrib.get(
        "ПризНовМСП"
    )

    result[
        "social_enterprise"
    ] = document.attrib.get(
        "СведСоцПред"
    )

    for element in document.iter():

        if "ИННЮЛ" in element.attrib:
            result[
                "inn"
            ] = element.attrib.get(
                "ИННЮЛ"
            )

        if "ОГРН" in element.attrib:
            result[
                "ogrn"
            ] = element.attrib.get(
                "ОГРН"
            )

        if "ИННФЛ" in element.attrib:
            result[
                "inn"
            ] = element.attrib.get(
                "ИННФЛ"
            )

        if "ОГРНИП" in element.attrib:
            result[
                "ogrn"
            ] = element.attrib.get(
                "ОГРНИП"
            )

        if "КодРегион" in element.attrib:
            result[
                "region_code"
            ] = element.attrib.get(
                "КодРегион"
            )

        if (
            local_name(
                element.tag
            )
            == "СвОКВЭДОсн"
        ):
            result[
                "okved"
            ] = element.attrib.get(
                "КодОКВЭД"
            )

            result[
                "activity"
            ] = element.attrib.get(
                "НаимОКВЭД"
            )

        if (
            local_name(
                element.tag
            )
            == "СведССЧР"
        ):
            result[
                "employee_count"
            ] = element.attrib.get(
                "КолРаб"
            )

    return result


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Поиск примеров ЮЛ и ИП "
            "в Реестре МСП ФНС"
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

    legal_document = None
    legal_file = None

    ip_document = None
    ip_file = None

    documents_scanned = 0
    files_scanned = 0

    print()
    print(
        "=========================================="
    )
    print(
        "FNS MSP ENTITY TYPE INSPECTOR"
    )
    print(
        "=========================================="
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

        print()
        print(
            "Ищем первый пример ЮЛ "
            "и первый пример ИП..."
        )
        print()

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

                    entity_type = (
                        detect_entity_type(
                            element
                        )
                    )

                    if (
                        entity_type
                        == "legal"
                        and legal_document
                        is None
                    ):
                        legal_document = (
                            ET.fromstring(
                                ET.tostring(
                                    element,
                                    encoding="utf-8",
                                )
                            )
                        )

                        legal_file = (
                            info.filename
                        )

                    if (
                        entity_type
                        == (
                            "individual_entrepreneur"
                        )
                        and ip_document
                        is None
                    ):
                        ip_document = (
                            ET.fromstring(
                                ET.tostring(
                                    element,
                                    encoding="utf-8",
                                )
                            )
                        )

                        ip_file = (
                            info.filename
                        )

                    element.clear()
                    root.clear()

                    if (
                        legal_document
                        is not None
                        and ip_document
                        is not None
                    ):
                        break

            if (
                legal_document
                is not None
                and ip_document
                is not None
            ):
                break

    print(
        "Просмотрено XML-файлов:",
        files_scanned,
    )

    print(
        "Просмотрено документов:",
        documents_scanned,
    )

    # =====================================================
    # LEGAL
    # =====================================================

    print()
    print(
        "=========================================="
    )
    print(
        "ЮРИДИЧЕСКОЕ ЛИЦО"
    )
    print(
        "=========================================="
    )
    print()

    if legal_document is None:

        print(
            "Пример ЮЛ не найден."
        )

    else:

        print(
            "XML:",
            legal_file,
        )

        print()
        print(
            "Нормализованная сводка:"
        )

        print(
            document_summary(
                legal_document
            )
        )

        print()
        print(
            "Полная структура документа:"
        )
        print()

        print_element(
            legal_document,
            max_depth=8,
        )

    # =====================================================
    # IP
    # =====================================================

    print()
    print(
        "=========================================="
    )
    print(
        "ИНДИВИДУАЛЬНЫЙ ПРЕДПРИНИМАТЕЛЬ"
    )
    print(
        "=========================================="
    )
    print()

    if ip_document is None:

        print(
            "Пример ИП не найден."
        )

    else:

        print(
            "XML:",
            ip_file,
        )

        print()
        print(
            "Нормализованная сводка:"
        )

        print(
            document_summary(
                ip_document
            )
        )

        print()
        print(
            "Полная структура документа:"
        )
        print()

        print_element(
            ip_document,
            max_depth=8,
        )


if __name__ == "__main__":
    main()