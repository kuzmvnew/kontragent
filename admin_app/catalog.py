"""Confirmed catalog metadata layered over operational PostgreSQL state."""

from __future__ import annotations

from urllib.parse import urlencode

# These are publisher/registry pages already recorded by the source registry
# services and passports.  They intentionally do not point at API methods or
# short-lived artifacts.
OFFICIAL_PAGE_URLS = {
    "fns": "https://www.nalog.gov.ru/",
    "girbo": "https://bo.nalog.gov.ru/",
    "fssp": "https://fssp.gov.ru/",
    "fedresurs": "https://fedresurs.ru/",
    "eis": "https://zakupki.gov.ru/",
    "dadata": "https://dadata.ru/",
    "cbr": "https://www.cbr.ru/",
    "genproc": "https://proverki.gov.ru/",
    "roszdravnadzor": "https://roszdravnadzor.gov.ru/",
    "mintrans": "https://mintrans.gov.ru/",
    "firmoteka": "https://firmoteka.ru/",
    "nostroy": "https://reestr.nostroy.ru/",
    "nopriz": "https://reestr.nopriz.ru/",
    "prime_disclosure": "https://disclosure.1prime.ru/",
    "roskomnadzor": "https://rkn.gov.ru/",
    "checko": "https://checko.ru/",
    "moscow_courts_official": "https://mos-gorsud.ru/",
}


# Stable access pages derived from code contracts where DataSet.source_url is
# absent, generic or becomes a short-lived artifact URL at discovery time.
DATA_ACCESS_URLS = {
    "mintrans_ted_registry": "https://www.mintrans.gov.ru/search?" + urlencode({
        "search_type": 2,
        "check_name": 1,
        "value": "транспортно-экспедиционной деятельности",
    }),
}


def access_kind(*, update_mode: str, data_format: str) -> str:
    if update_mode == "api":
        return "API / запрос реестра"
    if data_format in {"xlsx", "csv", "xml", "xml_zip", "zip_xml", "xml_bundle"}:
        return "Файл / страница выгрузки"
    if data_format == "html" or update_mode in {"catalog_crawl", "on_demand"}:
        return "Поиск / веб-реестр"
    return "Канал получения данных"
