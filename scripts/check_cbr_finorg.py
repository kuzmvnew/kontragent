import argparse
import json

from app.services.cbr_finorg_service import (
    refresh_cbr_finorg_check_for_inn,
)


def parse_arguments():
    parser = argparse.ArgumentParser(
        description=(
            "Точечная проверка ИНН через официальный веб-сервис "
            "Банка России «Участники финансового рынка»"
        )
    )
    parser.add_argument(
        "--inn",
        required=True,
        help="ИНН ЮЛ (10 цифр) или ИП (12 цифр)",
    )
    return parser.parse_args()


def main():
    args = parse_arguments()

    result = refresh_cbr_finorg_check_for_inn(
        args.inn
    )

    print(
        json.dumps(
            result,
            ensure_ascii=False,
            indent=2,
            default=str,
        )
    )


if __name__ == "__main__":
    main()
