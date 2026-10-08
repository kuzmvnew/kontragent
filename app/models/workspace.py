"""Customer Workspace P0 persistence.

Company remains global. Every customer-owned object carries workspace_id and
never copies company master data.
"""

from __future__ import annotations

from datetime import date, datetime
from uuid import UUID, uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class CustomerUser(Base):
    __tablename__ = "customer_users"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_customer_user_status"),
        CheckConstraint(
            "email::text = lower(btrim(email::text))",
            name="ck_customer_user_email_normalized",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class Workspace(Base):
    __tablename__ = "workspaces"
    __table_args__ = (
        CheckConstraint("status IN ('active','suspended','closed')", name="ck_workspace_status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(250), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class WorkspaceRole(Base):
    __tablename__ = "workspace_roles"
    __table_args__ = (
        UniqueConstraint("workspace_id", "role_key", name="uq_workspace_role_key"),
        UniqueConstraint("workspace_id", "id", name="uq_workspace_role_scope"),
        CheckConstraint(
            "role_key IN ('OWNER','ADMIN','MEMBER')",
            name="ck_workspace_role_key",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    role_key: Mapped[str] = mapped_column(String(80), nullable=False)
    name: Mapped[str] = mapped_column(String(160), nullable=False)
    is_system: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class WorkspaceRoleCapability(Base):
    __tablename__ = "workspace_role_capabilities"

    role_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspace_roles.id", ondelete="CASCADE"), primary_key=True)
    capability_key: Mapped[str] = mapped_column(String(100), primary_key=True)


class WorkspaceMembership(Base):
    __tablename__ = "workspace_memberships"
    __table_args__ = (
        UniqueConstraint("workspace_id", "user_id", name="uq_workspace_membership"),
        CheckConstraint("status IN ('active','suspended','revoked')", name="ck_workspace_membership_status"),
        ForeignKeyConstraint(
            ("workspace_id", "role_id"),
            ("workspace_roles.workspace_id", "workspace_roles.id"),
            name="fk_workspace_membership_role_scope",
            ondelete="RESTRICT",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("customer_users.id", ondelete="CASCADE"), nullable=False, index=True)
    role_id: Mapped[UUID] = mapped_column(Uuid, nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


class CustomerSession(Base):
    __tablename__ = "customer_sessions"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True)
    csrf_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("customer_users.id", ondelete="CASCADE"), nullable=False, index=True)
    active_workspace_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="SET NULL"), nullable=True, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)


class WorkspaceEntitlement(Base):
    __tablename__ = "workspace_entitlements"
    __table_args__ = (
        UniqueConstraint("workspace_id", "entitlement_key", name="uq_workspace_entitlement"),
        CheckConstraint("limit_value IS NULL OR limit_value >= 0", name="ck_workspace_entitlement_limit"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    entitlement_key: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    limit_value: Mapped[int | None] = mapped_column(Integer, nullable=True)
    policy_version: Mapped[str] = mapped_column(String(80), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now())


class SavedCompany(Base):
    __tablename__ = "saved_companies"
    __table_args__ = (
        UniqueConstraint("workspace_id", "company_id", name="uq_saved_company"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True)
    company_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("companies.id", ondelete="CASCADE"), nullable=False, index=True)
    saved_by_user_id: Mapped[UUID] = mapped_column(Uuid, ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=False, index=True)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class WorkspaceAuditEvent(Base):
    __tablename__ = "workspace_audit_events"
    __table_args__ = (
        CheckConstraint(
            "workspace_id IS NOT NULL OR (action::text = ANY "
            "(ARRAY['auth.login'::character varying::text, "
            "'auth.logout'::character varying::text, "
            "'workspace.select'::character varying::text]))",
            name="ck_workspace_audit_nullable_workspace",
        ),
        CheckConstraint(
            "workspace_id IS NOT NULL OR "
            "action::text <> 'workspace.select'::text OR "
            "outcome::text = 'denied'::text",
            name="ck_workspace_audit_denied_selection_workspace",
        ),
        CheckConstraint(
            "actor_user_id IS NOT NULL OR action::text = 'auth.login'::text "
            "AND outcome::text = 'denied'::text",
            name="ck_workspace_audit_nullable_actor",
        ),
        CheckConstraint(
            "(action::text <> ALL (ARRAY['auth.login'::character varying::text, "
            "'auth.logout'::character varying::text, "
            "'workspace.select'::character varying::text])) "
            "OR (outcome::text = ANY "
            "(ARRAY['success'::character varying::text, "
            "'denied'::character varying::text]))",
            name="ck_workspace_audit_auth_outcome",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=True, index=True)
    actor_user_id: Mapped[UUID | None] = mapped_column(Uuid, ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(80), nullable=False)
    target_ref: Mapped[str] = mapped_column(String(240), nullable=False)
    outcome: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now(), index=True)


class WorkspaceReport(Base):
    """Immutable customer-safe report snapshot owned by one Workspace."""

    __tablename__ = "workspace_reports"
    __table_args__ = (
        CheckConstraint(
            "report_type::text = 'COMPANY_CHECK_V1'::text",
            name="ck_workspace_report_type",
        ),
        CheckConstraint(
            "schema_version::text = 'workspace-report-v1'::text",
            name="ck_workspace_report_schema_version",
        ),
        CheckConstraint(
            "subject_inn::text ~ '^[0-9]{10}$'::text",
            name="ck_workspace_report_subject_inn",
        ),
        CheckConstraint(
            "snapshot_sha256::text ~ '^[0-9a-f]{64}$'::text",
            name="ck_workspace_report_snapshot_sha256",
        ),
        Index(
            "ix_workspace_reports_workspace_generated",
            "workspace_id",
            "generated_at",
        ),
        Index(
            "ix_workspace_reports_company_generated",
            "company_id",
            "generated_at",
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    company_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("companies.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    generated_by_user_id: Mapped[UUID] = mapped_column(
        Uuid,
        ForeignKey("customer_users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    report_type: Mapped[str] = mapped_column(String(80), nullable=False)
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    subject_inn: Mapped[str] = mapped_column(String(10), nullable=False, index=True)
    subject_name: Mapped[str] = mapped_column(String(1000), nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    company_view_contract_version: Mapped[str] = mapped_column(
        String(80), nullable=False
    )
    company_view_generated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    risk_ref: Mapped[str | None] = mapped_column(String(240), nullable=True)
    summary_ref: Mapped[str | None] = mapped_column(String(240), nullable=True)
    snapshot: Mapped[dict] = mapped_column(JSONB, nullable=False)
    snapshot_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class WorkspaceBulkJob(Base):
    """Durable, tenant-owned customer Bulk Check job."""

    __tablename__ = "workspace_bulk_jobs"
    __table_args__ = (
        UniqueConstraint("workspace_id", "id", name="uq_workspace_bulk_job_scope"),
        CheckConstraint(
            "schema_version::text = 'workspace-bulk-check-v1'::text",
            name="ck_workspace_bulk_job_schema_version",
        ),
        CheckConstraint(
            "status IN ('READY','RUNNING','COMPLETED','COMPLETED_WITH_ERRORS','CANCELLED','FAILED')",
            name="ck_workspace_bulk_job_status",
        ),
        CheckConstraint(
            "input_sha256::text ~ '^[0-9a-f]{64}$'::text",
            name="ck_workspace_bulk_job_input_sha256",
        ),
        CheckConstraint(
            "total_rows >= 0 AND unique_valid_count >= 0 AND invalid_count >= 0 "
            "AND duplicate_count >= 0 AND processed_count >= 0 AND ready_count >= 0 "
            "AND not_resolved_count >= 0 AND not_ready_count >= 0 "
            "AND failed_count >= 0 AND cancelled_count >= 0",
            name="ck_workspace_bulk_job_nonnegative_counts",
        ),
        Index("ix_workspace_bulk_jobs_workspace_created", "workspace_id", "created_at"),
        Index("ix_workspace_bulk_jobs_workspace_status", "workspace_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_by_user_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("customer_users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    schema_version: Mapped[str] = mapped_column(String(80), nullable=False)
    status: Mapped[str] = mapped_column(String(40), nullable=False, index=True)
    original_filename: Mapped[str] = mapped_column(String(240), nullable=False)
    input_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    public_release_id: Mapped[str | None] = mapped_column(String(120), nullable=True, index=True)
    public_schema_version: Mapped[str | None] = mapped_column(String(80), nullable=True)
    public_result_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    total_rows: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    unique_valid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    invalid_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_resolved_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    not_ready_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    cancelled_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    cancel_requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)


class WorkspaceBulkItem(Base):
    """One uploaded row and its historical result under a Bulk Check job."""

    __tablename__ = "workspace_bulk_items"
    __table_args__ = (
        ForeignKeyConstraint(
            ("workspace_id", "job_id"),
            ("workspace_bulk_jobs.workspace_id", "workspace_bulk_jobs.id"),
            name="fk_workspace_bulk_item_job_scope",
            ondelete="CASCADE",
        ),
        UniqueConstraint("job_id", "row_number", name="uq_workspace_bulk_item_row"),
        CheckConstraint("row_number > 0", name="ck_workspace_bulk_item_row_number"),
        CheckConstraint(
            "status IN ('PENDING','INVALID_INN','DUPLICATE','READY','NOT_RESOLVED','NOT_READY','PROCESSING_ERROR','CANCELLED')",
            name="ck_workspace_bulk_item_status",
        ),
        CheckConstraint(
            "result_sha256 IS NULL OR result_sha256::text ~ '^[0-9a-f]{64}$'::text",
            name="ck_workspace_bulk_item_result_sha256",
        ),
        CheckConstraint(
            "status::text = 'READY'::text AND result_payload IS NOT NULL AND result_sha256 IS NOT NULL "
            "OR status::text <> 'READY'::text AND result_payload IS NULL AND result_sha256 IS NULL",
            name="ck_workspace_bulk_item_result_state",
        ),
        CheckConstraint(
            "duplicate_of_row IS NULL OR duplicate_of_row < row_number",
            name="ck_workspace_bulk_item_duplicate_row",
        ),
        Index("ix_workspace_bulk_items_job_row", "job_id", "row_number"),
        Index("ix_workspace_bulk_items_workspace_job", "workspace_id", "job_id"),
        Index("ix_workspace_bulk_items_job_status", "job_id", "status"),
        Index("ix_workspace_bulk_items_normalized_inn", "normalized_inn"),
        Index("ix_workspace_bulk_items_company_id", "company_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    workspace_id: Mapped[UUID] = mapped_column(
        Uuid, ForeignKey("workspaces.id", ondelete="CASCADE"), nullable=False
    )
    job_id: Mapped[UUID] = mapped_column(Uuid, nullable=False)
    row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_inn: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_inn: Mapped[str | None] = mapped_column(String(32), nullable=True)
    status: Mapped[str] = mapped_column(String(40), nullable=False)
    duplicate_of_row: Mapped[int | None] = mapped_column(Integer, nullable=True)
    company_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("companies.id", ondelete="SET NULL"), nullable=True
    )
    result_payload: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    result_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(500), nullable=True)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
