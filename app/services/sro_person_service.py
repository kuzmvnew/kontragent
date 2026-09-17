from __future__ import annotations

from sqlalchemy.dialects.postgresql import insert

from app.database.postgres import get_session
from app.models.nostroy import SroPersonRegistryRecord
from app.providers.sro_person_provider import NOPRIZ_NRS_URL, NoprizNrsProvider
from app.services.sro_registry_service import ensure_sro_datasets


def refresh_nopriz_nrs_private(registration_number, provider=None):
    """Private/internal only. No company or public serializer calls this function."""
    ensure_sro_datasets()
    response = (provider or NoprizNrsProvider()).check_registration_number(registration_number)
    session = get_session()
    try:
        saved = []
        for record in response["records"]:
            values = {
                "source_code": "nopriz_nrs",
                "source_record_id": record["source_record_id"],
                "official_registration_number": record["registration_number"],
                "person_name": record["person_name"],
                "company_inn": None,
                "relationship_status": "manual_review_required",
                "professional_status": record["professional_status"],
                "status_date": record["status_date"],
                "valid_from": record["valid_from"],
                "valid_to": None,
                "match_confidence": "official_record_id",
                "source_url": NOPRIZ_NRS_URL,
                "evidence": {"matching_method": "registration_number_exact", "company_relationship": "not_published_by_source", "http_status": 200},
                "public_visibility": False,
                "access_classification": "PRIVATE_INTERNAL",
                "retention_policy": "review_annually_or_on_source_change",
            }
            statement = insert(SroPersonRegistryRecord).values(**values).on_conflict_do_update(index_elements=["source_code", "source_record_id"], set_={k: v for k, v in values.items() if k not in {"source_code", "source_record_id"}}).returning(SroPersonRegistryRecord.id)
            saved.append(session.execute(statement).scalar_one())
        session.commit()
        return {"result": "found" if saved else "not_found", "record_count": len(saved), "private": True, "public_visibility": False, "ids": saved}
    except Exception:
        session.rollback(); raise
    finally:
        session.close()
