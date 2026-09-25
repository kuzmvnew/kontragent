from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.ingestion import exact_source_workers as worker
from app.worker.errors import AccessRequiredError, LegalBlockError, WorkerNetworkError


NOW = datetime(2026, 9, 25, 10, tzinfo=timezone.utc)


def test_each_source_has_an_independent_worker_source_id_and_lease_namespace():
    assert set(worker.SPECS) == {
        "fns_npd",
        "nostroy_sro_members_on_demand",
        "nopriz_sro_members_on_demand",
        "prime_corporate_disclosure",
        "rkn_personal_data_operators",
    }
    assert len(set(worker.SPECS)) == 5


def test_http_protection_and_transient_failures_are_not_clean_negatives():
    with pytest.raises(LegalBlockError):
        worker._validate_response_status(
            "nostroy_sro_members_on_demand", SimpleNamespace(status_code=403)
        )
    with pytest.raises(WorkerNetworkError):
        worker._validate_response_status(
            "fns_npd", SimpleNamespace(status_code=429)
        )


def test_eis_fails_closed_without_token(monkeypatch):
    monkeypatch.delenv("EIS_IP_TOKEN", raising=False)
    with pytest.raises(AccessRequiredError, match="EIS_IP_TOKEN"):
        worker.schedule_eis_rnp_check(
            SimpleNamespace(), raw_root=__import__("pathlib").Path("/tmp"), now=NOW
        )
