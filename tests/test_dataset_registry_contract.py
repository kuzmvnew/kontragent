from datetime import date, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from app.contracts.dataset_registry import (
    AccessCost,
    AccessMethod,
    DatasetRegistryEntry,
    DatasetSnapshot,
    DatasetStatus,
    EntityKind,
    PermissionDecision,
    PublicationReadiness,
    RefreshCadence,
    SnapshotStatus,
    SourceAuthority,
    UsagePermission,
)


NOW = datetime(
    2026,
    9,
    14,
    12,
    tzinfo=timezone.utc,
)

CHECKSUM = "a" * 64


def reviewed_permissions(
    **changes,
):
    values = {
        "storage": PermissionDecision.ALLOWED,
        "public_display": PermissionDecision.ALLOWED,
        "commercial_use": PermissionDecision.ALLOWED,
        "redistribution": PermissionDecision.RESTRICTED,
        "reviewed_on": date(2026, 9, 14),
        "reviewed_by": "project-owner",
        "basis_url": "https://example.org/test-terms",
    }

    values.update(changes)

    return UsagePermission(
        **values,
    )


def available_snapshot(
    **changes,
):
    values = {
        "snapshot_id": "test-snapshot-1",
        "dataset_code": "test-dataset",
        "status": SnapshotStatus.AVAILABLE,
        "retrieved_at": NOW,
        "source_data_date": date(2026, 9, 1),
        "version_label": "test-version",
        "checksum_sha256": CHECKSUM,
        "source_records": 100,
        "imported_records": 70,
        "unmatched_records": 25,
        "rejected_records": 5,
    }

    values.update(changes)

    return DatasetSnapshot(
        **values,
    )


def make_entry(
    **changes,
):
    values = {
        "dataset_code": "test-dataset",
        "source_code": "test-source",
        "title": "Вымышленный набор данных",
        "description": (
            "Только проверка структуры "
            "реестра источников."
        ),
        "owner_name": "Вымышленный владелец",
        "authority": SourceAuthority.OFFICIAL,
        "source_url": (
            "https://example.org/test-source"
        ),
        "terms_url": (
            "https://example.org/test-terms"
        ),
        "access_methods": (
            AccessMethod.BULK_FILE,
        ),
        "access_cost": AccessCost.FREE,
        "entity_kinds": (
            EntityKind.LEGAL_ENTITY,
            EntityKind.INDIVIDUAL_ENTREPRENEUR,
        ),
        "refresh_cadence": (
            RefreshCadence.MONTHLY
        ),
        "requires_auth": False,
        "contains_personal_data": False,
        "status": DatasetStatus.ACTIVE,
        "usage_permission": (
            reviewed_permissions()
        ),
        "current_snapshot": (
            available_snapshot()
        ),
        "registered_at": (
            NOW - timedelta(days=10)
        ),
        "updated_at": (
            NOW + timedelta(minutes=1)
        ),
    }

    values.update(changes)

    return DatasetRegistryEntry(
        **values,
    )


def test_active_free_official_dataset_can_be_prioritized():
    entry = make_entry()

    assert entry.is_free_ingestion_candidate

    assert (
        entry.publication_readiness
        == PublicationReadiness.APPROVED
    )

    assert not entry.can_export_data


def test_redistribution_requires_separate_permission():
    entry = make_entry(
        usage_permission=reviewed_permissions(
            redistribution=PermissionDecision.ALLOWED,
        )
    )

    assert entry.can_export_data


def test_unknown_rights_require_review_and_block_free_candidate():
    entry = make_entry(
        status=DatasetStatus.RESEARCH,
        current_snapshot=None,
        usage_permission=UsagePermission(),
    )

    assert (
        entry.publication_readiness
        == PublicationReadiness.REVIEW_REQUIRED
    )

    assert not entry.is_free_ingestion_candidate
    assert not entry.can_export_data


@pytest.mark.parametrize(
    "field",
    [
        "storage",
        "public_display",
        "commercial_use",
    ],
)
def test_prohibition_blocks_publication(
    field,
):
    entry = make_entry(
        usage_permission=reviewed_permissions(
            **{
                field: (
                    PermissionDecision.PROHIBITED
                )
            }
        )
    )

    assert (
        entry.publication_readiness
        == PublicationReadiness.BLOCKED
    )


@pytest.mark.parametrize(
    "status",
    [
        DatasetStatus.BLOCKED,
        DatasetStatus.RETIRED,
    ],
)
def test_blocked_or_retired_dataset_cannot_be_published(
    status,
):
    entry = make_entry(
        status=status,
    )

    assert (
        entry.publication_readiness
        == PublicationReadiness.BLOCKED
    )

    assert not entry.can_export_data


def test_failed_current_refresh_is_not_publication_approval():
    failed = DatasetSnapshot(
        snapshot_id="failed-current",
        dataset_code="test-dataset",
        status=SnapshotStatus.FAILED,
        retrieved_at=NOW,
        failure_reason="download_failed",
    )

    entry = make_entry(
        status=DatasetStatus.DEGRADED,
        current_snapshot=failed,
    )

    assert (
        entry.publication_readiness
        == PublicationReadiness.REVIEW_REQUIRED
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"reviewed_on": None},
        {"reviewed_by": None},
        {"basis_url": None},
    ],
)
def test_known_permission_requires_review_provenance(
    changes,
):
    values = reviewed_permissions().model_dump(
        round_trip=True,
    )

    values.update(changes)

    with pytest.raises(
        ValidationError,
    ):
        UsagePermission.model_validate(
            values,
        )


@pytest.mark.parametrize(
    "changes",
    [
        {
            "reviewed_on": date(
                2026,
                9,
                14,
            )
        },
        {
            "reviewed_by": "somebody"
        },
        {
            "basis_url": (
                "https://example.org/terms"
            )
        },
    ],
)
def test_unknown_permission_cannot_look_reviewed(
    changes,
):
    with pytest.raises(
        ValidationError,
    ):
        UsagePermission(
            **changes,
        )


def test_available_snapshot_preserves_import_accounting():
    snapshot = available_snapshot()

    assert snapshot.source_records == (
        snapshot.imported_records
        + snapshot.unmatched_records
        + snapshot.rejected_records
    )


@pytest.mark.parametrize(
    "changes",
    [
        {"source_data_date": None},
        {"checksum_sha256": None},
        {"source_records": None},
        {"imported_records": None},
        {"unmatched_records": None},
        {"rejected_records": None},
        {"source_records": 101},
        {
            "checksum_sha256": (
                "not-a-sha256"
            )
        },
        {"failure_reason": "unexpected"},
    ],
)
def test_available_snapshot_rejects_incomplete_metadata(
    changes,
):
    with pytest.raises(
        ValidationError,
    ):
        available_snapshot(
            **changes,
        )


def test_failed_snapshot_requires_reason_and_has_no_final_counts():
    snapshot = DatasetSnapshot(
        snapshot_id="failed-1",
        dataset_code="test-dataset",
        status=SnapshotStatus.FAILED,
        retrieved_at=NOW,
        failure_reason="download_failed",
    )

    assert (
        snapshot.failure_reason
        == "download_failed"
    )

    with pytest.raises(
        ValidationError,
    ):
        DatasetSnapshot(
            snapshot_id="failed-2",
            dataset_code="test-dataset",
            status=SnapshotStatus.FAILED,
            retrieved_at=NOW,
        )

    with pytest.raises(
        ValidationError,
    ):
        DatasetSnapshot(
            snapshot_id="failed-3",
            dataset_code="test-dataset",
            status=SnapshotStatus.FAILED,
            retrieved_at=NOW,
            failure_reason="download_failed",
            source_records=100,
        )


def test_snapshot_requires_timezone():
    with pytest.raises(
        ValidationError,
        match="часовой пояс",
    ):
        available_snapshot(
            retrieved_at=datetime(
                2026,
                9,
                14,
                12,
            )
        )


def test_entry_rejects_snapshot_of_another_dataset():
    with pytest.raises(
        ValidationError,
        match="другому dataset",
    ):
        make_entry(
            current_snapshot=available_snapshot(
                dataset_code="another",
            )
        )


def test_entry_rejects_future_snapshot():
    with pytest.raises(
        ValidationError,
        match="новее записи",
    ):
        make_entry(
            current_snapshot=available_snapshot(
                retrieved_at=(
                    NOW + timedelta(days=1)
                )
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "access_methods",
        "entity_kinds",
    ],
)
def test_entry_rejects_duplicate_classification(
    field,
):
    if field == "access_methods":
        value = (
            AccessMethod.API,
            AccessMethod.API,
        )
    else:
        value = (
            EntityKind.LEGAL_ENTITY,
            EntityKind.LEGAL_ENTITY,
        )

    with pytest.raises(
        ValidationError,
        match="дубли",
    ):
        make_entry(
            **{
                field: value
            }
        )


def test_active_dataset_requires_available_snapshot():
    with pytest.raises(
        ValidationError,
    ):
        make_entry(
            current_snapshot=None,
        )

    with pytest.raises(
        ValidationError,
    ):
        make_entry(
            current_snapshot=available_snapshot(
                status=SnapshotStatus.STALE,
            )
        )


@pytest.mark.parametrize(
    "status",
    [
        SnapshotStatus.AVAILABLE,
        SnapshotStatus.STALE,
    ],
)
def test_degraded_dataset_allows_available_or_stale_snapshot(
    status,
):
    entry = make_entry(
        status=DatasetStatus.DEGRADED,
        current_snapshot=available_snapshot(
            status=status,
        ),
    )

    assert (
        entry.status
        == DatasetStatus.DEGRADED
    )


def test_updated_at_cannot_precede_registration():
    with pytest.raises(
        ValidationError,
    ):
        make_entry(
            updated_at=(
                NOW - timedelta(days=20)
            )
        )


@pytest.mark.parametrize(
    "field",
    [
        "requires_auth",
        "contains_personal_data",
    ],
)
@pytest.mark.parametrize(
    "value",
    [
        "true",
        "false",
        0,
        1,
    ],
)
def test_boolean_flags_are_strict(
    field,
    value,
):
    with pytest.raises(
        ValidationError,
    ):
        make_entry(
            **{
                field: value
            }
        )


def test_paid_or_manual_source_is_not_free_ingestion_candidate():
    paid = make_entry(
        access_cost=AccessCost.PAID,
    )

    manual = make_entry(
        access_methods=(
            AccessMethod.WEB_INTERFACE,
        ),
    )

    assert not paid.is_free_ingestion_candidate
    assert not manual.is_free_ingestion_candidate


def test_registry_entry_survives_json_round_trip():
    entry = make_entry()

    restored = (
        DatasetRegistryEntry
        .model_validate_json(
            entry.model_dump_json(
                round_trip=True,
            )
        )
    )

    assert restored == entry

    assert (
        restored.publication_readiness
        == PublicationReadiness.APPROVED
    )


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "publication_readiness",
            "approved",
        ),
        (
            "is_free_ingestion_candidate",
            True,
        ),
        (
            "can_export_data",
            True,
        ),
    ],
)
def test_caller_cannot_override_derived_decisions(
    field,
    value,
):
    with pytest.raises(
        ValidationError,
    ):
        make_entry(
            **{
                field: value
            }
        )