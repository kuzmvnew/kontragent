from app.services import company_service


def test_existing_company_does_not_construct_external_provider(monkeypatch):
    monkeypatch.setattr(
        company_service,
        "get_company_from_database",
        lambda inn: {"inn": inn, "name": "Existing Master company"},
    )
    monkeypatch.setattr(
        company_service,
        "_external_provider",
        lambda: (_ for _ in ()).throw(
            AssertionError("external provider must not be constructed for a local hit")
        ),
    )

    company = company_service.get_company_by_inn("7707083893")

    assert company["source"] == "database"
    assert company["inn"] == "7707083893"
