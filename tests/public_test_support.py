from __future__ import annotations

from datetime import date, datetime, timezone

from public_app.contracts import (
    CompanyInfo,
    Freshness,
    PublicationInfo,
    PublicProjection,
    PublicRisk,
    PublicRiskFactor,
    PublicSourceBlock,
    PublicState,
    PublicSummary,
)


def legal_inn(sequence: int) -> str:
    prefix = f"{sequence % 1_000_000_000:09d}"
    weights = (2, 4, 10, 3, 5, 9, 4, 6, 8)
    check = sum(int(digit) * weight for digit, weight in zip(prefix, weights)) % 11 % 10
    return prefix + str(check)


def projection(
    sequence: int = 274101890,
    *,
    release_id: str = "public-v1-test-release",
    state: PublicState = PublicState.PARTIAL,
    index_eligible: bool = True,
) -> PublicProjection:
    inn = "0274101890" if sequence == 274101890 else legal_inn(sequence)
    now = datetime(2026, 9, 25, 7, 0, tzinfo=timezone.utc)
    sources = tuple(
        PublicSourceBlock(
            code=code,
            state=PublicState.FOUND,
            values={"Показатель, ₽": "100.00"},
            source_name=f"Источник {code}",
            source_data_date=date(2026, 8, 1),
            result_date=date(2026, 9, 25),
            freshness=Freshness.CURRENT,
        )
        for code in ("REVEXP", "PAYTAX", "DEBTAM", "TAXOFFENCE")
    )
    return PublicProjection(
        publication=PublicationInfo(
            schema_version="public-projection-v1",
            release_id=release_id,
            published_at=now,
            result_date=now.date(),
            content_updated_at=now,
            index_eligible=index_eligible,
        ),
        company=CompanyInfo(
            name=f"ООО ТЕСТ {sequence}",
            full_name=f"ОБЩЕСТВО С ОГРАНИЧЕННОЙ ОТВЕТСТВЕННОСТЬЮ ТЕСТ {sequence}",
            legal_status="Действует",
            inn=inn,
            kpp="027701001",
            ogrn="1050203896027",
            address="г. Екатеринбург",
            registration_date=date(2020, 1, 1),
            director_name="Иванов Иван Иванович",
            director_position="Директор",
        ),
        risk=PublicRisk(
            state=state,
            title="Оценка содержит ограничения" if state == PublicState.PARTIAL else "Выявлены факторы",
            explanation="Risk v3 объясняет подтверждённые факторы без рейтинга.",
            factors=(
                PublicRiskFactor(
                    meaning_id="meaning:test:tax-debt",
                    category="Налоги",
                    severity="Значимый фактор",
                    title="По данным ФНС указана налоговая задолженность.",
                    explanation="В официальном наборе данных для ИНН указана ненулевая сумма задолженности.",
                    full_explanation="В официальном наборе данных для ИНН указана ненулевая сумма задолженности.",
                    client_meaning="Фактор следует учесть при согласовании условий оплаты.",
                    what_it_does_not_mean="Запись не доказывает невозможность расчёта по сделке.",
                    current_state="Текущий подтверждённый факт",
                    confidence=1.0,
                    source_name="ФНС",
                    source_data_date=date(2026, 8, 1),
                ),
            ),
            limitations=("PARTIAL_SCOPE",) if state == PublicState.PARTIAL else (),
            assessment_date=date(2026, 9, 25),
            model_version="risk-v3",
            ruleset_version="rules-v3",
        ),
        summary=PublicSummary(
            short_conclusion="Выявлен фактор, требующий внимания.",
            main_factors=("Налоговая задолженность",),
            limitations=("PARTIAL_SCOPE",),
            recommendations=("REQUEST_TAX_DEBT_CLEARANCE",),
            generated_at=now,
        ),
        sources=sources,
    )


def forty_projections(
    release_id: str,
    *,
    sequence_start: int = 100_000_000,
) -> list[PublicProjection]:
    return [projection(sequence=sequence_start + index, release_id=release_id) for index in range(40)]
