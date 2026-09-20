import os

import pytest
from sqlalchemy.engine import make_url


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost:5432/test",
)

os.environ.setdefault(
    "DADATA_API_KEY",
    "test-api-key",
)


if make_url(os.environ["DATABASE_URL"]).database == "kontragent":
    raise pytest.UsageError(
        "Refusing pytest against historical database 'kontragent'; "
        "set DATABASE_URL to an isolated test database."
    )
