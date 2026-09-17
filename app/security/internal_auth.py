"""Minimal fail-closed authentication for mutation and internal routes."""

from __future__ import annotations

import hmac
import os

from fastapi import Header, HTTPException, status


TOKEN_ENV = "KONTRAGENT_INTERNAL_TOKEN"


def require_internal_token(
    authorization: str | None = Header(default=None),
    x_internal_token: str | None = Header(default=None),
) -> None:
    """Require an explicitly configured environment secret.

    ``X-Internal-Token`` remains available for simple local tooling while the
    standard ``Authorization: Bearer`` form is preferred.  Missing server
    configuration is a service failure, never an authentication bypass.
    """

    expected = os.getenv(TOKEN_ENV)
    if not expected:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Internal access is not configured",
        )

    supplied = x_internal_token
    if authorization:
        scheme, _, credentials = authorization.partition(" ")
        if scheme.casefold() == "bearer" and credentials:
            supplied = credentials

    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid internal credential",
            headers={"WWW-Authenticate": "Bearer"},
        )
