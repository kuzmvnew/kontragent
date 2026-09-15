from scripts import accept_phase3_tax_sources


def test_phase3_tax_acceptance_script_imports():
    assert callable(
        accept_phase3_tax_sources.main
    )
