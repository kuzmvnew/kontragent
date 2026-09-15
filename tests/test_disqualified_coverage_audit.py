from datetime import date

import pytest

from scripts.audit_disqualified_coverage import (
    parse_data_date,
    percentage,
)


def test_percentage_handles_normal_values():
    assert percentage(25, 100) == 25.0
    assert percentage(1, 3) == 33.33


def test_percentage_handles_zero_total():
    assert percentage(10, 0) == 0.0


def test_parse_data_date():
    assert parse_data_date("2026-09-13") == date(2026, 9, 13)


def test_parse_data_date_rejects_invalid_value():
    with pytest.raises(Exception):
        parse_data_date("13.09.2026")
