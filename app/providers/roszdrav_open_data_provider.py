"""Official open-data transport for Roszdravnadzor licence snapshots.

The existing on-demand W1-004 provider remains intentionally untouched.  This
provider is limited to the three public bulk XML licence datasets and their
published XSD files.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone
from hashlib import sha256
from pathlib import PurePosixPath
import re
from typing import Mapping
from urllib.parse import urljoin, urlparse

import httpx
from lxml import html


REQUEST_TIMEOUT_SECONDS = 120.0
OFFICIAL_HOSTS = frozenset(
    {
        "roszdravnadzor.gov.ru",
        "www.roszdravnadzor.gov.ru",
    }
)
_ARCHIVE_NAME = re.compile(r"data-(?P<date>[0-9]{8})-structure-[0-9]{8}\.zip$")
_XSD_NAME = re.compile(r"structure-[0-9]{8}\.xsd$")


class RoszdravOpenDataError(Exception):
    def __init__(
        self,
        *,
        kind: str,
        message: str,
        http_status: int | None = None,
    ):
        super().__init__(message)
        self.kind = kind
        self.message = message
        self.http_status = http_status


@dataclass(frozen=True)
class RoszdravOpenDataRelease:
    source_page_url: str
    artifact_url: str
    xsd_url: str
    source_data_date: date
    actual_until: date
    discovered_at: datetime
    provenance: str

    @property
    def identity(self) -> str:
        return sha256(
            "\n".join(
                (
                    self.artifact_url,
                    self.xsd_url,
                    self.source_data_date.isoformat(),
                )
            ).encode("utf-8")
        ).hexdigest()

    def as_metadata(self) -> dict[str, str]:
        return {
            "source_page_url": self.source_page_url,
            "artifact_url": self.artifact_url,
            "xsd_url": self.xsd_url,
            "source_data_date": self.source_data_date.isoformat(),
            "actual_until": self.actual_until.isoformat(),
            "discovered_at": self.discovered_at.isoformat(),
            "provenance": self.provenance,
            "release_identity": self.identity,
        }


def _official_url(value: str, *, base_url: str) -> str:
    result = urljoin(base_url.rstrip("/") + "/", value)
    parsed = urlparse(result)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() not in OFFICIAL_HOSTS:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор опубликовал ссылку вне официального HTTPS-host",
        )
    return result


def _parse_date(value: str, *, field: str) -> date:
    try:
        return datetime.strptime(value.strip(), "%d.%m.%Y").date()
    except ValueError as error:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message=f"Росздравнадзор: некорректная дата {field}",
        ) from error


def _field(rows: Mapping[str, str], label: str) -> str:
    for key, value in rows.items():
        if label in key:
            return value
    raise RoszdravOpenDataError(
        kind="invalid_response",
        message=f"Росздравнадзор: в паспорте отсутствует поле {label}",
    )


def parse_open_data_release(
    content: bytes,
    *,
    source_page_url: str,
    discovered_at: datetime,
) -> RoszdravOpenDataRelease:
    """Parse the official dataset passport without guessing file names."""

    if discovered_at.tzinfo is None or discovered_at.utcoffset() is None:
        raise ValueError("discovered_at must contain a timezone")
    try:
        document = html.fromstring(content.decode("utf-8"))
    except (TypeError, UnicodeDecodeError, ValueError) as error:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор вернул некорректную HTML-страницу паспорта",
        ) from error

    rows: dict[str, str] = {}
    for row in document.xpath("//tr"):
        cells = [" ".join(cell.text_content().split()) for cell in row.xpath("./th|./td")]
        if len(cells) >= 3 and cells[-2] and cells[-1]:
            rows[cells[-2]] = cells[-1]
        elif len(cells) == 2 and cells[0] and cells[1]:
            rows[cells[0]] = cells[1]

    links = [str(value).strip() for value in document.xpath("//a/@href") if value]
    artifact_hrefs = [
        value
        for value in links
        if _ARCHIVE_NAME.fullmatch(PurePosixPath(urlparse(value).path).name)
    ]
    xsd_hrefs = [
        value
        for value in links
        if _XSD_NAME.fullmatch(PurePosixPath(urlparse(value).path).name)
    ]
    if not artifact_hrefs or not xsd_hrefs:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор: passport не содержит текущие ZIP/XSD",
        )

    # The first matching links are the current file and current structure;
    # historical versions follow them on the official page.
    artifact_url = _official_url(artifact_hrefs[0], base_url=source_page_url)
    xsd_url = _official_url(xsd_hrefs[0], base_url=source_page_url)
    filename = PurePosixPath(urlparse(artifact_url).path).name
    match = _ARCHIVE_NAME.fullmatch(filename)
    if match is None:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор: unexpected archive filename",
        )
    filename_date = datetime.strptime(match.group("date"), "%Y%m%d").date()
    changed_at = _parse_date(
        _field(rows, "Дата последнего внесения изменений"),
        field="изменения",
    )
    actual_until = _parse_date(
        _field(rows, "Дата актуальности набора данных"),
        field="актуальности",
    )
    if filename_date != changed_at:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор: дата ZIP расходится с датой изменения паспорта",
        )
    if actual_until < changed_at:
        raise RoszdravOpenDataError(
            kind="invalid_response",
            message="Росздравнадзор: дата актуальности раньше даты выпуска",
        )
    return RoszdravOpenDataRelease(
        source_page_url=source_page_url,
        artifact_url=artifact_url,
        xsd_url=xsd_url,
        source_data_date=filename_date,
        actual_until=actual_until,
        discovered_at=discovered_at.astimezone(timezone.utc),
        provenance=_field(rows, "Содержание последнего изменения"),
    )


class RoszdravOpenDataProvider:
    def __init__(self, client=None):
        self._external_client = client

    def _client(self):
        if self._external_client is not None:
            return self._external_client, False
        return (
            httpx.Client(
                timeout=httpx.Timeout(REQUEST_TIMEOUT_SECONDS),
                follow_redirects=True,
                headers={
                    "Accept": "text/html,application/zip,application/xml",
                    "User-Agent": "Kontragent/1.0 official-data-ingestion",
                },
            ),
            True,
        )

    @staticmethod
    def _response_error(response) -> RoszdravOpenDataError:
        status = int(response.status_code)
        return RoszdravOpenDataError(
            kind=("service_unavailable" if status >= 500 else "http_error"),
            message=f"Росздравнадзор вернул HTTP {status}",
            http_status=status,
        )

    def _get(self, url: str) -> dict:
        _official_url(url, base_url=url)
        client, should_close = self._client()
        try:
            try:
                response = client.get(url)
            except httpx.TimeoutException as error:
                raise RoszdravOpenDataError(
                    kind="timeout",
                    message="Росздравнадзор: превышено время ожидания",
                ) from error
            except httpx.RequestError as error:
                raise RoszdravOpenDataError(
                    kind="network_error",
                    message="Росздравнадзор: ошибка сетевого запроса",
                ) from error
            if response.status_code != 200:
                raise self._response_error(response)
            final_url = str(getattr(response, "url", url) or url)
            _official_url(final_url, base_url=final_url)
            content = bytes(response.content)
            headers = {
                key.lower(): value
                for key, value in getattr(response, "headers", {}).items()
                if key.lower()
                in {
                    "content-encoding",
                    "content-length",
                    "content-type",
                    "date",
                    "etag",
                    "last-modified",
                }
            }
            length = headers.get("content-length")
            if length is not None and headers.get("content-encoding", "identity") in {
                "",
                "identity",
            }:
                try:
                    length_matches = int(length) == len(content)
                except ValueError:
                    length_matches = False
                if not length_matches:
                    raise RoszdravOpenDataError(
                        kind="invalid_response",
                        message="Росздравнадзор: тело не соответствует Content-Length",
                        http_status=200,
                    )
            return {"content": content, "headers": headers, "http_status": 200}
        finally:
            if should_close:
                client.close()

    def discover(
        self,
        source_page_url: str,
        *,
        now: datetime | None = None,
    ) -> RoszdravOpenDataRelease:
        discovered_at = now or datetime.now(timezone.utc)
        response = self._get(source_page_url)
        return parse_open_data_release(
            response["content"],
            source_page_url=source_page_url,
            discovered_at=discovered_at,
        )

    def download(self, url: str) -> dict:
        return self._get(url)
