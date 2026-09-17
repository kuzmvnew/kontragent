from __future__ import annotations

import re

from sqlalchemy import func, select

from app.database.postgres import get_session
from app.models.company import Company
from app.services.company_fact_service import upsert_company_public_fact


ROOM_MARKER = re.compile(r"\b(?:офис|помещ(?:ение)?|комн(?:ата)?|кв(?:артира)?|литер[а]?|стр(?:оение)?)\b.*$", re.I)


def normalize_legal_address(value: str | None) -> str | None:
    if not value:
        return None
    value = value.upper().replace("Ё", "Е")
    value = re.sub(r"[\"'«»]", "", value)
    value = re.sub(r"[^0-9А-ЯA-Z]+", " ", value)
    return " ".join(value.split()) or None


def building_level_address(value: str | None) -> str | None:
    normalized = normalize_legal_address(value)
    if not normalized:
        return None
    return ROOM_MARKER.sub("", normalized).strip() or normalized


def refresh_company_address_context(inn: str) -> dict:
    session = get_session()
    try:
        company = session.scalar(select(Company).where(Company.inn == inn))
        if company is None or not company.address:
            return {"status": "unavailable", "reason": "legal_address_missing"}
        exact_active_count = session.scalar(
            select(func.count()).select_from(Company).where(
                Company.address == company.address,
                Company.status == "ACTIVE",
            )
        )
        value = {
            "legal_address": company.address,
            "normalized_legal_address": normalize_legal_address(company.address),
            "building_level_address": building_level_address(company.address),
            "exact_full_address_active_count": int(exact_active_count or 0),
            "building_level_active_count": None,
            "room_office_active_count": None,
            "coverage": "exact_full_address_active_count_only",
            "interpretation": "Address concentration is context only; a business centre is not automatically a negative fact.",
        }
        company_id = company.id
        dataset_id = company.master_dataset_id
    finally:
        session.close()
    upsert_company_public_fact(
        company_id=company_id,
        dataset_id=dataset_id,
        fact_type="mass_address",
        value=value,
        source_code="master_registry",
        source_identifier=f"{inn}:exact_active_address_count",
        source_url="internal://master-registry/company-address",
        currentness="derived_current_snapshot",
        confidence="exact_address_derived",
        evidence={"matching_method": "exact_normalized_legal_address", "negative_inference": False},
    )
    return {"status": "success", **value}
