"""Customer authentication primitives for Workspace P0.

This module is intentionally separate from admin_app.auth. Customer sessions
are opaque, revocable database records and never authorize owner-console
operations.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import unicodedata
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from urllib.parse import unquote, urlsplit
from uuid import UUID

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models.workspace import CustomerSession, CustomerUser, Workspace, WorkspaceMembership


SESSION_COOKIE = "nextcompany_session"
CSRF_COOKIE = "nextcompany_csrf"
LOGIN_CSRF_COOKIE = "nextcompany_login_csrf"
HASH_PREFIX = "scrypt-v1"
_SCRYPT_N = 2**15
_SCRYPT_R = 8
_SCRYPT_P = 1
_SCRYPT_MAXMEM = 64 * 1024 * 1024
DEFAULT_SESSION_TTL = timedelta(hours=8)
_RETURN_TO_MAX_LENGTH = 1000
_RETURN_TO_DECODE_LIMIT = 4
_ASCII_HEX_DIGITS = frozenset("0123456789ABCDEFabcdef")


@dataclass(frozen=True)
class SessionPrincipal:
    session_id: UUID
    user_id: UUID
    email: str
    active_workspace_id: UUID | None
    expires_at: datetime


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def normalize_email(value: str) -> str:
    email = " ".join(str(value or "").strip().split()).casefold()
    if len(email) > 320 or re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email) is None:
        raise ValueError("invalid email")
    return email


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    if len(password) < 14:
        raise ValueError("password must contain at least 14 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"),
        salt=salt,
        n=_SCRYPT_N,
        r=_SCRYPT_R,
        p=_SCRYPT_P,
        dklen=32,
        maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (
            HASH_PREFIX,
            str(_SCRYPT_N),
            str(_SCRYPT_R),
            str(_SCRYPT_P),
            _b64encode(salt),
            _b64encode(digest),
        )
    )


def verify_password(password: str, encoded: str) -> bool:
    try:
        prefix, n, r, p, salt, expected = encoded.split("$", 5)
        if prefix != HASH_PREFIX:
            return False
        params = (int(n), int(r), int(p))
        if params != (_SCRYPT_N, _SCRYPT_R, _SCRYPT_P):
            return False
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=_b64decode(salt),
            n=params[0],
            r=params[1],
            p=params[2],
            dklen=32,
            maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def new_login_csrf() -> str:
    return secrets.token_urlsafe(32)


def login_csrf_valid(cookie_value: str | None, form_value: str | None) -> bool:
    if not cookie_value or not form_value:
        return False
    return hmac.compare_digest(cookie_value, form_value)


def create_customer_session(
    session: Session,
    *,
    user: CustomerUser,
    active_workspace_id: UUID | None = None,
    now: datetime | None = None,
    ttl: timedelta = DEFAULT_SESSION_TTL,
) -> tuple[str, str, CustomerSession]:
    now = now or datetime.now(UTC)
    if ttl <= timedelta(0):
        raise ValueError("session ttl must be positive")
    if user.status != "active":
        raise PermissionError("active customer user is required")
    if active_workspace_id is not None:
        membership = session.scalar(
            sa.select(WorkspaceMembership.id)
            .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
            .where(
                WorkspaceMembership.user_id == user.id,
                WorkspaceMembership.workspace_id == active_workspace_id,
                WorkspaceMembership.status == "active",
                Workspace.status == "active",
            )
        )
        if membership is None:
            raise PermissionError("active workspace membership is required")
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(32)
    record = CustomerSession(
        token_hash=_token_hash(token),
        csrf_hash=_token_hash(csrf),
        user_id=user.id,
        active_workspace_id=active_workspace_id,
        created_at=now,
        expires_at=now + ttl,
    )
    session.add(record)
    session.flush()
    return token, csrf, record


def load_principal(
    session: Session,
    token: str | None,
    *,
    now: datetime | None = None,
) -> SessionPrincipal | None:
    if not token:
        return None
    now = now or datetime.now(UTC)
    row = session.scalar(
        sa.select(CustomerSession).where(
            CustomerSession.token_hash == _token_hash(token),
            CustomerSession.revoked_at.is_(None),
            CustomerSession.expires_at > now,
        )
    )
    if row is None:
        return None
    user = session.get(CustomerUser, row.user_id)
    if user is None or user.status != "active":
        return None
    return SessionPrincipal(
        session_id=row.id,
        user_id=user.id,
        email=user.email,
        active_workspace_id=row.active_workspace_id,
        expires_at=row.expires_at,
    )


def verify_session_csrf(
    session: Session,
    principal: SessionPrincipal,
    *,
    cookie_value: str | None,
    form_value: str | None,
) -> bool:
    if not cookie_value or not form_value or not hmac.compare_digest(cookie_value, form_value):
        return False
    record = session.get(CustomerSession, principal.session_id)
    if record is None or record.revoked_at is not None:
        return False
    return hmac.compare_digest(record.csrf_hash, _token_hash(form_value))


def revoke_session(
    session: Session,
    principal: SessionPrincipal,
    *,
    now: datetime | None = None,
) -> None:
    now = now or datetime.now(UTC)
    record = session.get(CustomerSession, principal.session_id)
    if record is not None and record.revoked_at is None:
        record.revoked_at = now


def active_membership_workspaces(session: Session, user_id: UUID) -> tuple[UUID, ...]:
    rows = session.scalars(
        sa.select(WorkspaceMembership.workspace_id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == user_id,
            WorkspaceMembership.status == "active",
            Workspace.status == "active",
        )
        .order_by(WorkspaceMembership.created_at, WorkspaceMembership.workspace_id)
    ).all()
    return tuple(rows)


def set_active_workspace(
    session: Session,
    principal: SessionPrincipal,
    workspace_id: UUID,
) -> None:
    membership = session.scalar(
        sa.select(WorkspaceMembership.id)
        .join(Workspace, Workspace.id == WorkspaceMembership.workspace_id)
        .where(
            WorkspaceMembership.user_id == principal.user_id,
            WorkspaceMembership.workspace_id == workspace_id,
            WorkspaceMembership.status == "active",
            Workspace.status == "active",
        )
    )
    if membership is None:
        raise PermissionError("workspace membership is not active")
    record = session.get(CustomerSession, principal.session_id)
    if record is None or record.revoked_at is not None:
        raise PermissionError("session is not active")
    record.active_workspace_id = workspace_id


def _contains_unicode_control(value: str) -> bool:
    return any(
        unicodedata.category(character).startswith("C") for character in value
    )


def _has_invalid_percent_escape(value: str) -> bool:
    index = 0
    while index < len(value):
        if value[index] != "%":
            index += 1
            continue
        if index + 2 >= len(value):
            return True
        if (
            value[index + 1] not in _ASCII_HEX_DIGITS
            or value[index + 2] not in _ASCII_HEX_DIGITS
        ):
            return True
        index += 3
    return False


def _query_is_unsafe(value: str) -> bool:
    candidate = value
    for _ in range(_RETURN_TO_DECODE_LIMIT):
        if (
            _has_invalid_percent_escape(candidate)
            or _contains_unicode_control(candidate)
        ):
            return True
        decoded = unquote(candidate, errors="strict")
        if _contains_unicode_control(decoded):
            return True
        if decoded == candidate:
            return False
        candidate = decoded
    return True


def safe_return_to(value: str | None, *, default: str = "/app") -> str:
    try:
        raw = "" if value is None else str(value)
        if (
            not raw
            or len(raw) > _RETURN_TO_MAX_LENGTH
            or raw != raw.strip()
            or raw.startswith("//")
            or "\\" in raw
            or _contains_unicode_control(raw)
        ):
            return default

        parsed = urlsplit(raw)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            return default

        path = parsed.path
        if "//" in path or (path != "/app" and not path.startswith("/app/")):
            return default

        candidate = path
        for _ in range(_RETURN_TO_DECODE_LIMIT):
            if (
                _has_invalid_percent_escape(candidate)
                or _contains_unicode_control(candidate)
                or "\\" in candidate
            ):
                return default
            if any(segment in {".", ".."} for segment in candidate.split("/")):
                return default

            decoded = unquote(candidate, errors="strict")
            if _contains_unicode_control(decoded):
                return default
            if decoded == candidate:
                break
            if decoded.count("/") != candidate.count("/") or "\\" in decoded:
                return default
            if any(segment in {".", ".."} for segment in decoded.split("/")):
                return default
            candidate = decoded
        else:
            return default

        if "%" in candidate:
            return default
        if "//" in candidate or (
            candidate != "/app" and not candidate.startswith("/app/")
        ):
            return default
        if any(segment in {".", ".."} for segment in candidate.split("/")):
            return default
        if parsed.query and _query_is_unsafe(parsed.query):
            return default
    except (TypeError, ValueError, UnicodeError, OverflowError):
        return default
    return raw
