VALID_CHECK_RESULTS = {
    "found",
    "not_found",
    "not_applicable",
    "unavailable",
}


def build_check_result(
    *,
    checked,
    applicable,
    result,
    data_date,
    dataset_code,
    source,
    reason=None,
    **extra,
):
    """
    Единый контракт результата проверки источника.

    found:
        источник применим, проверка выполнена,
        сведения найдены.

    not_found:
        источник применим, проверка выполнена,
        сведения в актуальном наборе не найдены.

    not_applicable:
        источник не применяется к данному типу сущности.

    unavailable:
        проверку нельзя корректно выполнить сейчас,
        например dataset ещё не загружен.
    """

    if result not in VALID_CHECK_RESULTS:
        raise ValueError(
            f"Неизвестный result проверки: {result}"
        )

    if result in {"found", "not_found"}:
        if checked is not True or applicable is not True:
            raise ValueError(
                "found/not_found требуют "
                "checked=True и applicable=True"
            )

    if result == "not_applicable":
        if checked is not True or applicable is not False:
            raise ValueError(
                "not_applicable требует "
                "checked=True и applicable=False"
            )

    if result == "unavailable":
        if checked is not False:
            raise ValueError(
                "unavailable требует checked=False"
            )

    payload = {
        "checked": checked,
        "applicable": applicable,
        "result": result,
        "data_date": data_date,
        "dataset_code": dataset_code,
        "source": source,
        "reason": reason,
    }

    payload.update(extra)

    return payload
