"""Строгие контракты каталога источников и загруженных срезов данных."""

from datetime import date, datetime
from enum import StrEnum

from pydantic import (
    Field,
    HttpUrl,
    StrictBool,
    computed_field,
    field_validator,
    model_validator,
)

from app.contracts.decision import ContractModel


class SourceAuthority(StrEnum):
    OFFICIAL = "official"
    PARTNER = "partner"
    COMMERCIAL = "commercial"
    UNKNOWN = "unknown"


class AccessMethod(StrEnum):
    BULK_FILE = "bulk_file"
    API = "api"
    WEB_INTERFACE = "web_interface"
    MANUAL = "manual"
    OTHER = "other"


class AccessCost(StrEnum):
    FREE = "free"
    PAID = "paid"
    TRIAL = "trial"
    UNKNOWN = "unknown"


class EntityKind(StrEnum):
    LEGAL_ENTITY = "legal_entity"
    INDIVIDUAL_ENTREPRENEUR = "individual_entrepreneur"
    PERSON = "person"
    OTHER = "other"


class RefreshCadence(StrEnum):
    REAL_TIME = "real_time"
    DAILY = "daily"
    WEEKLY = "weekly"
    MONTHLY = "monthly"
    QUARTERLY = "quarterly"
    ON_PUBLICATION = "on_publication"
    MANUAL = "manual"
    UNKNOWN = "unknown"


class PermissionDecision(StrEnum):
    ALLOWED = "allowed"
    RESTRICTED = "restricted"
    PROHIBITED = "prohibited"
    UNKNOWN = "unknown"


class DatasetStatus(StrEnum):
    RESEARCH = "research"
    READY = "ready"
    ACTIVE = "active"
    DEGRADED = "degraded"
    BLOCKED = "blocked"
    RETIRED = "retired"


class SnapshotStatus(StrEnum):
    LOADING = "loading"
    AVAILABLE = "available"
    STALE = "stale"
    FAILED = "failed"


class PublicationReadiness(StrEnum):
    APPROVED = "approved"
    REVIEW_REQUIRED = "review_required"
    BLOCKED = "blocked"


def _unique_enum_values(
    values: tuple,
    *,
    field_name: str,
) -> tuple:
    if len(values) != len(set(values)):
        raise ValueError(
            f"{field_name} не должен содержать дубли"
        )

    return values


def _aware(
    value: datetime,
    *,
    field_name: str,
) -> datetime:
    if (
        value.tzinfo is None
        or value.utcoffset() is None
    ):
        raise ValueError(
            f"{field_name} должен содержать часовой пояс"
        )

    return value


class UsagePermission(ContractModel):
    """
    Зафиксированное решение по использованию,
    а не юридическое заключение.
    """

    storage: PermissionDecision = (
        PermissionDecision.UNKNOWN
    )

    public_display: PermissionDecision = (
        PermissionDecision.UNKNOWN
    )

    commercial_use: PermissionDecision = (
        PermissionDecision.UNKNOWN
    )

    redistribution: PermissionDecision = (
        PermissionDecision.UNKNOWN
    )

    reviewed_on: date | None = None

    reviewed_by: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )

    basis_url: HttpUrl | None = None

    notes: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )

    @model_validator(mode="after")
    def validate_review(
        self,
    ) -> "UsagePermission":
        decisions = {
            self.storage,
            self.public_display,
            self.commercial_use,
            self.redistribution,
        }

        has_reviewed_decision = (
            decisions
            != {PermissionDecision.UNKNOWN}
        )

        review_fields = (
            self.reviewed_on,
            self.reviewed_by,
            self.basis_url,
        )

        if (
            has_reviewed_decision
            and any(
                value is None
                for value in review_fields
            )
        ):
            raise ValueError(
                "Для решения по использованию нужны "
                "дата, автор и основание"
            )

        if (
            not has_reviewed_decision
            and any(
                value is not None
                for value in review_fields
            )
        ):
            raise ValueError(
                "Неизвестные права не должны "
                "выглядеть проверенными"
            )

        return self


class DatasetSnapshot(ContractModel):
    """
    Один технический срез набора данных
    и результат его обработки.
    """

    snapshot_id: str = Field(
        min_length=1,
        max_length=200,
    )

    dataset_code: str = Field(
        min_length=1,
        max_length=100,
    )

    status: SnapshotStatus
    retrieved_at: datetime
    source_data_date: date | None = None

    version_label: str | None = Field(
        default=None,
        min_length=1,
        max_length=500,
    )

    checksum_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )

    source_records: int | None = Field(
        default=None,
        ge=0,
    )

    imported_records: int | None = Field(
        default=None,
        ge=0,
    )

    unmatched_records: int | None = Field(
        default=None,
        ge=0,
    )

    rejected_records: int | None = Field(
        default=None,
        ge=0,
    )

    failure_reason: str | None = Field(
        default=None,
        min_length=1,
        max_length=4000,
    )

    @field_validator("retrieved_at")
    @classmethod
    def validate_retrieved_at(
        cls,
        value: datetime,
    ) -> datetime:
        return _aware(
            value,
            field_name="retrieved_at",
        )

    @model_validator(mode="after")
    def validate_snapshot(
        self,
    ) -> "DatasetSnapshot":
        complete = self.status in {
            SnapshotStatus.AVAILABLE,
            SnapshotStatus.STALE,
        }

        count_fields = (
            self.source_records,
            self.imported_records,
            self.unmatched_records,
            self.rejected_records,
        )

        if complete:
            if (
                self.source_data_date is None
                or self.checksum_sha256 is None
            ):
                raise ValueError(
                    "Доступному срезу нужны "
                    "дата данных и checksum"
                )

            if any(
                value is None
                for value in count_fields
            ):
                raise ValueError(
                    "Доступному срезу нужны "
                    "все счётчики обработки"
                )

            if self.source_records != sum(
                count_fields[1:]
            ):
                raise ValueError(
                    "source_records должен равняться "
                    "сумме результатов обработки"
                )

            if self.failure_reason is not None:
                raise ValueError(
                    "Доступный срез не должен "
                    "содержать failure_reason"
                )

        elif any(
            value is not None
            for value in count_fields
        ):
            raise ValueError(
                "Незавершённый срез не должен "
                "публиковать итоговые счётчики"
            )

        if (
            self.status == SnapshotStatus.FAILED
            and self.failure_reason is None
        ):
            raise ValueError(
                "Неуспешному срезу нужна "
                "причина ошибки"
            )

        if (
            self.status != SnapshotStatus.FAILED
            and self.failure_reason is not None
        ):
            raise ValueError(
                "failure_reason допустим "
                "только для failed"
            )

        return self


class DatasetRegistryEntry(ContractModel):
    """Паспорт одного dataset в реестре источников проекта."""

    dataset_code: str = Field(
        min_length=1,
        max_length=100,
    )

    source_code: str = Field(
        min_length=1,
        max_length=100,
    )

    title: str = Field(
        min_length=1,
        max_length=1000,
    )

    description: str = Field(
        min_length=1,
        max_length=4000,
    )

    owner_name: str = Field(
        min_length=1,
        max_length=1000,
    )

    authority: SourceAuthority
    source_url: HttpUrl
    terms_url: HttpUrl | None = None

    access_methods: tuple[
        AccessMethod,
        ...,
    ] = Field(
        min_length=1,
    )

    access_cost: AccessCost

    entity_kinds: tuple[
        EntityKind,
        ...,
    ] = Field(
        min_length=1,
    )

    refresh_cadence: RefreshCadence
    requires_auth: StrictBool
    contains_personal_data: StrictBool
    status: DatasetStatus
    usage_permission: UsagePermission
    current_snapshot: DatasetSnapshot | None = None
    registered_at: datetime
    updated_at: datetime

    @field_validator(
        "access_methods",
        "entity_kinds",
    )
    @classmethod
    def validate_unique_values(
        cls,
        values: tuple,
        info,
    ) -> tuple:
        return _unique_enum_values(
            values,
            field_name=info.field_name,
        )

    @field_validator(
        "registered_at",
        "updated_at",
    )
    @classmethod
    def validate_timestamps(
        cls,
        value: datetime,
        info,
    ) -> datetime:
        return _aware(
            value,
            field_name=info.field_name,
        )

    @model_validator(mode="after")
    def validate_entry(
        self,
    ) -> "DatasetRegistryEntry":
        if self.updated_at < self.registered_at:
            raise ValueError(
                "updated_at не может быть "
                "раньше registered_at"
            )

        if self.current_snapshot is not None:
            if (
                self.current_snapshot.dataset_code
                != self.dataset_code
            ):
                raise ValueError(
                    "Срез относится к другому dataset"
                )

            if (
                self.current_snapshot.retrieved_at
                > self.updated_at
            ):
                raise ValueError(
                    "Срез не может быть новее "
                    "записи реестра"
                )

        if self.status in {
            DatasetStatus.ACTIVE,
            DatasetStatus.DEGRADED,
        }:
            if self.current_snapshot is None:
                raise ValueError(
                    "Активному dataset нужен "
                    "текущий срез"
                )

            allowed = (
                {SnapshotStatus.AVAILABLE}
                if self.status
                == DatasetStatus.ACTIVE
                else {
                    SnapshotStatus.AVAILABLE,
                    SnapshotStatus.STALE,
                    SnapshotStatus.FAILED,
                }
            )

            if (
                self.current_snapshot.status
                not in allowed
            ):
                raise ValueError(
                    "Состояние среза не соответствует "
                    "состоянию dataset"
                )

        return self

    @computed_field
    @property
    def publication_readiness(
        self,
    ) -> PublicationReadiness:
        if self.status in {
            DatasetStatus.BLOCKED,
            DatasetStatus.RETIRED,
        }:
            return PublicationReadiness.BLOCKED

        decisions = (
            self.usage_permission.storage,
            self.usage_permission.public_display,
            self.usage_permission.commercial_use,
        )

        if any(
            value == PermissionDecision.PROHIBITED
            for value in decisions
        ):
            return PublicationReadiness.BLOCKED

        if (
            self.status not in {
                DatasetStatus.ACTIVE,
                DatasetStatus.DEGRADED,
            }
            or self.current_snapshot is None
            or self.current_snapshot.status
            not in {
                SnapshotStatus.AVAILABLE,
                SnapshotStatus.STALE,
            }
            or any(
                value != PermissionDecision.ALLOWED
                for value in decisions
            )
        ):
            return (
                PublicationReadiness.REVIEW_REQUIRED
            )

        return PublicationReadiness.APPROVED

    @computed_field
    @property
    def is_free_ingestion_candidate(
        self,
    ) -> bool:
        supported_methods = {
            AccessMethod.BULK_FILE,
            AccessMethod.API,
        }

        return (
            self.authority
            == SourceAuthority.OFFICIAL
            and self.access_cost
            == AccessCost.FREE
            and bool(
                supported_methods.intersection(
                    self.access_methods
                )
            )
            and self.status in {
                DatasetStatus.READY,
                DatasetStatus.ACTIVE,
                DatasetStatus.DEGRADED,
            }
            and self.usage_permission.storage
            == PermissionDecision.ALLOWED
        )

    @computed_field
    @property
    def can_export_data(
        self,
    ) -> bool:
        return (
            self.publication_readiness
            == PublicationReadiness.APPROVED
            and self.usage_permission.redistribution
            == PermissionDecision.ALLOWED
        )