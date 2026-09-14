import os


os.environ.setdefault(
    "DATABASE_URL",
    "postgresql+psycopg://test:test@localhost:5432/test",
)

os.environ.setdefault(
    "DADATA_API_KEY",
    "test-api-key",
)