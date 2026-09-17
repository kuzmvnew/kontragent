from sqlalchemy import select

from app.database.postgres import get_session
from app.models.legal_event import CompanyLegalEvent, LEGAL_EVENT_TYPES


EVENT_LABELS = {
    "bankruptcy_intent": "Опубликовано намерение обратиться с заявлением о банкротстве",
    "bankruptcy_application_filed": "Заявление о банкротстве подано",
    "bankruptcy_application_accepted": "Суд принял заявление о признании компании банкротом",
    "bankruptcy_observation": "Суд ввёл процедуру наблюдения",
    "bankruptcy_financial_rehabilitation": "Введено финансовое оздоровление",
    "bankruptcy_external_administration": "Введено внешнее управление",
    "bankruptcy_restructuring": "Введена реструктуризация долгов",
    "bankruptcy_asset_realisation": "Введена реализация имущества",
    "bankruptcy_estate": "Открыто конкурсное производство",
    "bankruptcy_procedure_terminated": "Производство по делу о банкротстве прекращено",
    "bankruptcy_procedure_completed": "Процедура банкротства завершена",
    "liquidation_decision": "Принято решение о ликвидации компании",
    "liquidation_in_process": "Компания находится в процессе ликвидации",
    "planned_exclusion": "Опубликовано решение о предстоящем исключении из ЕГРЮЛ",
    "actual_exclusion": "Компания исключена из ЕГРЮЛ",
}

CONFIRMED_BANKRUPTCY_PROCEDURES = {
    "bankruptcy_observation",
    "bankruptcy_financial_rehabilitation",
    "bankruptcy_external_administration",
    "bankruptcy_restructuring",
    "bankruptcy_asset_realisation",
    "bankruptcy_estate",
}


def validate_event_type(event_type: str) -> str:
    if event_type not in LEGAL_EVENT_TYPES:
        raise ValueError(f"Unsupported legal event type: {event_type}")
    return event_type


def interpret_event(event_type: str) -> dict:
    """Return conservative semantics without collapsing events to a boolean."""
    validate_event_type(event_type)
    bankruptcy_procedure_confirmed = event_type in CONFIRMED_BANKRUPTCY_PROCEDURES
    return {
        "label": EVENT_LABELS[event_type],
        "bankruptcy_procedure_confirmed": bankruptcy_procedure_confirmed,
        "liquidation_event": event_type.startswith("liquidation_"),
        "exclusion_event": event_type in {"planned_exclusion", "actual_exclusion"},
        "interpretation_note": _interpretation_note(event_type),
    }


def _interpretation_note(event_type: str) -> str:
    if event_type == "bankruptcy_intent":
        return "Намерение не подтверждает подачу заявления или введение процедуры банкротства."
    if event_type == "bankruptcy_application_filed":
        return "Подача заявления не подтверждает его принятие судом или введение процедуры."
    if event_type == "bankruptcy_application_accepted":
        return "Принятие заявления судом ещё не означает введение процедуры банкротства."
    if event_type.startswith("liquidation_"):
        return "Ликвидация не равна банкротству; процедура банкротства подтверждается отдельно."
    if event_type in {"planned_exclusion", "actual_exclusion"}:
        return "Исключение из ЕГРЮЛ не следует автоматически называть банкротством или ликвидацией."
    if event_type in {"bankruptcy_procedure_terminated", "bankruptcy_procedure_completed"}:
        return "Событие описывает завершение/прекращение процедуры и не подтверждает её текущую активность."
    return "Официальная стадия процедуры отображается только в пределах приложенного доказательства."


def serialize_legal_event(row: CompanyLegalEvent) -> dict:
    semantics = interpret_event(row.event_type)
    return {
        "event_type": row.event_type,
        "event_date": row.event_date,
        "publication_date": row.publication_date,
        "status": row.status,
        "source": row.source_code,
        "source_identifier": row.source_identifier,
        "source_url": row.source_url,
        "raw_event_type": row.raw_event_type,
        "evidence": dict(row.evidence or {}),
        "checked_at": row.checked_at,
        "retrieved_at": row.retrieved_at,
        **semantics,
    }


def get_legal_events_for_company(company_id: int, limit: int = 100) -> list[dict]:
    with get_session() as session:
        rows = session.scalars(
            select(CompanyLegalEvent)
            .where(CompanyLegalEvent.company_id == company_id)
            .order_by(
                CompanyLegalEvent.event_date.desc(),
                CompanyLegalEvent.id.desc(),
            )
            .limit(limit)
        ).all()
        return [serialize_legal_event(row) for row in rows]
