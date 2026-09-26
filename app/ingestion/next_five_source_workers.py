"""Fail-closed Worker Foundation contracts for the next five source families.

The product/cache models for courts and legal events already exist.  This
module only makes each future transport visible as an independent Worker
source.  Until a permitted machine channel and source-specific baseline are
accepted, execution stops with ``AccessRequiredError`` before network or
publication work.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
from typing import Mapping

from sqlalchemy.orm import Session

from app.worker.contracts import HandlerContext, HandlerResult
from app.worker.errors import AccessRequiredError
from app.worker.execution import register_handler
from app.worker.registry import HandlerRegistry, RegisteredHandler


HANDLER_VERSION = "source-factory-access-gate-v1"


@dataclass(frozen=True)
class SourceAccessContract:
    source_id: str
    access_channel: str
    required_environment: tuple[str, ...] = ()
    pending_reason: str = "permitted machine access and baseline are not accepted"


CONTRACTS = {
    "fedresurs_messages": SourceAccessContract(
        source_id="fedresurs_messages",
        access_channel="fedresurs_machine_channel",
        pending_reason="permitted Fedresurs machine access and event baseline are not accepted",
    ),
    "checko_arbitration_cases": SourceAccessContract(
        source_id="checko_arbitration_cases",
        access_channel="checko_legal_cases_api",
        required_environment=("CHECKO_API_KEY",),
        pending_reason="Checko arbitration API terms and source baseline are not accepted",
    ),
    "fssp_enforcement": SourceAccessContract(
        source_id="fssp_enforcement",
        access_channel="fssp_machine_channel",
        pending_reason="permitted FSSP machine access and enforcement baseline are not accepted",
    ),
    "moscow_general_court_cases": SourceAccessContract(
        source_id="moscow_general_court_cases",
        access_channel="region_scoped_general_court_search",
        pending_reason="stable permitted court access and explicit regional coverage are not accepted",
    ),
    "eis_procurements": SourceAccessContract(
        source_id="eis_procurements",
        access_channel="eis_procurement_archive",
        required_environment=("EIS_IP_TOKEN",),
        pending_reason="EIS procurement archive schema has not been accepted separately from RNP",
    ),
}


def access_state(
    source_id: str,
    environment: Mapping[str, str] | None = None,
) -> tuple[bool, tuple[str, ...]]:
    """Return the access gate without exposing credential values."""

    contract = CONTRACTS[source_id]
    values = environment if environment is not None else os.environ
    missing = tuple(
        name for name in contract.required_environment if not values.get(name, "").strip()
    )
    # Credential presence alone never proves accepted terms, schema or
    # production permission for a new source.
    return False, missing


def access_pending_handler(context: HandlerContext) -> HandlerResult:
    """Non-empty production handler that fails before any external request."""

    contract = CONTRACTS.get(context.source_id)
    if contract is None:
        raise AccessRequiredError("unknown Source Factory access contract")
    _, missing = access_state(context.source_id)
    if missing:
        raise AccessRequiredError(
            f"{context.source_id} requires configured access: {', '.join(missing)}"
        )
    raise AccessRequiredError(f"{context.source_id}: {contract.pending_reason}")


def register_next_five_workers(
    session: Session,
    registry: HandlerRegistry,
) -> tuple[RegisteredHandler, ...]:
    """Connect five distinct source IDs without scheduling any work."""

    return tuple(
        register_handler(
            session,
            registry,
            source_id=source_id,
            version=HANDLER_VERSION,
            handler=access_pending_handler,
            publisher=None,
            approved=True,
            live=False,
            fixture=False,
            metadata={
                "mode": "access_gate",
                "access_channel": contract.access_channel,
                "required_environment": list(contract.required_environment),
                "fail_closed": True,
                "schedule_enabled": False,
            },
        )
        for source_id, contract in CONTRACTS.items()
    )
