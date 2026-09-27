"""Measured scale controls and adaptive intake pressure for the company factory."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
import os
from pathlib import Path
import shutil
from typing import Literal

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.models.company import Company
from app.models.company_enrichment import CompanyEnrichmentRun, CompanySourceCoverage
from app.models.firmoteka import FirmotekaCrawlItem, FirmotekaRawArtifact
from app.models.registry_master import MasterReplaySignal
from app.models.source import DataSet
from app.services.source_applicability_service import source_applicability_clause


FactoryLane = Literal[
    "master_intake",
    "bulk_enrichment",
    "point_enrichment",
    "source_control",
]
FACTORY_LANES: tuple[FactoryLane, ...] = (
    "master_intake",
    "bulk_enrichment",
    "point_enrichment",
    "source_control",
)


def _environment_int(name: str, default: int, *, minimum: int = 0) -> int:
    raw = str(os.environ.get(name, default)).strip()
    try:
        value = int(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < minimum:
        raise ValueError(f"{name} must be at least {minimum}")
    return value


@dataclass(frozen=True)
class FactoryScaleConfig:
    """Non-secret controls; defaults preserve the proven production envelope."""

    active_run_limit: int = 100
    refill_low_watermark: int = 50
    bulk_batch_size: int = 100
    generation_selection_batch: int = 100
    reconcile_batch: int = 100
    master_intake_jobs: int = 2
    bulk_enrichment_jobs: int = 12
    point_enrichment_jobs: int = 2
    source_control_jobs: int = 4
    hard_backlog_companies: int = 2_000
    max_backlog_age_seconds: int = 3_600
    min_disk_free_percent: int = 10
    min_disk_runway_hours: int = 72
    max_host_cpu_percent: int = 90
    max_database_connection_percent: int = 80

    def __post_init__(self) -> None:
        if self.active_run_limit <= 0:
            raise ValueError("active_run_limit must be positive")
        if not 0 <= self.refill_low_watermark < self.active_run_limit:
            raise ValueError("refill_low_watermark must be below active_run_limit")
        if not 1 <= self.bulk_batch_size <= 5_000:
            raise ValueError("bulk_batch_size must be between 1 and 5000")
        if self.generation_selection_batch <= 0 or self.reconcile_batch <= 0:
            raise ValueError("factory batch controls must be positive")
        if not any(self.lane_budgets().values()):
            raise ValueError("at least one factory lane must have capacity")
        if (
            self.hard_backlog_companies <= 0
            or self.max_backlog_age_seconds <= 0
            or self.min_disk_runway_hours <= 0
        ):
            raise ValueError("backpressure bounds must be positive")
        for value in (
            self.min_disk_free_percent,
            self.max_host_cpu_percent,
            self.max_database_connection_percent,
        ):
            if not 1 <= value <= 100:
                raise ValueError("percentage controls must be between 1 and 100")

    def lane_budgets(self) -> dict[FactoryLane, int]:
        return {
            "master_intake": self.master_intake_jobs,
            "bulk_enrichment": self.bulk_enrichment_jobs,
            "point_enrichment": self.point_enrichment_jobs,
            "source_control": self.source_control_jobs,
        }

    @classmethod
    def from_environment(cls) -> "FactoryScaleConfig":
        return cls(
            active_run_limit=_environment_int(
                "FACTORY_ACTIVE_RUN_LIMIT", 100, minimum=1
            ),
            refill_low_watermark=_environment_int(
                "FACTORY_REFILL_LOW_WATERMARK", 50
            ),
            bulk_batch_size=_environment_int(
                "FACTORY_BULK_BATCH_SIZE", 100, minimum=1
            ),
            generation_selection_batch=_environment_int(
                "FACTORY_GENERATION_SELECTION_BATCH", 100, minimum=1
            ),
            reconcile_batch=_environment_int(
                "FACTORY_RECONCILE_BATCH", 100, minimum=1
            ),
            master_intake_jobs=_environment_int("FACTORY_MASTER_INTAKE_JOBS", 2),
            bulk_enrichment_jobs=_environment_int(
                "FACTORY_BULK_ENRICHMENT_JOBS", 12
            ),
            point_enrichment_jobs=_environment_int(
                "FACTORY_POINT_ENRICHMENT_JOBS", 2
            ),
            source_control_jobs=_environment_int(
                "FACTORY_SOURCE_CONTROL_JOBS", 4
            ),
            hard_backlog_companies=_environment_int(
                "FACTORY_HARD_BACKLOG_COMPANIES", 2_000, minimum=1
            ),
            max_backlog_age_seconds=_environment_int(
                "FACTORY_MAX_BACKLOG_AGE_SECONDS", 3_600, minimum=1
            ),
            min_disk_free_percent=_environment_int(
                "FACTORY_MIN_DISK_FREE_PERCENT", 10, minimum=1
            ),
            min_disk_runway_hours=_environment_int(
                "FACTORY_MIN_DISK_RUNWAY_HOURS", 72, minimum=1
            ),
            max_host_cpu_percent=_environment_int(
                "FACTORY_MAX_HOST_CPU_PERCENT", 90, minimum=1
            ),
            max_database_connection_percent=_environment_int(
                "FACTORY_MAX_DATABASE_CONNECTION_PERCENT", 80, minimum=1
            ),
        )


@dataclass(frozen=True)
class FactoryPressureInputs:
    actionable_backlog: int
    oldest_backlog_seconds: float | None
    enrichment_companies_per_hour: float
    firmoteka_companies_per_hour: float
    database_connection_percent: float
    host_cpu_percent: float
    disk_free_percent: float
    raw_growth_bytes_per_hour: int
    disk_free_bytes: int | None = None


@dataclass(frozen=True)
class FactoryPressureDecision:
    mode: Literal["open", "throttled", "paused"]
    allow_company_intake: bool
    recommended_intake_ratio: float
    reasons: tuple[str, ...]
    inputs: FactoryPressureInputs

    def as_dict(self) -> dict[str, object]:
        return {**asdict(self), "reasons": list(self.reasons)}


def adaptive_intake_decision(
    inputs: FactoryPressureInputs,
    config: FactoryScaleConfig,
) -> FactoryPressureDecision:
    """Fail closed on resource pressure; otherwise balance measured rates."""

    hard_reasons: list[str] = []
    if inputs.disk_free_percent < config.min_disk_free_percent:
        hard_reasons.append("disk_free_below_floor")
    if (
        inputs.raw_growth_bytes_per_hour > 0
        and inputs.disk_free_bytes is not None
        and inputs.disk_free_bytes / inputs.raw_growth_bytes_per_hour
        < config.min_disk_runway_hours
    ):
        hard_reasons.append("raw_growth_exhausts_disk_runway")
    if inputs.host_cpu_percent >= config.max_host_cpu_percent:
        hard_reasons.append("host_cpu_above_ceiling")
    if inputs.database_connection_percent >= config.max_database_connection_percent:
        hard_reasons.append("database_connections_above_ceiling")
    if inputs.actionable_backlog >= config.hard_backlog_companies:
        hard_reasons.append("actionable_backlog_above_ceiling")
    if (
        inputs.actionable_backlog > 0
        and inputs.oldest_backlog_seconds is not None
        and inputs.oldest_backlog_seconds >= config.max_backlog_age_seconds
        and inputs.enrichment_companies_per_hour <= 0
    ):
        hard_reasons.append("backlog_stalled")
    if hard_reasons:
        return FactoryPressureDecision(
            mode="paused",
            allow_company_intake=False,
            recommended_intake_ratio=0.0,
            reasons=tuple(hard_reasons),
            inputs=inputs,
        )

    if not inputs.actionable_backlog:
        return FactoryPressureDecision(
            mode="open",
            allow_company_intake=True,
            recommended_intake_ratio=1.0,
            reasons=("factory_caught_up",),
            inputs=inputs,
        )

    if inputs.firmoteka_companies_per_hour <= 0:
        ratio = 1.0
    else:
        ratio = min(
            1.0,
            inputs.enrichment_companies_per_hour
            / inputs.firmoteka_companies_per_hour,
        )
    if ratio < 0.9:
        return FactoryPressureDecision(
            mode="throttled",
            allow_company_intake=ratio >= 0.25,
            recommended_intake_ratio=round(ratio, 3),
            reasons=("intake_exceeds_enrichment_throughput",),
            inputs=inputs,
        )
    return FactoryPressureDecision(
        mode="open",
        allow_company_intake=True,
        recommended_intake_ratio=1.0,
        reasons=("sustainable_measured_throughput",),
        inputs=inputs,
    )


def collect_factory_pressure(
    session: Session,
    *,
    raw_root: str | Path,
    now: datetime | None = None,
) -> FactoryPressureInputs:
    """Collect inexpensive persisted and host inputs used by intake control."""

    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=1)
    active = select(
        CompanyEnrichmentRun.company_id.label("company_id"),
        CompanyEnrichmentRun.created_at.label("created_at"),
    ).where(
        CompanyEnrichmentRun.status.in_(
            ("pending", "waiting_sources", "retry_scheduled", "running")
        ),
        select(CompanySourceCoverage.id)
        .where(
            CompanySourceCoverage.enrichment_run_id == CompanyEnrichmentRun.id,
            CompanySourceCoverage.execution_status.in_(
                ("pending", "queued", "running", "retry_scheduled")
            ),
        )
        .exists(),
    )
    actionable_signals = (
        select(
            MasterReplaySignal.company_id.label("company_id"),
            MasterReplaySignal.created_at.label("created_at"),
        )
        .join(DataSet, DataSet.code == MasterReplaySignal.target_source_id)
        .join(Company, Company.id == MasterReplaySignal.company_id)
        .where(
            MasterReplaySignal.status.in_(("pending", "scheduled")),
            DataSet.enabled.is_(True),
            DataSet.auto_update_status == "configured",
            DataSet.operational_status == "current",
            DataSet.last_success_at.is_not(None),
            DataSet.coverage["operational_accepted"].as_boolean().is_not(False),
            (
                DataSet.next_expected_update_at.is_(None)
                | (DataSet.next_expected_update_at > now)
            ),
            source_applicability_clause(
                DataSet.applicability,
                Company.entity_type,
                Company.inn,
            ),
        )
    )
    backlog_rows = active.union_all(actionable_signals).subquery()
    backlog = int(
        session.scalar(select(func.count(func.distinct(backlog_rows.c.company_id))))
        or 0
    )
    oldest = session.scalar(select(func.min(backlog_rows.c.created_at)))
    completed = int(
        session.scalar(
            select(func.count(func.distinct(CompanyEnrichmentRun.company_id))).where(
                CompanyEnrichmentRun.status == "succeeded",
                CompanyEnrichmentRun.public_ready.is_(True),
                CompanyEnrichmentRun.finished_at >= cutoff,
                ~select(CompanySourceCoverage.id)
                .where(
                    CompanySourceCoverage.enrichment_run_id
                    == CompanyEnrichmentRun.id,
                    CompanySourceCoverage.status == "APPLICABILITY_UNKNOWN",
                )
                .exists(),
            )
        )
        or 0
    )
    firmoteka = int(
        session.scalar(
            select(func.count(func.distinct(FirmotekaCrawlItem.inn))).where(
                FirmotekaCrawlItem.status == "succeeded",
                FirmotekaCrawlItem.fetched_at >= cutoff,
            )
        )
        or 0
    )
    active_connections = float(
        session.scalar(text("SELECT count(*) FROM pg_stat_activity")) or 0
    )
    max_connections = float(
        session.scalar(select(func.current_setting("max_connections")).limit(1))
        or 100
    )
    cpu_count = max(1, os.cpu_count() or 1)
    try:
        host_cpu = min(100.0, os.getloadavg()[0] * 100.0 / cpu_count)
    except OSError:
        host_cpu = 0.0
    root = Path(raw_root).expanduser()
    usage = shutil.disk_usage(root if root.exists() else root.parent)
    raw_growth = int(
        session.scalar(
            select(func.coalesce(func.sum(FirmotekaRawArtifact.size_bytes), 0)).where(
                FirmotekaRawArtifact.retrieved_at >= cutoff
            )
        )
        or 0
    )
    return FactoryPressureInputs(
        actionable_backlog=backlog,
        oldest_backlog_seconds=(now - oldest).total_seconds() if oldest else None,
        enrichment_companies_per_hour=float(completed),
        firmoteka_companies_per_hour=float(firmoteka),
        database_connection_percent=round(
            active_connections * 100.0 / max(1.0, max_connections), 3
        ),
        host_cpu_percent=round(host_cpu, 3),
        disk_free_percent=round(usage.free * 100.0 / usage.total, 3),
        raw_growth_bytes_per_hour=raw_growth,
        disk_free_bytes=usage.free,
    )
