from __future__ import annotations

import hashlib
import html
import re
from dataclasses import dataclass
from datetime import date, datetime
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin

import httpx


METADATA_URL = "https://www.nalog.gov.ru/opendata/7707329152-rsmppp/"
DATASET_PATH = "/opendata/7707329152-rsmppp/"
_FILE_RE = re.compile(r"data-(\d{8})-structure-(\d{8})\.zip", re.IGNORECASE)
_XSD_RE = re.compile(r"structure-(\d{8})\.xsd", re.IGNORECASE)
_DATE_RE = re.compile(r"\b(\d{2}\.\d{2}\.\d{4})\b")


class FnsSmeSupportProviderError(RuntimeError):
    def __init__(self, *, kind: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.http_status = http_status


@dataclass(frozen=True)
class FnsSmeSupportRelease:
    data_url: str
    structure_url: str | None
    modified_date: date | None
    data_date: date


class _MetadataParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[str] = []
        self.parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag.lower() == "a":
            href = dict(attrs).get("href")
            if href:
                self.links.append(html.unescape(href).strip())

    def handle_data(self, data):
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    @property
    def text(self) -> str:
        return " ".join(self.parts)


def _parse_ru_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%d.%m.%Y").date()
    except ValueError:
        return None


def _date_after_label(text: str, label: str) -> date | None:
    position = text.find(label)
    if position < 0:
        return None
    match = _DATE_RE.search(text[position : position + 220])
    return _parse_ru_date(match.group(1)) if match else None


def parse_release_metadata(html_text: str, *, base_url: str = METADATA_URL) -> FnsSmeSupportRelease:
    parser = _MetadataParser()
    parser.feed(html_text)

    candidates: list[tuple[date, date, str]] = []
    structure_candidates: list[tuple[date, str]] = []
    for href in parser.links:
        absolute = urljoin(base_url, href)
        match = _FILE_RE.search(absolute)
        if match:
            release_date = datetime.strptime(match.group(1), "%Y%m%d").date()
            structure_date = datetime.strptime(match.group(2), "%Y%m%d").date()
            candidates.append((release_date, structure_date, absolute))
        xsd_match = _XSD_RE.search(absolute)
        if xsd_match:
            structure_date = datetime.strptime(xsd_match.group(1), "%Y%m%d").date()
            structure_candidates.append((structure_date, absolute))

    if not candidates:
        # Some FNS renderings expose the URL as text but not as a clickable link.
        for match in re.finditer(
            r"https://file\.nalog\.ru/opendata/7707329152-rsmppp/"
            r"data-(\d{8})-structure-(\d{8})\.zip",
            html_text,
            re.IGNORECASE,
        ):
            candidates.append(
                (
                    datetime.strptime(match.group(1), "%Y%m%d").date(),
                    datetime.strptime(match.group(2), "%Y%m%d").date(),
                    match.group(0),
                )
            )

    if not candidates:
        raise FnsSmeSupportProviderError(
            kind="metadata_parse_error",
            message="На официальной metadata-странице ФНС не найден bulk ZIP РМСП-ПП",
        )

    # The page contains previous releases too. The newest official release wins.
    _, selected_structure_date, data_url = max(candidates, key=lambda item: item[0])
    exact_structures = [
        url for structure_date, url in structure_candidates
        if structure_date == selected_structure_date
    ]
    structure_url = exact_structures[0] if exact_structures else urljoin(
        data_url, f"structure-{selected_structure_date.strftime('%Y%m%d')}.xsd"
    )

    data_date = _date_after_label(parser.text, "Дата актуальности")
    if data_date is None:
        # The filename is the publication/change date, not necessarily the relevance date.
        # Never silently pretend it is the same thing.
        raise FnsSmeSupportProviderError(
            kind="metadata_parse_error",
            message="На metadata-странице ФНС не определена дата актуальности набора",
        )

    modified_date = _date_after_label(parser.text, "Дата последнего внесения изменений")
    return FnsSmeSupportRelease(
        data_url=data_url,
        structure_url=structure_url,
        modified_date=modified_date,
        data_date=data_date,
    )


class FnsSmeSupportProvider:
    def __init__(self, client: httpx.Client | None = None):
        self.client = client

    def _client(self) -> httpx.Client:
        return self.client or httpx.Client(
            timeout=httpx.Timeout(300.0, connect=30.0),
            follow_redirects=True,
            headers={"User-Agent": "Kontragent/1.0 (+official-open-data-sync)"},
        )

    def discover_release(self) -> FnsSmeSupportRelease:
        owns_client = self.client is None
        client = self._client()
        try:
            response = client.get(METADATA_URL)
            if response.status_code != 200:
                raise FnsSmeSupportProviderError(
                    kind="http_error",
                    message=f"ФНС metadata HTTP {response.status_code}",
                    http_status=response.status_code,
                )
            return parse_release_metadata(response.text, base_url=METADATA_URL)
        except FnsSmeSupportProviderError:
            raise
        except httpx.HTTPError as error:
            raise FnsSmeSupportProviderError(
                kind="network_error", message=f"Ошибка запроса metadata ФНС: {error}"
            ) from error
        finally:
            if owns_client:
                client.close()

    def download(self, release: FnsSmeSupportRelease, destination: Path) -> dict:
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(destination.suffix + ".part")
        digest = hashlib.sha256()
        size = 0
        owns_client = self.client is None
        client = self._client()
        try:
            with client.stream("GET", release.data_url) as response:
                if response.status_code != 200:
                    raise FnsSmeSupportProviderError(
                        kind="http_error",
                        message=f"ФНС bulk ZIP HTTP {response.status_code}",
                        http_status=response.status_code,
                    )
                with temporary.open("wb") as output:
                    for chunk in response.iter_bytes(chunk_size=1024 * 1024):
                        if not chunk:
                            continue
                        output.write(chunk)
                        digest.update(chunk)
                        size += len(chunk)
            temporary.replace(destination)
            return {
                "path": destination,
                "sha256": digest.hexdigest(),
                "bytes": size,
                "http_status": 200,
                "source_url": release.data_url,
            }
        except FnsSmeSupportProviderError:
            temporary.unlink(missing_ok=True)
            raise
        except (httpx.HTTPError, OSError) as error:
            temporary.unlink(missing_ok=True)
            raise FnsSmeSupportProviderError(
                kind="download_error", message=f"Ошибка скачивания bulk ZIP ФНС: {error}"
            ) from error
        finally:
            if owns_client:
                client.close()
