import argparse
import hashlib
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlparse
import xml.etree.ElementTree as ET

import httpx


ERKNM_BASE_URL = "https://proverki.gov.ru/blob/erknm-opendata"
DEFAULT_OUTPUT_DIR = Path("data/erknm")


def build_metadata_url(year: int, month: int) -> str:
    if year < 2000:
        raise ValueError("year должен быть >= 2000")
    if month < 1 or month > 12:
        raise ValueError("month должен быть от 1 до 12")

    return (
        f"{ERKNM_BASE_URL}/"
        f"7710146102-inspection-{year}-{month}.xml"
    )


def parse_months(value: str) -> list[int]:
    months: set[int] = set()

    for part in value.split(","):
        part = part.strip()
        if not part:
            continue

        if "-" in part:
            start_text, end_text = part.split("-", 1)
            start = int(start_text)
            end = int(end_text)
            if start > end:
                raise ValueError(
                    f"Некорректный диапазон месяцев: {part}"
                )
            months.update(range(start, end + 1))
        else:
            months.add(int(part))

    if not months:
        raise ValueError("Не указан ни один месяц")

    invalid = [month for month in months if month < 1 or month > 12]
    if invalid:
        raise ValueError(
            "Месяцы должны быть от 1 до 12: "
            + ", ".join(map(str, sorted(invalid)))
        )

    return sorted(months)


def _text(root: ET.Element, path: str) -> str | None:
    value = root.findtext(path)
    if value is None:
        return None

    value = value.strip()
    return value or None


def parse_erknm_metadata(xml_bytes: bytes) -> dict:
    root = ET.fromstring(xml_bytes)

    identifier = _text(root, "identifier")
    title = _text(root, "title")
    subject = _text(root, "subject") or ""

    if "248" not in subject or "ФЗ" not in subject.upper():
        raise ValueError(
            "Metadata не относится к ФГИС ЕРКНМ по 248-ФЗ"
        )

    structure_url = (
        _text(root, "erknmStructure/structureversion")
        or _text(root, "structure/structureversion")
    )

    if not structure_url:
        raise ValueError("В metadata отсутствует URL XSD")

    if not structure_url.startswith(ERKNM_BASE_URL + "/"):
        raise ValueError(
            "XSD не относится к официальному ERKNM open-data path"
        )

    versions = []

    for item in root.findall("./data/dataversion"):
        source_url = _text(item, "source")
        created = _text(item, "created")
        structure = _text(item, "structure")
        provenance = _text(item, "provenance")

        if not source_url or not created:
            continue

        if not source_url.startswith(ERKNM_BASE_URL + "/"):
            continue

        versions.append(
            {
                "source_url": source_url,
                "created": created,
                "structure": structure,
                "provenance": provenance,
            }
        )

    if not versions:
        raise ValueError(
            "В metadata нет допустимых ERKNM dataversion"
        )

    latest = max(
        versions,
        key=lambda item: (
            item["created"],
            item["source_url"],
        ),
    )

    return {
        "identifier": identifier,
        "title": title,
        "subject": subject,
        "structure_url": structure_url,
        "latest": latest,
        "version_count": len(versions),
    }


def _filename_from_url(url: str) -> str:
    name = Path(urlparse(url).path).name
    if not name:
        raise ValueError(f"Не удалось определить имя файла из URL: {url}")
    return name


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while True:
            chunk = source.read(chunk_size)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def download_stream(
    client: httpx.Client,
    url: str,
    destination: Path,
    force: bool = False,
) -> dict:
    destination.parent.mkdir(parents=True, exist_ok=True)

    if destination.exists() and not force:
        return {
            "path": str(destination),
            "downloaded": False,
            "size": destination.stat().st_size,
            "sha256": sha256_file(destination),
        }

    temp_path = destination.with_suffix(destination.suffix + ".part")

    if temp_path.exists():
        temp_path.unlink()

    try:
        with client.stream("GET", url) as response:
            response.raise_for_status()
            with temp_path.open("wb") as target:
                for chunk in response.iter_bytes():
                    target.write(chunk)

        temp_path.replace(destination)

    except Exception:
        if temp_path.exists():
            temp_path.unlink()
        raise

    return {
        "path": str(destination),
        "downloaded": True,
        "size": destination.stat().st_size,
        "sha256": sha256_file(destination),
    }


def fetch_metadata(
    client: httpx.Client,
    year: int,
    month: int,
) -> tuple[str, bytes] | None:
    url = build_metadata_url(year, month)
    response = client.get(url)

    if response.status_code == 404:
        return None

    response.raise_for_status()
    return url, response.content


def sync_month(
    client: httpx.Client,
    year: int,
    month: int,
    output_dir: Path,
    force: bool = False,
    metadata_only: bool = False,
) -> dict | None:
    fetched = fetch_metadata(client, year, month)
    if fetched is None:
        return None

    metadata_url, xml_bytes = fetched
    metadata = parse_erknm_metadata(xml_bytes)

    month_dir = output_dir / str(year) / f"{month:02d}"
    month_dir.mkdir(parents=True, exist_ok=True)

    metadata_path = month_dir / "metadata.xml"
    metadata_path.write_bytes(xml_bytes)

    latest = metadata["latest"]
    data_url = latest["source_url"]
    data_filename = _filename_from_url(data_url)

    result = {
        "year": year,
        "month": month,
        "metadata_url": metadata_url,
        "metadata_path": str(metadata_path),
        "identifier": metadata["identifier"],
        "title": metadata["title"],
        "subject": metadata["subject"],
        "version_count": metadata["version_count"],
        "source_created": latest["created"],
        "source_url": data_url,
        "structure_url": metadata["structure_url"],
        "data": None,
        "structure": None,
    }

    if metadata_only:
        return result

    data_path = month_dir / data_filename
    result["data"] = download_stream(
        client=client,
        url=data_url,
        destination=data_path,
        force=force,
    )

    structure_filename = _filename_from_url(metadata["structure_url"])
    structure_path = output_dir / "schema" / structure_filename
    result["structure"] = download_stream(
        client=client,
        url=metadata["structure_url"],
        destination=structure_path,
        force=force,
    )

    return result


def sync_year(
    year: int,
    months: list[int],
    output_dir: Path,
    timeout: float = 120.0,
    force: bool = False,
    metadata_only: bool = False,
) -> list[dict]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []

    headers = {
        "User-Agent": (
            "Kontragent/0.1 ERKNM open-data sync "
            "(official public datasets)"
        )
    }

    with httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers=headers,
    ) as client:
        for month in months:
            print()
            print(f"===== {year}-{month:02d} =====")

            try:
                result = sync_month(
                    client=client,
                    year=year,
                    month=month,
                    output_dir=output_dir,
                    force=force,
                    metadata_only=metadata_only,
                )
            except Exception as error:
                print("ERROR:", type(error).__name__, str(error))
                continue

            if result is None:
                print("Metadata пока не опубликован — пропускаем")
                continue

            results.append(result)
            print("Источник:", result["subject"])
            print("Версий:", result["version_count"])
            print("Актуальная версия:", result["source_created"])
            print("ZIP:", result["source_url"])

            if result["data"]:
                print("Файл:", result["data"]["path"])
                print("Размер:", result["data"]["size"])
                print("SHA-256:", result["data"]["sha256"])

    manifest_path = output_dir / str(year) / "manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(
        json.dumps(
            {
                "year": year,
                "synced_at": date.today().isoformat(),
                "months_requested": months,
                "months_found": [item["month"] for item in results],
                "items": results,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    print()
    print("===== ERKNM SYNC FINISHED =====")
    print("Найдено месяцев:", len(results))
    print("Manifest:", manifest_path)

    return results


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Автоматически находит и скачивает последние официальные "
            "версии открытых данных ФГИС ЕРКНМ (248-ФЗ)"
        )
    )
    parser.add_argument(
        "--year",
        type=int,
        default=date.today().year,
    )
    parser.add_argument(
        "--months",
        default="1-12",
        help="Например: 1-12 или 1,2,7-9",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_DIR),
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=120.0,
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Перекачать уже существующие файлы",
    )
    parser.add_argument(
        "--metadata-only",
        action="store_true",
        help="Проверить metadata без скачивания ZIP/XSD",
    )

    args = parser.parse_args()

    months = parse_months(args.months)

    sync_year(
        year=args.year,
        months=months,
        output_dir=Path(args.output_dir),
        timeout=args.timeout,
        force=args.force,
        metadata_only=args.metadata_only,
    )


if __name__ == "__main__":
    main()
