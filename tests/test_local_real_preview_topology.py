from __future__ import annotations

import os
from pathlib import Path

import pytest

from scripts import local_real_preview_support as preview


def _tcp(database: str) -> str:
    return f"postgresql+psycopg://127.0.0.1/{database}"


def _socket(database: str) -> str:
    return f"postgresql+psycopg://localhost/{database}?host=/tmp"


def _validate_source(source_url: str, *, environment=None):
    return preview.validate_database_topology(
        source_url=source_url,
        operational_url=_tcp(preview.OPERATIONAL_DATABASE),
        public_url=_tcp(preview.PUBLIC_DATABASE),
        environment={} if environment is None else environment,
    )


def test_pinned_sqlalchemy_and_psycopg_apply_query_host_override():
    url = (
        f"postgresql+psycopg://localhost/{preview.SOURCE_DATABASE}"
        "?host=remote.example"
    )

    parameters = preview._effective_connection_parameters(url)

    # This is the effective value returned by the real SQLAlchemy 2 / psycopg 3
    # parsers pinned by the project, not an authority-string approximation.
    assert parameters["host"] == "remote.example"


@pytest.mark.parametrize(
    ("source_url", "message"),
    [
        pytest.param(
            f"postgresql+psycopg://remote.example/{preview.SOURCE_DATABASE}",
            "loopback",
            id="remote-authority-host",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?host=remote.example",
            "loopback",
            id="remote-query-host-override",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?hostaddr=203.0.113.10",
            "hostaddr must be loopback",
            id="remote-hostaddr",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?host=127.0.0.1,remote.example",
            "ambiguous PostgreSQL",
            id="mixed-local-remote-multi-host",
        ),
        pytest.param(
            f"postgresql+psycopg:///{preview.SOURCE_DATABASE}",
            "explicit single local PostgreSQL host",
            id="implicit-default-host",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?host=127.0.0.1&host=remote.example",
            "disagree about host",
            id="duplicate-ambiguous-host",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?host=/var/run/postgresql",
            "Unix socket must be exactly /tmp",
            id="unapproved-unix-socket",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?service=production",
            "service parameters are forbidden",
            id="libpq-service-query",
        ),
        pytest.param(
            f"postgresql+psycopg://127.0.0.1/{preview.SOURCE_DATABASE}"
            "?dbname=postgres",
            "database must be exactly",
            id="query-database-override",
        ),
    ],
)
def test_fail_closed_topology_rejects_effective_remote_or_ambiguous_endpoint(
    source_url: str,
    message: str,
):
    with pytest.raises(ValueError, match=message):
        _validate_source(source_url)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PGHOST", "remote.example"),
        ("PGHOST", ""),
        ("PGHOSTADDR", "203.0.113.10"),
        ("PGPORT", "6432"),
        ("PGDATABASE", "postgres"),
        ("PGSERVICE", "production"),
        ("PGSERVICEFILE", "/tmp/pg_service.conf"),
        ("PGSYSCONFDIR", "/tmp"),
    ],
)
def test_fail_closed_topology_rejects_libpq_environment_sources(name: str, value: str):
    with pytest.raises(ValueError, match=name):
        _validate_source(_tcp(preview.SOURCE_DATABASE), environment={name: value})


def test_localhost_name_is_rejected_if_resolution_is_not_exclusively_loopback(
    monkeypatch,
):
    monkeypatch.setattr(
        preview.socket,
        "getaddrinfo",
        lambda *args, **kwargs: [
            (
                preview.socket.AF_INET,
                preview.socket.SOCK_STREAM,
                6,
                "",
                ("127.0.0.1", 0),
            ),
            (
                preview.socket.AF_INET,
                preview.socket.SOCK_STREAM,
                6,
                "",
                ("203.0.113.10", 0),
            ),
        ],
    )

    with pytest.raises(ValueError, match="resolve only to loopback"):
        _validate_source(
            f"postgresql+psycopg://localhost/{preview.SOURCE_DATABASE}"
        )


@pytest.mark.parametrize("builder", [_tcp, _socket], ids=("loopback-tcp", "tmp-socket"))
def test_topology_accepts_explicit_approved_local_endpoints(builder):
    assert preview.validate_database_topology(
        source_url=builder(preview.SOURCE_DATABASE),
        operational_url=builder(preview.OPERATIONAL_DATABASE),
        public_url=builder(preview.PUBLIC_DATABASE),
        environment={},
    ) == {
        "source": preview.SOURCE_DATABASE,
        "operational": preview.OPERATIONAL_DATABASE,
        "public": preview.PUBLIC_DATABASE,
    }


def test_remote_query_override_is_rejected_before_psycopg_connect(monkeypatch):
    url = (
        f"postgresql+psycopg://localhost/{preview.SOURCE_DATABASE}"
        "?host=remote.example"
    )
    assert preview._effective_connection_parameters(url)["host"] == "remote.example"
    connect_called = False

    def unexpected_connect(*args, **kwargs):
        nonlocal connect_called
        connect_called = True
        raise AssertionError("network connection must not be attempted")

    monkeypatch.setattr(preview.psycopg, "connect", unexpected_connect)

    with pytest.raises(ValueError, match="loopback"):
        preview.inspect_source(url)
    assert connect_called is False


def test_remote_query_override_is_rejected_before_sqlalchemy_engine(monkeypatch):
    url = (
        f"postgresql+psycopg://localhost/{preview.OPERATIONAL_DATABASE}"
        "?host=remote.example"
    )
    assert preview._effective_connection_parameters(url)["host"] == "remote.example"
    create_engine_called = False

    def unexpected_create_engine(*args, **kwargs):
        nonlocal create_engine_called
        create_engine_called = True
        raise AssertionError("SQLAlchemy engine must not be created")

    monkeypatch.setattr(preview.sa, "create_engine", unexpected_create_engine)

    with pytest.raises(ValueError, match="loopback"):
        preview.bootstrap_workspace(url, "a-secure-preview-password")
    assert create_engine_called is False


@pytest.mark.skipif(
    os.getenv("LOCAL_REAL_PREVIEW_SOCKET_TEST") != "1"
    or not Path("/tmp/.s.PGSQL.5432").exists(),
    reason="set LOCAL_REAL_PREVIEW_SOCKET_TEST=1 with a local PostgreSQL /tmp socket",
)
def test_approved_tmp_socket_opens_the_expected_real_local_database():
    url = _socket(preview.SOURCE_DATABASE)
    preview._validated_database_name(url, preview.SOURCE_DATABASE, environment={})

    with preview.psycopg.connect(preview._psycopg_url(url)) as connection:
        connection.execute("SET TRANSACTION READ ONLY")
        observed = connection.execute("SELECT current_database()").fetchone()[0]

    assert observed == preview.SOURCE_DATABASE
