from datetime import datetime


LEGAL_DATASET_CODE = "fns_snr"
IP_DATASET_CODE = "fns_snrip"


REGIME_NAMES = {
    "usn": "Упрощенная система налогообложения (УСН)",
    "ausn": (
        "Автоматизированная упрощенная "
        "система налогообложения (АУСН)"
    ),
    "eshn": (
        "Единый сельскохозяйственный налог (ЕСХН)"
    ),
    "srp": (
        "Система налогообложения при выполнении "
        "соглашения о разделе продукции (СРП)"
    ),
    "psn": "Патентная система налогообложения (ПСН)",
    "npd": "Налог на профессиональный доход (НПД)",
}


LEGAL_FLAG_TO_REGIME = {
    "ПризнУСН": "usn",
    "ПризнАУСН": "ausn",
    "ПризнЕСХН": "eshn",
    "ПризнСРП": "srp",
}


IP_CODE_TO_REGIME = {
    "1": "usn",
    "2": "ausn",
    "3": "eshn",
    "4": "psn",
    "5": "npd",
}


def local_name(tag):
    if "}" in tag:
        return tag.rsplit(
            "}",
            1,
        )[-1]

    return tag


def parse_fns_date(value):
    if not value:
        return None

    for date_format in (
        "%d.%m.%Y",
        "%Y-%m-%d",
    ):
        try:
            return datetime.strptime(
                value,
                date_format,
            ).date()

        except ValueError:
            pass

    return None


def deduplicate(values):
    result = []

    for value in values:
        if value not in result:
            result.append(value)

    return result


def parse_legal_document(document):
    """
    Специальные налоговые режимы ЮЛ.

    ФНС публикует отдельные флаги 0/1:
    УСН, АУСН, ЕСХН и СРП.
    """

    taxpayer = None
    regime_element = None

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        if (
            tag == "СведНП"
            and taxpayer is None
        ):
            taxpayer = element

        elif (
            tag == "СведСНР"
            and regime_element is None
        ):
            regime_element = element

    if (
        taxpayer is None
        or regime_element is None
    ):
        return None

    inn = taxpayer.attrib.get(
        "ИННЮЛ"
    )

    if not inn:
        return None

    inn = str(inn).strip()

    if (
        len(inn) != 10
        or not inn.isdigit()
    ):
        return None

    data_date = parse_fns_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    if data_date is None:
        return None

    regime_codes = []
    unknown_codes = []

    for (
        attribute_name,
        regime_code,
    ) in LEGAL_FLAG_TO_REGIME.items():

        value = regime_element.attrib.get(
            attribute_name
        )

        if value == "1":
            regime_codes.append(
                regime_code
            )

        elif value not in {
            None,
            "0",
        }:
            unknown_codes.append(
                f"{attribute_name}={value}"
            )

    return {
        "inn": inn,
        "entity_type": "legal",
        "dataset_code": LEGAL_DATASET_CODE,
        "data_date": data_date,
        "source_document_date": (
            parse_fns_date(
                document.attrib.get(
                    "ДатаДок"
                )
            )
        ),
        "source_document_id": (
            document.attrib.get(
                "ИдДок"
            )
        ),
        "regime_codes": (
            deduplicate(
                regime_codes
            )
        ),
        "unknown_codes": (
            deduplicate(
                unknown_codes
            )
        ),
    }


def parse_ip_document(document):
    """
    Специальные налоговые режимы ИП.

    Один документ может содержать
    несколько элементов СведСНР.

    Коды ФНС:
    1 — УСН
    2 — АУСН
    3 — ЕСХН
    4 — ПСН
    5 — НПД
    """

    taxpayer = None
    regime_elements = []

    for element in document.iter():

        tag = local_name(
            element.tag
        )

        if (
            tag == "СведНП"
            and taxpayer is None
        ):
            taxpayer = element

        elif tag == "СведСНР":
            regime_elements.append(
                element
            )

    if taxpayer is None:
        return None

    inn = taxpayer.attrib.get(
        "ИННФЛ"
    )

    if not inn:
        return None

    inn = str(inn).strip()

    if (
        len(inn) != 12
        or not inn.isdigit()
    ):
        return None

    data_date = parse_fns_date(
        document.attrib.get(
            "ДатаСост"
        )
    )

    if data_date is None:
        return None

    regime_codes = []
    unknown_codes = []

    for element in regime_elements:

        source_code = element.attrib.get(
            "ПризнСНР"
        )

        if source_code is None:
            continue

        source_code = str(
            source_code
        ).strip()

        regime_code = (
            IP_CODE_TO_REGIME.get(
                source_code
            )
        )

        if regime_code is None:
            unknown_codes.append(
                source_code
            )
        else:
            regime_codes.append(
                regime_code
            )

    return {
        "inn": inn,
        "ogrn": (
            taxpayer.attrib.get(
                "ОГРНИП"
            )
        ),
        "entity_type": (
            "individual_entrepreneur"
        ),
        "dataset_code": IP_DATASET_CODE,
        "data_date": data_date,
        "source_document_date": (
            parse_fns_date(
                document.attrib.get(
                    "ДатаДок"
                )
            )
        ),
        "source_document_id": (
            document.attrib.get(
                "ИдДок"
            )
        ),
        "regime_codes": (
            deduplicate(
                regime_codes
            )
        ),
        "unknown_codes": (
            deduplicate(
                unknown_codes
            )
        ),
    }


def get_regime_name(regime_code):
    if regime_code is None:
        return None

    return REGIME_NAMES.get(
        str(regime_code).strip()
    )
