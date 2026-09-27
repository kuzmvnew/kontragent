from __future__ import annotations

import importlib

from scripts.import_public_release import SEO_COLUMNS, _seo_storage_available


class RecordingOp:
    def __init__(self):
        self.added = []
        self.dropped = []
        self.created_indexes = []
        self.dropped_indexes = []
        self.created_constraints = []
        self.dropped_constraints = []

    def add_column(self, table, column):
        self.added.append((table, column.name, column.nullable))

    def drop_column(self, table, column):
        self.dropped.append((table, column))

    def create_index(self, name, table, columns):
        self.created_indexes.append((name, table, tuple(columns)))

    def drop_index(self, name, table_name):
        self.dropped_indexes.append((name, table_name))

    def create_check_constraint(self, name, table, condition):
        self.created_constraints.append((name, table, condition))

    def drop_constraint(self, name, table, type_):
        self.dropped_constraints.append((name, table, type_))


def test_public_0002_upgrade_downgrade_are_symmetric_and_preserve_existing_rows(monkeypatch):
    migration = importlib.import_module("public_migrations.versions.public_0002_seo_projection")
    recorder = RecordingOp()
    monkeypatch.setattr(migration, "op", recorder)
    migration.upgrade()
    assert {name for _, name, _ in recorder.added} == SEO_COLUMNS
    assert all(nullable is True for _, _, nullable in recorder.added)
    assert {name for name, _, _ in recorder.created_indexes} == {
        "ix_public_projection_seo_sitemap",
        "ix_public_projection_seo_catalog",
    }
    migration.downgrade()
    assert {name for _, name in recorder.dropped} == SEO_COLUMNS
    assert {name for name, _ in recorder.dropped_indexes} == {
        "ix_public_projection_seo_sitemap",
        "ix_public_projection_seo_catalog",
    }


class ColumnCursor:
    def __init__(self, names):
        self.names = names
        self.query = None

    def execute(self, query, parameters):
        self.query = (query, parameters)

    def fetchall(self):
        return [{"column_name": name} for name in self.names]


def test_importer_detects_old_and_new_public_schema_without_guessing():
    assert _seo_storage_available(ColumnCursor(SEO_COLUMNS)) is True
    assert _seo_storage_available(ColumnCursor(set())) is False
    assert _seo_storage_available(ColumnCursor(SEO_COLUMNS - {"seo_projection"})) is False
