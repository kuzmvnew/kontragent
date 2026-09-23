"""Public enums shared by the projection and Card v2 boundary."""

from enum import StrEnum


class PublicUIState(StrEnum):
    """Closed state vocabulary understood by Card v2 components."""

    FOUND = "FOUND"
    NOT_FOUND = "NOT_FOUND"
    NOT_APPLICABLE = "NOT_APPLICABLE"
    PARTIAL = "PARTIAL"
    SOURCE_UNAVAILABLE = "SOURCE_UNAVAILABLE"
    STALE = "STALE"
    UNKNOWN = "UNKNOWN"
    CONFLICTING_EVIDENCE = "CONFLICTING_EVIDENCE"
    ERROR = "ERROR"


class CardActionKind(StrEnum):
    GET_REPORT = "GET_REPORT"
    WATCH = "WATCH"
    SAVE = "SAVE"
