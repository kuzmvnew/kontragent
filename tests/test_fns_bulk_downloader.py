from __future__ import annotations

from hashlib import md5, sha256
from io import BytesIO

import pytest

from app.ingestion import fns_bulk_worker as bulk


URL = "https://file.nalog.ru/opendata/7707329152-rsmp/data.zip"


class FakeResponse:
    def __init__(self, content: bytes, headers: dict[str, str], *, status: int = 200):
        self._stream = BytesIO(content)
        self.headers = headers
        self.status = status
        self.read_calls = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        self.read_calls += 1
        return self._stream.read(size)


def _part_digest(content: bytes) -> bytes:
    digest = md5(usedforsecurity=False)
    digest.update(content)
    return digest.digest()


def _multipart_etag(content: bytes, part_size: int) -> str:
    part_digests = [
        _part_digest(content[offset : offset + part_size])
        for offset in range(0, len(content), part_size)
    ]
    digest = md5(usedforsecurity=False)
    digest.update(b"".join(part_digests))
    return f'"{digest.hexdigest()}-{len(part_digests)}"'


def _multipart_opener(
    content: bytes,
    *,
    part_size: int,
    etag: str | None = None,
    advertised_sha256: str | None = None,
    mutate_range_headers=None,
    mutate_range_body=None,
):
    etag = etag or _multipart_etag(content, part_size)
    advertised_sha256 = advertised_sha256 or sha256(content).hexdigest()
    initial_headers = {
        "Content-Length": str(len(content)),
        "Accept-Ranges": "bytes",
        "ETag": etag,
        "x-amz-meta-sha256": advertised_sha256,
    }
    initial = FakeResponse(b"must-not-be-read", initial_headers)
    requests = []

    def opener(request, *, timeout):
        assert timeout == 120
        requests.append(request)
        raw_range = request.get_header("Range")
        if raw_range is None:
            return initial
        start_text, end_text = raw_range.removeprefix("bytes=").split("-")
        start, end = int(start_text), int(end_text)
        body = content[start : end + 1]
        expected_length = len(body)
        if mutate_range_body is not None:
            body = mutate_range_body(body, start, end)
        headers = {
            "Content-Range": f"bytes {start}-{end}/{len(content)}",
            "Content-Length": str(expected_length),
            "ETag": etag,
            "x-amz-meta-sha256": advertised_sha256,
        }
        if mutate_range_headers is not None:
            mutate_range_headers(headers, start, end)
        return FakeResponse(body, headers, status=206)

    return opener, initial, requests


def test_short_range_is_retried_without_accepting_partial_bytes(
    monkeypatch, tmp_path
):
    content = b"0123456789"
    attempts = {}

    def truncate_first(body, start, _end):
        attempts[start] = attempts.get(start, 0) + 1
        return body[:-1] if attempts[start] == 1 else body

    opener, _initial, requests = _multipart_opener(
        content,
        part_size=4,
        mutate_range_body=truncate_first,
    )
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bulk, "urlopen", opener)

    path, _headers = bulk._download_temp(URL, tmp_path)

    assert path.read_bytes() == content
    assert [request.get_header("Range") for request in requests] == [
        None,
        "bytes=0-3",
        "bytes=0-3",
        "bytes=4-7",
        "bytes=4-7",
        "bytes=8-9",
        "bytes=8-9",
    ]
    assert all(
        request.get_header("If-match") == _multipart_etag(content, 4)
        for request in requests[1:]
    )


def test_exhausted_short_range_is_retryable_and_removes_partial(
    monkeypatch, tmp_path
):
    content = b"0123456789"
    opener, _initial, requests = _multipart_opener(
        content,
        part_size=4,
        mutate_range_body=lambda body, _start, _end: body[:-1],
    )
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk.time, "sleep", lambda _seconds: None)
    monkeypatch.setattr(bulk, "urlopen", opener)

    with pytest.raises(
        bulk.WorkerNetworkError,
        match="transport failed after 3 attempts",
    ):
        bulk._download_temp(URL, tmp_path)

    assert [request.get_header("Range") for request in requests] == [
        None,
        "bytes=0-3",
        "bytes=0-3",
        "bytes=0-3",
    ]
    assert all(
        request.get_header("If-match") == _multipart_etag(content, 4)
        for request in requests[1:]
    )
    assert list(tmp_path.iterdir()) == []


def test_small_download_preserves_single_response_behavior(monkeypatch, tmp_path):
    response = FakeResponse(b"small official XSD", {"Content-Length": "18"})
    requests = []

    def opener(request, *, timeout):
        requests.append(request)
        assert timeout == 120
        return response

    monkeypatch.setattr(bulk, "urlopen", opener)

    path, headers = bulk._download_temp(URL, tmp_path)

    assert path.read_bytes() == b"small official XSD"
    assert headers == {"Content-Length": "18"}
    assert len(requests) == 1
    assert requests[0].get_header("Range") is None


def test_large_multipart_download_is_range_pinned_and_verified(monkeypatch, tmp_path):
    content = b"0123456789"
    part_size = 4
    opener, initial, requests = _multipart_opener(content, part_size=part_size)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", part_size)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk, "urlopen", opener)

    path, headers = bulk._download_temp(URL, tmp_path)

    assert path.read_bytes() == content
    assert headers["x-amz-meta-sha256"] == sha256(content).hexdigest()
    assert initial.read_calls == 0
    assert [request.get_header("Range") for request in requests] == [
        None,
        "bytes=0-3",
        "bytes=4-7",
        "bytes=8-9",
    ]
    assert all(
        request.get_header("If-match") == headers["ETag"]
        for request in requests[1:]
    )


@pytest.mark.parametrize(
    ("header_name", "header_value", "error_match"),
    [
        ("Content-Range", "bytes 1-4/10", "Content-Range differs"),
        ("Content-Length", "3", "Content-Length differs"),
        ("ETag", '"00000000000000000000000000000000-3"', "ETag changed"),
        ("x-amz-meta-sha256", "0" * 64, "official SHA256 changed"),
    ],
)
def test_range_metadata_mismatch_fails_closed_and_removes_partial(
    monkeypatch,
    tmp_path,
    header_name,
    header_value,
    error_match,
):
    content = b"0123456789"

    def mutate(headers, start, _end):
        if start == 4:
            headers[header_name] = header_value

    opener, _initial, _requests = _multipart_opener(
        content,
        part_size=4,
        mutate_range_headers=mutate,
    )
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk, "urlopen", opener)

    with pytest.raises(bulk.InvalidDataError, match=error_match):
        bulk._download_temp(URL, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_assembled_multipart_etag_mismatch_is_not_accepted(monkeypatch, tmp_path):
    content = b"0123456789"
    wrong_etag = _multipart_etag(b"abcdefghij", 4)
    opener, _initial, _requests = _multipart_opener(
        content,
        part_size=4,
        etag=wrong_etag,
        advertised_sha256=sha256(content).hexdigest(),
    )
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk, "urlopen", opener)

    with pytest.raises(bulk.InvalidDataError, match="assembled ETag differs"):
        bulk._download_temp(URL, tmp_path)

    assert list(tmp_path.iterdir()) == []


def test_large_unknown_multipart_layout_fails_before_reading_body(
    monkeypatch, tmp_path
):
    headers = {
        "Content-Length": "10",
        "Accept-Ranges": "bytes",
        "ETag": '"0123456789abcdef0123456789abcdef-2"',
    }
    response = FakeResponse(b"must-not-be-read", headers)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(bulk.InvalidDataError, match="part count differs"):
        bulk._download_temp(URL, tmp_path)

    assert response.read_calls == 0
    assert list(tmp_path.iterdir()) == []


@pytest.mark.parametrize(
    ("headers", "error_match"),
    [
        (
            {
                "Accept-Ranges": "bytes",
                "ETag": '"0123456789abcdef0123456789abcdef-3"',
            },
            "no Content-Length",
        ),
        (
            {
                "Content-Length": "10",
                "Accept-Ranges": "bytes",
                "ETag": 'W/"0123456789abcdef0123456789abcdef-3"',
            },
            "no strong ETag",
        ),
        (
            {
                "Content-Length": "10",
                "ETag": '"0123456789abcdef0123456789abcdef-3"',
            },
            "does not advertise byte ranges",
        ),
    ],
)
def test_unverifiable_large_multipart_response_fails_closed(
    monkeypatch, tmp_path, headers, error_match
):
    response = FakeResponse(b"must-not-be-read", headers)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_PART_SIZE", 4)
    monkeypatch.setattr(bulk, "MULTIPART_RANGE_MIN_SIZE", 8)
    monkeypatch.setattr(bulk, "urlopen", lambda *_args, **_kwargs: response)

    with pytest.raises(bulk.InvalidDataError, match=error_match):
        bulk._download_temp(URL, tmp_path)

    assert response.read_calls == 0
    assert list(tmp_path.iterdir()) == []
