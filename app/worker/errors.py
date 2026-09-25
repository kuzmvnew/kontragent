"""Explicit failure taxonomy used by the retry policy."""

from enum import StrEnum


class FailureKind(StrEnum):
    ACCESS_REQUIRED = "access_required"
    TIMEOUT = "timeout"
    NETWORK = "network_failure"
    TEMPORARY_INFRASTRUCTURE = "temporary_infrastructure"
    INVALID_DATA = "invalid_data"
    SCHEMA_MISMATCH = "schema_mismatch"
    LEGAL_BLOCK = "legal_block"
    HANDLER_MISSING = "handler_missing"
    HANDLER_FAILURE = "handler_failure"


class WorkerFoundationError(RuntimeError):
    kind = FailureKind.HANDLER_FAILURE
    retryable = False


class AccessRequiredError(WorkerFoundationError):
    """Official transport exists but owner credentials are not configured."""

    kind = FailureKind.ACCESS_REQUIRED


class WorkerTimeoutError(WorkerFoundationError):
    kind = FailureKind.TIMEOUT
    retryable = True


class WorkerNetworkError(WorkerFoundationError):
    kind = FailureKind.NETWORK
    retryable = True


class TemporaryInfrastructureError(WorkerFoundationError):
    kind = FailureKind.TEMPORARY_INFRASTRUCTURE
    retryable = True


class InvalidDataError(WorkerFoundationError):
    kind = FailureKind.INVALID_DATA


class SchemaMismatchError(WorkerFoundationError):
    kind = FailureKind.SCHEMA_MISMATCH


class LegalBlockError(WorkerFoundationError):
    kind = FailureKind.LEGAL_BLOCK


class HandlerNotRegisteredError(WorkerFoundationError):
    kind = FailureKind.HANDLER_MISSING


class LiveHandlerProhibitedError(WorkerFoundationError):
    kind = FailureKind.LEGAL_BLOCK


class LeaseConflictError(WorkerFoundationError):
    kind = FailureKind.TEMPORARY_INFRASTRUCTURE


class LeaseLostError(WorkerFoundationError):
    kind = FailureKind.TEMPORARY_INFRASTRUCTURE


class IdempotencyConflictError(WorkerFoundationError):
    kind = FailureKind.INVALID_DATA


class PublicationValidationError(InvalidDataError):
    pass
