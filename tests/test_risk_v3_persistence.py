from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.contracts.risk_v3 import NormalizedEvidenceCandidate, SubjectScope
from app.database.postgres import engine
from app.models.company import Company
from app.models.risk import CompanyRiskAssessment
from app.models.risk_v3 import CompanyRiskAssessmentV3, CompanySummaryV3
from app.models.summary import CompanySummary
from app.services.risk_engine_v3_service import calculate_risk_v3
from app.services.risk_v3_persistence_service import (
    calculate_company_risk_v3_from_persisted,
    get_or_create_summary_v3,
    get_risk_assessment_v3,
    get_summary_v3,
    persist_risk_assessment_v3,
)
from tests.test_risk_v3 import NOW, complete_legal_baseline


def _for_company(company_id):
    values = []
    for item in complete_legal_baseline():
        payload = item.model_dump(mode="python")
        payload["company_id"] = company_id
        payload["subject_identity"] = {
            **item.subject_identity.model_dump(),
            "company_id": company_id,
        }
        payload["candidate_ref"] = f"company:{company_id}:{item.capability_code}"
        values.append(NormalizedEvidenceCandidate.model_validate(payload))
    return tuple(values)


def test_real_postgresql_v3_round_trip_reuse_constraints_and_v1_isolation():
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
        inspector = sa.inspect(connection)
        assert {
            "company_risk_assessments_v3",
            "company_summaries_v3",
        } <= set(inspector.get_table_names())
        risk_columns = {
            item["name"]: item["type"]
            for item in inspector.get_columns("company_risk_assessments_v3")
        }
        for forbidden in (
            "risk_score",
            "reliability_index",
            "section_scores",
            "weighted_coverage",
        ):
            assert forbidden not in risk_columns
        assert str(risk_columns["result_payload"]) == "JSONB"

    with Session(engine) as session:
        company = Company(
            inn=str(uuid4().int)[:10],
            name="Risk v3 persistence test",
            entity_type="legal",
        )
        session.add(company)
        session.flush()
        v1_risk_before = session.scalar(
            sa.select(sa.func.count()).select_from(CompanyRiskAssessment)
        )
        v1_summary_before = session.scalar(
            sa.select(sa.func.count()).select_from(CompanySummary)
        )

        assessment = calculate_risk_v3(
            _for_company(company.id),
            company_id=company.id,
            subject_scope=SubjectScope.LEGAL_ENTITY,
            calculated_at=NOW,
        )
        persisted, reused = persist_risk_assessment_v3(session, assessment)
        assert reused is False
        repeated, reused = persist_risk_assessment_v3(session, assessment)
        assert reused is True
        assert repeated == persisted
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CompanyRiskAssessmentV3)
        ) == 1
        assert get_risk_assessment_v3(session, assessment.assessment_id) == assessment

        summary, reused = get_or_create_summary_v3(
            session, assessment.assessment_id, generated_at=NOW
        )
        assert reused is False
        repeated_summary, reused = get_or_create_summary_v3(
            session, assessment.assessment_id, generated_at=NOW
        )
        assert reused is True
        assert repeated_summary == summary
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CompanySummaryV3)
        ) == 1
        assert get_summary_v3(session, summary.summary_id) == summary

        assert session.scalar(
            sa.select(sa.func.count()).select_from(CompanyRiskAssessment)
        ) == v1_risk_before
        assert session.scalar(
            sa.select(sa.func.count()).select_from(CompanySummary)
        ) == v1_summary_before
        row = session.scalar(
            sa.select(CompanyRiskAssessmentV3).where(
                CompanyRiskAssessmentV3.assessment_id == assessment.assessment_id
            )
        )
        assert row.result_payload["input_hash"] == assessment.input_hash
        assert row.coverage_snapshot["numerator"] == 9
        session.rollback()


def test_v3_orm_rejects_in_place_updates():
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    with Session(engine) as session:
        company = Company(
            inn=str(uuid4().int)[:10],
            name="Risk v3 immutable test",
            entity_type="legal",
        )
        session.add(company)
        session.flush()
        assessment = calculate_risk_v3(
            _for_company(company.id),
            company_id=company.id,
            subject_scope=SubjectScope.LEGAL_ENTITY,
            calculated_at=NOW,
        )
        persist_risk_assessment_v3(session, assessment)
        row = session.scalar(
            sa.select(CompanyRiskAssessmentV3).where(
                CompanyRiskAssessmentV3.assessment_id == assessment.assessment_id
            )
        )
        row.subject_scope = "UNKNOWN"
        with pytest.raises(ValueError, match="immutable"):
            session.flush()
        session.rollback()


def test_db_only_boundary_calculates_from_persisted_rows_without_refresh():
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT current_database()")) != "kontragent"
    with Session(engine) as session:
        company = Company(
            inn=str(uuid4().int)[:10],
            name="Risk v3 DB-only test",
            entity_type="legal",
            status="ACTIVE",
        )
        session.add(company)
        session.flush()
        result, reused = calculate_company_risk_v3_from_persisted(
            session, company.id, calculated_at=NOW
        )
        assert reused is False
        assert result.company_id == company.id
        assert len(result.evidence_snapshot) == 13
        assert any(
            item.capability_code == "fssp" and item.execution.value == "NOT_CHECKED"
            for item in result.evidence_snapshot
        )
        assert result.mandatory_gate.allowed is False
        session.rollback()
