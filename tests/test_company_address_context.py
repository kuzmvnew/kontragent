from app.services.company_address_context_service import building_level_address, normalize_legal_address


def test_address_normalization_is_deterministic():
    assert normalize_legal_address('г. Москва, ул. Тестовая, д. 1, офис 5') == "Г МОСКВА УЛ ТЕСТОВАЯ Д 1 ОФИС 5"


def test_building_level_does_not_make_negative_inference():
    assert building_level_address('г. Москва, ул. Тестовая, д. 1, офис 5') == "Г МОСКВА УЛ ТЕСТОВАЯ Д 1"
