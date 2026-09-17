"""Low-load source governor with per-source/IP/global budgets and fail-closed circuits."""

from __future__ import annotations

import json
import random
import threading
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Callable


@dataclass(frozen=True)
class SourceRatePolicy:
    min_interval_seconds: float
    max_interval_seconds: float
    concurrency: int = 1
    per_ip_interval_seconds: float | None = None
    global_interval_seconds: float | None = None
    max_retries: int = 2
    backoff_seconds: float = 3
    circuit_failures: int = 3
    circuit_cooldown_seconds: float = 300
    multi_ip_authorized: bool = False
    max_session_requests: int = 100
    max_daily_requests: int = 1000


FIRMOTEKA_BASELINE_POLICY = SourceRatePolicy(
    min_interval_seconds=6,
    max_interval_seconds=12,
    concurrency=1,
    per_ip_interval_seconds=6,
    global_interval_seconds=0,
    max_retries=2,
    backoff_seconds=6,
    circuit_failures=3,
    circuit_cooldown_seconds=900,
    multi_ip_authorized=True,
    max_session_requests=600,
    max_daily_requests=5000,
)


@dataclass
class _Circuit:
    failures: int = 0
    opened_at: float | None = None


class CircuitOpenError(RuntimeError):
    pass


class SourceBudgetExceededError(RuntimeError):
    pass


class SourceRateGovernor:
    """Thread-safe governor; processes can share progress through checkpoints.

    Distributed workers must use stable worker/IP identities and stable shards.
    The governor never rotates identity to evade a block.
    """

    def __init__(
        self, policies: dict[str, SourceRatePolicy], *,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        random_uniform: Callable[[float, float], float] = random.uniform,
        current_day: Callable[[], date] = lambda: datetime.now(timezone.utc).date(),
    ):
        self.policies = policies
        self.clock = clock
        self.sleep = sleep
        self.random_uniform = random_uniform
        self.current_day = current_day
        self._lock = threading.RLock()
        self._source_next: dict[str, float] = defaultdict(float)
        self._ip_next: dict[tuple[str, str], float] = defaultdict(float)
        self._global_next = 0.0
        self._active: dict[str, int] = defaultdict(int)
        self._circuits: dict[str, _Circuit] = defaultdict(_Circuit)
        self._cache: dict[tuple[str, str], Any] = {}
        self._session_requests: dict[str, int] = defaultdict(int)
        self._daily_requests: dict[tuple[str, date], int] = defaultdict(int)

    def _assert_worker(self, source: str, worker_id: str, egress_ip: str, shard: str) -> None:
        if not all((worker_id, egress_ip, shard)):
            raise ValueError("worker_id, stable egress_ip and stable shard are required")
        policy = self.policies[source]
        if worker_id != "single" and not policy.multi_ip_authorized:
            raise ValueError(f"multi-IP is not authorized for source {source}")

    def acquire(self, source: str, *, worker_id: str = "single", egress_ip: str = "local", shard: str = "0") -> None:
        self._assert_worker(source, worker_id, egress_ip, shard)
        policy = self.policies[source]
        with self._lock:
            circuit = self._circuits[source]
            now = self.clock()
            if circuit.opened_at is not None:
                if now - circuit.opened_at < policy.circuit_cooldown_seconds:
                    raise CircuitOpenError(f"circuit is open for {source}")
                circuit.failures = 0
                circuit.opened_at = None
            if self._active[source] >= policy.concurrency:
                raise RuntimeError(f"concurrency budget exhausted for {source}")
            day = self.current_day()
            if self._session_requests[source] >= policy.max_session_requests:
                raise SourceBudgetExceededError(f"session request budget exhausted for {source}")
            if self._daily_requests[(source, day)] >= policy.max_daily_requests:
                raise SourceBudgetExceededError(f"daily request budget exhausted for {source}")
            wait_until = max(self._source_next[source], self._ip_next[(source, egress_ip)], self._global_next)
            delay = max(0.0, wait_until - now)
            if delay:
                self.sleep(delay)
                now = self.clock()
            jitter = self.random_uniform(policy.min_interval_seconds, policy.max_interval_seconds)
            self._source_next[source] = now + jitter
            self._ip_next[(source, egress_ip)] = now + (policy.per_ip_interval_seconds or jitter)
            self._global_next = now + (policy.global_interval_seconds or 0)
            self._active[source] += 1
            self._session_requests[source] += 1
            self._daily_requests[(source, day)] += 1

    def release(self, source: str, *, success: bool, retry_after: float | None = None) -> None:
        policy = self.policies[source]
        with self._lock:
            self._active[source] = max(0, self._active[source] - 1)
            circuit = self._circuits[source]
            if success:
                circuit.failures = 0
                circuit.opened_at = None
                return
            circuit.failures += 1
            if retry_after is not None:
                self._source_next[source] = max(self._source_next[source], self.clock() + retry_after)
            if circuit.failures >= policy.circuit_failures:
                circuit.opened_at = self.clock()

    def run(self, source: str, key: str, call: Callable[[], Any], **identity: str) -> Any:
        cache_key = (source, key)
        if cache_key in self._cache:
            return self._cache[cache_key]
        policy = self.policies[source]
        last_error: Exception | None = None
        for attempt in range(policy.max_retries + 1):
            self.acquire(source, **identity)
            try:
                value = call()
                self.release(source, success=True)
                self._cache[cache_key] = value
                return value
            except Exception as error:
                last_error = error
                retry_after = getattr(error, "retry_after", None)
                self.release(source, success=False, retry_after=retry_after)
                if attempt < policy.max_retries:
                    self.sleep(retry_after or policy.backoff_seconds * (2 ** attempt))
        raise last_error or RuntimeError("source call failed")

    def checkpoint(self, path: Path, completed: dict[str, list[str]]) -> None:
        payload = {"version": 1, "completed": completed}
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        temporary.replace(path)

    @staticmethod
    def resume(path: Path) -> dict[str, list[str]]:
        if not path.exists():
            return {}
        return json.loads(path.read_text(encoding="utf-8")).get("completed", {})
