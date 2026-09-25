"""Secret-safe values shared by persistence, notifications and repair packages."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit


SECRET_KEY = re.compile(r"(?i)(password|passwd|secret|token|cookie|authorization|api[_-]?key|dsn|database_url)")
SECRET_VALUE = re.compile(
    r"(?i)(postgres(?:ql)?(?:\+\w+)?://[^\s]+|(?:password|token|secret|api[_-]?key)\s*[=:]\s*[^\s,;]+|Bearer\s+[^\s]+)"
)


def safe_text(value: object, *, limit: int = 1000) -> str:
    text = str(value or "")
    text = SECRET_VALUE.sub("[REDACTED]", text)
    words: list[str] = []
    for word in text.split():
        parsed = urlsplit(word.rstrip(".,)"))
        if parsed.scheme in {"http", "https"} and parsed.netloc and parsed.query:
            suffix = word[len(word.rstrip(".,)")):]
            word = parsed._replace(query="", fragment="").geturl() + suffix
        words.append(word)
    return " ".join(words)[:limit]


def safe_value(value: Any, *, depth: int = 0) -> Any:
    if depth > 5:
        return "[TRUNCATED]"
    if isinstance(value, dict):
        return {
            str(key)[:100]: (
                "[REDACTED]" if SECRET_KEY.search(str(key)) else safe_value(item, depth=depth + 1)
            )
            for key, item in list(value.items())[:100]
        }
    if isinstance(value, (list, tuple)):
        return [safe_value(item, depth=depth + 1) for item in list(value)[:100]]
    if isinstance(value, str):
        return safe_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return safe_text(value)

