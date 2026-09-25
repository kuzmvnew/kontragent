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
            factors=(PublicRiskFactor(title="Налоговая задолженность", source_name="ФНС", source_data_date=date(2026, 8, 1)),),
            limitations=("Один источник проверен частично.",) if state == PublicState.PARTIAL else (),
            assessment_date=date(2026, 9, 25),
            model_version="risk-v3",
            ruleset_version="rules-v3",
        ),
        summary=PublicSummary(
            short_conclusion="Выявлен фактор, требующий внимания.",
            main_factors=("Налоговая задолженность",),
            limitations=("Один источник проверен частично.",),
            recommendations=("Проверить актуальное состояние расчётов.",),
            generated_at=now,
        ),
        sources=sources,
    )


def forty_projections(release_id: str) -> list[PublicProjection]:
    return [projection(sequence=100_000_000 + index, release_id=release_id) for index in range(40)]
