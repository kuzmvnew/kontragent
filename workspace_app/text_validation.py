"""Shared raw-text safety checks for Workspace identity and display fields."""

from __future__ import annotations

import unicodedata


_FORBIDDEN_CATEGORIES = frozenset({"Cc", "Cf", "Cs"})


def has_forbidden_text_character(value: str) -> bool:
    """Reject controls, invisible format characters, and unpaired surrogates."""

    return any(unicodedata.category(char) in _FORBIDDEN_CATEGORIES for char in value)
