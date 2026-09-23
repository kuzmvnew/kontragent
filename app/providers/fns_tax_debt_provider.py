"""Strict official-page discovery for the S02 controlled live pilot."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timezone
import html
from html.parser import HTMLParser
from pathlib import Path
import re
from urllib.parse import urljoin, urlparse

import httpx

from app.sources.fns_tax_debt import OFFICIAL_FILE_BASE, OFFICIAL_SOURCE_PAGE
from app.worker.errors import InvalidDataError, TemporaryInfrastructureError


DATASET_ID = "7707329152-debtam"
_ARTIFACT_RE = re.compile(r"data-(\d{8})-structure-(\d{8})\.zip\Z")
_XSD_RE = re.compile(r"structure-(\d{8})\.xsd\Z")
_RU_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


class TaxDebtDiscoveryError(InvalidDataError):
    pass


@dataclass(frozen=True)
class TaxDebtOfficialRelease:
    discovery_page_url: str
    artifact_url: str
    xsd_url: str
    source_as_of: datetime
    data_as_of: date
    official_actual_until: date
    metadata: dict[str, str]

    def as_dict(self) -> dict:
        return asdict(self)


@dataclass(frozen=True)
class TaxDebtDiscovery:
    release: TaxDebtOfficialRelease
    changed: bool
    discovered_at: datetime


class _OfficialTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self.row_links: list[list[str]] = []
        self._row: list[str] | None = None
        self._links: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        tag = tag.lower()
        if tag == "tr":
            self._row = []
            self._links = []
        elif tag in {"td", "th"} and self._row is not None:
            self._cell = []
        elif tag == "a":
            href = dict(attrs).get("href")
            if href and self._links is not None:
                self._links.append(html.unescape(href).strip())

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            value = " ".join(data.split())
            if value:
                self._cell.append(value)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in {"td", "th"} and self._cell is not None:
            assert self._row is not None
            self._row.append(" ".join(self._cell))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
                self.row_links.append(self._links or [])
            self._row = None
            self._links = None


def _parse_ru_date(value: str, *, field: str) -> date:
    match = _RU_DATE_RE.search(value)
    if match is None:
        raise TaxDebtDiscoveryError(f"official S02 page has no valid {field}")
    try:
        return datetime.strptime(match.group(1), "%d.%m.%Y").date()
    except ValueError as error:
        raise TaxDebtDiscoveryError(f"official S02 page has invalid {field}") from error


def _strict_official_url(value: str, *, kind: str) -> tuple[str, re.Match[str]]:
    absolute = urljoin(OFFICIAL_SOURCE_PAGE, value)
    parsed = urlparse(absolute)
    if (
        parsed.scheme != "https"
        or parsed.hostname != "file.nalog.ru"
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.query
        or parsed.fragment
        or not parsed.path.startswith(urlparse(OFFICIAL_FILE_BASE).path)
    ):
        raise TaxDebtDiscoveryError(f"S02 {kind} URL is not an approved official URL")
    filename = Path(parsed.path).name
    pattern = _ARTIFACT_RE if kind == "artifact" else _XSD_RE
    match = pattern.fullmatch(filename)
    if match is None:
        raise TaxDebtDiscoveryError(f"S02 {kind} URL has an unsupported filename")
    return absolute, match


def parse_tax_debt_discovery_page(page_html: str) -> TaxDebtOfficialRelease:
    """Parse only the one approved dataset page; never synthesize file URLs."""

    parser = _OfficialTableParser()
    parser.feed(page_html)
    metadata = {
        row[-2].strip(): row[-1].strip()
        for row in parser.rows
        if len(row) >= 2 and row[-2].strip()
    }
    if metadata.get("Идентификационный номер") != DATASET_ID:
        raise TaxDebtDiscoveryError("official S02 dataset identifier is missing or changed")

    current_artifact_links: list[str] = []
    current_xsd_links: list[str] = []
    for row, links in zip(parser.rows, parser.row_links):
        if len(row) < 2:
            continue
        label = row[-2].strip()
        if label == "Гиперссылка (URL) на набор":
            current_artifact_links.extend(
                links
                or ([row[-1].strip()] if row[-1].strip().startswith("https://") else [])
            )
        elif label == "Описание структуры набора данных":
            current_xsd_links.extend(
                links
                or ([row[-1].strip()] if row[-1].strip().startswith("https://") else [])
            )

    artifacts: list[tuple[str, re.Match[str]]] = []
    schemas: list[tuple[str, re.Match[str]]] = []
    for href in current_artifact_links:
        absolute, match = _strict_official_url(href, kind="artifact")
        artifacts.append((absolute, match))
    for href in current_xsd_links:
        try:
            absolute, match = _strict_official_url(href, kind="xsd")
        except TaxDebtDiscoveryError as error:
            raise TaxDebtDiscoveryError(
                "official S02 page exposes an invalid current XSD URL"
            ) from error
        schemas.append((absolute, match))

    if len(artifacts) != 1:
        raise TaxDebtDiscoveryError("official S02 page must expose exactly one current artifact")
    artifact_url, artifact_match = artifacts[0]
    structure_version = artifact_match.group(2)
    matching_schemas = [
        (url, match) for url, match in schemas if match.group(1) == structure_version
    ]
    if len(matching_schemas) != 1:
        raise TaxDebtDiscoveryError(
            "official S02 page must expose the artifact's exact XSD URL"
        )
    xsd_url = matching_schemas[0][0]

    modified = _parse_ru_date(
        metadata.get("Дата последнего внесения изменений", ""),
        field="source_as_of",
    )
    artifact_date = datetime.strptime(artifact_match.group(1), "%Y%m%d").date()
    if modified != artifact_date:
        raise TaxDebtDiscoveryError(
            "official S02 artifact date differs from metadata modification date"
        )
    data_as_of = _parse_ru_date(
        metadata.get("Содержание последнего изменения", ""),
        field="data_as_of",
    )
    actual_until = _parse_ru_date(
        metadata.get("Дата актуальности", ""),
        field="official_actual_until",
    )
    return TaxDebtOfficialRelease(
        discovery_page_url=OFFICIAL_SOURCE_PAGE,
        artifact_url=artifact_url,
        xsd_url=xsd_url,
        source_as_of=datetime.combine(modified, time.min, tzinfo=timezone.utc),
        data_as_of=data_as_of,
        official_actual_until=actual_until,
        metadata=metadata,
    )


def release_fingerprint(release: TaxDebtOfficialRelease) -> tuple[object, ...]:
    return (
        release.artifact_url,
        release.xsd_url,
        release.source_as_of,
        release.data_as_of,
        release.official_actual_until,
    )


def validate_tax_debt_release(release: TaxDebtOfficialRelease) -> None:
    if release.discovery_page_url != OFFICIAL_SOURCE_PAGE:
        raise TaxDebtDiscoveryError("S02 discovery page URL is not pinned")
    _, artifact_match = _strict_official_url(release.artifact_url, kind="artifact")
    _, xsd_match = _strict_official_url(release.xsd_url, kind="xsd")
    if artifact_match.group(2) != xsd_match.group(1):
        raise TaxDebtDiscoveryError("S02 artifact and XSD structure versions differ")
    artifact_date = datetime.strptime(artifact_match.group(1), "%Y%m%d").date()
    if release.source_as_of.tzinfo is None or release.source_as_of.utcoffset() is None:
        raise TaxDebtDiscoveryError("S02 source_as_of must contain a timezone")
    if release.source_as_of.astimezone(timezone.utc).date() != artifact_date:
        raise TaxDebtDiscoveryError("S02 artifact and source_as_of dates differ")


def discover_tax_debt_release(
    page_html: str,
    *,
    discovered_at: datetime,
    previous: TaxDebtOfficialRelease | None = None,
) -> TaxDebtDiscovery:
    if discovered_at.tzinfo is None or discovered_at.utcoffset() is None:
        raise ValueError("discovered_at must contain a timezone")
    release = parse_tax_debt_discovery_page(page_html)
    validate_tax_debt_release(release)
    return TaxDebtDiscovery(
        release=release,
        changed=previous is None or release_fingerprint(release) != release_fingerprint(previous),
        discovered_at=discovered_at.astimezone(timezone.utc),
    )


class FnsTaxDebtOfficialClient:
    """One-page discovery and exact discovered-file retrieval; no crawling."""

    def __init__(self, client: httpx.Client | None = None) -> None:
        self.client = client

    def _client(self) -> httpx.Client:
        return self.client or httpx.Client(
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": "Kontragent/1.0 (+S02-controlled-live-pilot)"},
        )

    def discover(
        self,
        *,
        discovered_at: datetime,
        previous: TaxDebtOfficialRelease | None = None,
    ) -> TaxDebtDiscovery:
        owns_client = self.client is None
        client = self._client()
        try:
            response = client.get(OFFICIAL_SOURCE_PAGE)
            if response.status_code != 200:
                raise TemporaryInfrastructureError(
                    f"official S02 discovery HTTP {response.status_code}"
                )
            return discover_tax_debt_release(
                response.text,
                discovered_at=discovered_at,
                previous=previous,
            )
        except (TaxDebtDiscoveryError, TemporaryInfrastructureError):
            raise
        except httpx.HTTPError as error:
            raise TemporaryInfrastructureError(
                f"official S02 discovery request failed: {error}"
            ) from error
        finally:
            if owns_client:
                client.close()

    def download_exact(self, url: str, destination: Path, *, kind: str) -> Path:
        approved_url, _ = _strict_official_url(url, kind=kind)
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        owns_client = self.client is None
        client = self._client()
        try:
            with client.stream("GET", approved_url) as response:
                if response.status_code != 200:
                    raise TemporaryInfrastructureError(
                        f"official S02 {kind} HTTP {response.status_code}"
                    )
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if chunk:
                            output.write(chunk)
            temporary.replace(destination)
            return destination
        except TemporaryInfrastructureError:
            temporary.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise TemporaryInfrastructureError(
                f"official S02 {kind} download failed: {error}"
            ) from error
        finally:
            if owns_client:
                client.close()
