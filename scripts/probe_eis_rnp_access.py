import argparse
import sys
from datetime import date
from pathlib import Path

from app.providers.eis_rnp_provider import (
    EisCredentialError,
    build_rnp_by_region_date_request,
    build_rnp_by_registry_number_request,
    download_archive,
    get_eis_ip_token,
    request_archive,
)


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError(
            "Дата должна быть YYYY-MM-DD"
        ) from error


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Live probe официального сервиса ЕИС "
            "для РНП по 44-ФЗ. Token не печатается."
        )
    )

    mode = parser.add_mutually_exclusive_group(
        required=False,
    )

    mode.add_argument(
        "--registry-number",
        help=(
            "Проверить один реестровый номер РНП"
        ),
    )

    mode.add_argument(
        "--region",
        help=(
            "Код региона для запроса архива по дате"
        ),
    )

    parser.add_argument(
        "--date",
        type=parse_date,
        help=(
            "Дата для --region в формате YYYY-MM-DD"
        ),
    )

    parser.add_argument(
        "--output",
        default=(
            "data/eis/rnp/probe.zip"
        ),
        help=(
            "Куда сохранить архив при успешном ответе"
        ),
    )

    return parser.parse_args()


def main():
    args = parse_arguments()

    if (
        args.region
        and args.date is None
    ):
        print(
            "Для --region обязательно укажи --date."
        )
        sys.exit(2)

    if (
        args.date is not None
        and not args.region
    ):
        print(
            "--date используется только вместе с --region."
        )
        sys.exit(2)

    try:
        token = get_eis_ip_token()
    except EisCredentialError as error:
        print(
            "ДОСТУП ЕИС ЕЩЁ НЕ НАСТРОЕН"
        )
        print(error)
        print()
        print(
            "Token должен храниться только локально "
            "в переменной EIS_IP_TOKEN или .env."
        )
        sys.exit(2)

    print(
        "=" * 72
    )
    print(
        "ЕИС — РНП 44-ФЗ — LIVE ACCESS PROBE"
    )
    print(
        "=" * 72
    )
    print(
        "Credential: найден локально, значение не выводится"
    )

    if args.registry_number:
        print(
            "Режим: по реестровому номеру"
        )
        print(
            "Реестровый номер:",
            args.registry_number,
        )

        soap_xml = (
            build_rnp_by_registry_number_request(
                token=token,
                registry_number=(
                    args.registry_number
                ),
            )
        )

    else:
        region = (
            args.region
            or "77"
        )
        exact_date = (
            args.date
            or date(2025, 1, 20)
        )

        print(
            "Режим: регион + дата"
        )
        print(
            "Регион:",
            region,
        )
        print(
            "Дата:",
            exact_date,
        )

        soap_xml = (
            build_rnp_by_region_date_request(
                token=token,
                region_code=region,
                exact_date=exact_date,
            )
        )

    print()
    print(
        "Отправляем один запрос в официальный getDocsIP..."
    )

    result = request_archive(
        soap_xml=soap_xml,
        token=token,
    )

    print()
    print(
        "HTTP status:",
        result.get("http_status"),
    )
    print(
        "EIS status:",
        result.get("status"),
    )

    if result.get("status") == "error":
        print(
            "Error code:",
            result.get("error_code"),
        )
        print(
            "Error message:",
            result.get("error_message"),
        )
        sys.exit(1)

    if result.get("status") == "no_data":
        print(
            "ЕИС корректно ответил, но по этому запросу данных нет."
        )
        return

    if result.get("status") != "archive":
        print(
            "Ответ ЕИС получен, но archiveUrl не найден."
        )
        print(
            "Нужно сохранить/проинспектировать фактический SOAP response "
            "без публикации token."
        )
        sys.exit(1)

    destination = Path(
        args.output
    )

    print(
        "Archive URL: получен"
    )
    print(
        "Скачиваем архив..."
    )

    archive = download_archive(
        archive_url=(
            result["archive_url"]
        ),
        token=token,
        destination=destination,
    )

    print()
    print(
        "АРХИВ ПОЛУЧЕН"
    )
    print(
        "Файл:",
        archive["path"],
    )
    print(
        "Размер:",
        archive["size"],
    )
    print(
        "SHA-256:",
        archive["sha256"],
    )


if __name__ == "__main__":
    main()
