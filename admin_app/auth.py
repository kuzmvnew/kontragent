"""Owner authentication for the private admin console.

The remote mode is deliberately fail-closed: it needs an explicit username,
password hash, session secret, public Host header and trusted proxy address.
No forwarded client address is used for authorization.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Final

SESSION_COOKIE: Final = "nextcompany_admin_session"
HASH_PREFIX: Final = "scrypt-v1"
_SCRYPT_N: Final = 2**15
_SCRYPT_R: Final = 8
_SCRYPT_P: Final = 1
_SCRYPT_MAXMEM: Final = 64 * 1024 * 1024


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _b64decode(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def hash_password(password: str, *, salt: bytes | None = None) -> str:
    """Return the versioned stdlib-scrypt format accepted by the console."""

    if len(password) < 14:
        raise ValueError("admin password must contain at least 14 characters")
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R,
        p=_SCRYPT_P, dklen=32, maxmem=_SCRYPT_MAXMEM,
    )
    return "$".join(
        (HASH_PREFIX, str(_SCRYPT_N), str(_SCRYPT_R), str(_SCRYPT_P), _b64encode(salt), _b64encode(digest))
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
            password.encode("utf-8"), salt=_b64decode(salt), n=params[0],
            r=params[1], p=params[2], dklen=32, maxmem=_SCRYPT_MAXMEM,
        )
        return hmac.compare_digest(actual, _b64decode(expected))
    except (ValueError, TypeError):
        return False


def _csv(name: str, default: str = "") -> tuple[str, ...]:
    return tuple(item.strip() for item in os.getenv(name, default).split(",") if item.strip())


@dataclass(frozen=True)
class AuthConfig:
    mode: str
    username: str | None
    password_hash: str | None
    session_secret: str | None
    session_ttl_seconds: int
    allowed_hosts: tuple[str, ...]
    trusted_proxy_ips: tuple[str, ...]

    @property
    def auth_required(self) -> bool:
        return self.mode == "remote" or bool(self.username and self.password_hash and self.session_secret)

    @property
    def secure_cookie(self) -> bool:
        return self.mode == "remote"


def load_auth_config() -> AuthConfig:
    mode = os.getenv("ADMIN_ACCESS_MODE", "local").strip().lower()
    if mode not in {"local", "remote"}:
        raise RuntimeError("ADMIN_ACCESS_MODE must be local or remote")
    ttl = int(os.getenv("ADMIN_SESSION_TTL_SECONDS", "28800"))
    if not 300 <= ttl <= 86400:
        raise RuntimeError("ADMIN_SESSION_TTL_SECONDS must be between 300 and 86400")
    local_hosts = ("127.0.0.1", "localhost", "testserver")
    allowed_hosts = _csv("ADMIN_ALLOWED_HOSTS") or local_hosts
    trusted = _csv("ADMIN_TRUSTED_PROXY_IPS", "127.0.0.1,::1")
    username = os.getenv("ADMIN_USERNAME") or None
    password_hash = os.getenv("ADMIN_PASSWORD_HASH") or None
    session_secret = os.getenv("ADMIN_SESSION_SECRET") or None
    if mode == "remote":
        if not username or not password_hash or not session_secret:
            raise RuntimeError("remote admin requires username, password hash and session secret")
        if len(session_secret) < 32:
            raise RuntimeError("ADMIN_SESSION_SECRET must contain at least 32 characters")
        if allowed_hosts == local_hosts or any(host == "*" for host in allowed_hosts):
            raise RuntimeError("remote admin requires explicit non-wildcard ADMIN_ALLOWED_HOSTS")
        for address in trusted:
            ipaddress.ip_address(address)
    elif any((username, password_hash, session_secret)) and not all((username, password_hash, session_secret)):
        raise RuntimeError("local authenticated mode requires all credential settings")
    return AuthConfig(
        mode=mode, username=username, password_hash=password_hash,
        session_secret=session_secret, session_ttl_seconds=ttl,
        allowed_hosts=allowed_hosts, trusted_proxy_ips=trusted,
    )


class SessionStore:
    """Process-local allow-list; restart and logout invalidate every affected SID."""

    def __init__(self) -> None:
        self._active: dict[str, int] = {}

    def _cleanup(self, now: int) -> None:
        self._active = {sid: expiry for sid, expiry in self._active.items() if expiry > now}

    def create(self, config: AuthConfig, *, now: int | None = None) -> str:
        now = int(time.time()) if now is None else now
        self._cleanup(now)
        sid = secrets.token_urlsafe(24)
        expiry = now + config.session_ttl_seconds
        self._active[sid] = expiry
        payload = _b64encode(json.dumps(
            {"v": 1, "sub": config.username, "sid": sid, "iat": now, "exp": expiry},
            separators=(",", ":"), sort_keys=True,
        ).encode("utf-8"))
        signature = hmac.new(
            str(config.session_secret).encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
        ).digest()
        return f"{payload}.{_b64encode(signature)}"

    def verify(self, token: str | None, config: AuthConfig, *, now: int | None = None) -> dict | None:
        if not token or not config.session_secret:
            return None
        now = int(time.time()) if now is None else now
        self._cleanup(now)
        try:
            payload, signature = token.split(".", 1)
            expected = hmac.new(
                config.session_secret.encode("utf-8"), payload.encode("ascii"), hashlib.sha256,
            ).digest()
            if not hmac.compare_digest(expected, _b64decode(signature)):
                return None
            claims = json.loads(_b64decode(payload))
            if claims.get("v") != 1 or claims.get("sub") != config.username:
                return None
            if not isinstance(claims.get("exp"), int) or claims["exp"] <= now:
                return None
            if self._active.get(str(claims.get("sid"))) != claims["exp"]:
                return None
            return claims
        except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
            return None

    def revoke(self, token: str | None, config: AuthConfig) -> None:
        claims = self.verify(token, config)
        if claims:
            self._active.pop(str(claims["sid"]), None)


class LoginLimiter:
    """Bounded in-memory limiter without login-name enumeration."""

    def __init__(self, *, attempts: int = 5, window_seconds: int = 300) -> None:
        self.attempts = attempts
        self.window_seconds = window_seconds
        self._failures: dict[str, deque[float]] = defaultdict(deque)

    def allowed(self, key: str, *, now: float | None = None) -> bool:
        now = time.monotonic() if now is None else now
        queue = self._failures[key]
        while queue and queue[0] <= now - self.window_seconds:
            queue.popleft()
        return len(queue) < self.attempts

    def failure(self, key: str, *, now: float | None = None) -> None:
        now = time.monotonic() if now is None else now
        self.allowed(key, now=now)
        self._failures[key].append(now)

    def success(self, key: str) -> None:
        self._failures.pop(key, None)


SESSIONS = SessionStore()
LOGIN_LIMITER = LoginLimiter()
