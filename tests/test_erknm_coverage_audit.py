from scripts.audit_erknm_coverage import format_number, percentage


def test_percentage_handles_zero_denominator():
    assert percentage(10, 0) == 0.0


def test_percentage_rounds_to_two_decimals():
    assert percentage(1, 3) == 33.33


def test_format_number_uses_spaces():
    assert format_number(172417) == "172 417"
