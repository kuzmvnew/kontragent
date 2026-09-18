"""Runner registry and precedence-aware company fact resolver."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable, Iterable

from app.contracts.source_architecture import (
    NormalizedCheckResult,
    NormalizedResultStatus,
    SourceClass,
)
from app.services.source_capability_catalog import CATALOG, SourceCapabilityCatalog

Runner = Callable[..., NormalizedCheckResult]
SOURCE_RANK = {
    SourceClass.OFFICIAL_DIRECT: 0,
    SourceClass.OFFICIAL_DOWNLOADED_DATASET: 1,
    SourceClass.AUTHORIZED_BRIDGE: 2,
    SourceClass.DISCOVERY_ONLY: 3,
    SourceClass.POLICY_RULE: 4,
}
RESULT_RANK = {
    NormalizedResultStatus.FOUND: 0,
    NormalizedResultStatus.NOT_FOUND: 1,
    NormalizedResultStatus.NOT_APPLICABLE: 2,
    NormalizedResultStatus.PARTIAL: 3,
    NormalizedResultStatus.UNAVAILABLE: 4,
    NormalizedResultStatus.ERROR: 5,
}


class SourceRunnerRegistry:
    def __init__(self):
        self._runners: dict[str, Runner] = {}

    def register(self, code: str, runner: Runner) -> None:
        if code in self._runners:
            raise ValueError(f"runner already registered: {code}")
        self._runners[code] = runner

    def get(self, code: str) -> Runner:
        try:
            return self._runners[code]
        except KeyError as error:
            raise KeyError(f"runner is not registered: {code}") from error

    def codes(self) -> tuple[str, ...]:
        return tuple(sorted(self._runners))


class SourceResolver:
    def __init__(self, catalog: SourceCapabilityCatalog = CATALOG):
        self.catalog = catalog

    def resolve(self, results: Iterable[NormalizedCheckResult]) -> dict[str, NormalizedCheckResult]:
        grouped: dict[str, list[NormalizedCheckResult]] = defaultdict(list)
        for item in results:
            grouped[item.check_code].append(item)
        resolved: dict[str, NormalizedCheckResult] = {}
        for code, candidates in grouped.items():
            capability = self.catalog.get(code)
            allowed = []
            for item in candidates:
                if item.source_class == SourceClass.AUTHORIZED_BRIDGE:
                    if not capability.bridge_allowed:
                        continue
                    if item.result == NormalizedResultStatus.NOT_FOUND and not capability.negative_bridge_allowed:
                        continue
                allowed.append(item)
            if not allowed:
                allowed = candidates
            chosen = min(
                allowed,
                key=lambda item: (
                    0 if item.result in {
                        NormalizedResultStatus.FOUND,
                        NormalizedResultStatus.NOT_FOUND,
                        NormalizedResultStatus.NOT_APPLICABLE,
                    } else 1 if item.result == NormalizedResultStatus.PARTIAL else 2,
                    SOURCE_RANK[item.source_class], RESULT_RANK[item.result],
                    -item.coverage, -item.confidence,
                ),
            )
            provenance = tuple(dict.fromkeys(item.source_code for item in candidates))
            resolved[code] = chosen.model_copy(update={"resolved_by": provenance})
        return resolved


DEFAULT_RUNNER_REGISTRY = SourceRunnerRegistry()
DEFAULT_RESOLVER = SourceResolver()
