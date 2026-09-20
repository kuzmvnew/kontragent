"""Read-only PostgreSQL schema contract comparison.

Kept versioned with migrations: reconciliation revisions must not import live
ORM definitions, whose schema can change after a revision has been deployed.
Index and named CHECK identities are significant. Equivalent full-table UNIQUE
constraints and UNIQUE indexes are compared by their data-write semantics.
"""

from __future__ import annotations

import re

import sqlalchemy as sa


def _sql(value, dialect):
    return str(value.compile(dialect=dialect, compile_kwargs={"literal_binds": True}))


def _default(value):
    if value is None:
        return None
    value = str(value).strip()
    # PostgreSQL reflects literal string defaults with an explicit type cast.
    value = re.sub(r"::(?:character varying|text|boolean|integer|bigint|numeric)(?:\([\d, ]+\))?$", "", value)
    if value.upper() in {"CURRENT_TIMESTAMP", "NOW()"}:
        return "now()"
    if value.lower() in {"true", "false"}:
        return value.lower()
    return value


def _index_options(options):
    return {
        "using": options.get("postgresql_using", "btree"),
        "where": str(options["postgresql_where"]) if options.get("postgresql_where") is not None else None,
        "ops": dict(options.get("postgresql_ops") or {}),
        "include": list(options.get("postgresql_include") or []),
        "nulls_not_distinct": bool(options.get("postgresql_nulls_not_distinct", False)),
    }


def _strip_outer_parentheses(value):
    value = value.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        encloses_all = True
        for index, character in enumerate(value):
            if character == "(":
                depth += 1
            elif character == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    encloses_all = False
                    break
        if not encloses_all or depth != 0:
            break
        value = value[1:-1].strip()
    return value


def _lower_unquoted(value):
    parts = re.split(r"('(?:''|[^'])*')", value)
    return "".join(part if index % 2 else part.lower() for index, part in enumerate(parts))


def _canonical_check(value):
    """Normalize the PostgreSQL forms used by the project's ORM checks."""
    value = _strip_outer_parentheses(str(value))
    # PostgreSQL rewrites VARCHAR IN (...) as TEXT = ANY (ARRAY[...]). Remove
    # representation-only scalar/array casts, then restore the declared IN form.
    parts = re.split(r"('(?:''|[^'])*')", value)
    cast_pattern = re.compile(
        r"::\s*(?:character\s+varying|varchar|text|smallint|integer|bigint|"
        r"numeric|boolean|date)(?:\s*\([^)]*\))?(?:\s*\[\s*\])?",
        flags=re.IGNORECASE,
    )
    value = "".join(
        part if index % 2 else cast_pattern.sub("", part)
        for index, part in enumerate(parts)
    )
    value = re.sub(
        r"\b([A-Za-z_][\w.]*)\s*=\s*ANY\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)",
        r"\1 IN (\2)",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = re.sub(
        r"\b([A-Za-z_][\w.]*)\s*<>\s*ALL\s*\(\s*ARRAY\s*\[(.*?)\]\s*\)",
        r"\1 NOT IN (\2)",
        value,
        flags=re.IGNORECASE | re.DOTALL,
    )
    value = _lower_unquoted(value)
    value = re.sub(r"\s+", " ", value).strip()
    value = re.sub(r"\s*([(),])\s*", r"\1", value)
    value = re.sub(r"\s*(>=|<=|<>|!=|=|>|<)\s*", r"\1", value)
    return _strip_outer_parentheses(value)


def _unique_signature(value):
    return (
        tuple(value["columns"]),
        bool(value.get("nulls_not_distinct", False)),
    )


# This partial index is created intentionally by d4e5f6a7b8c9. It enforces
# one active publication per dataset and has no equivalent ORM Index object.
# Match its complete reflected shape so the exception cannot hide another
# unique index or a changed predicate.
KNOWN_DB_ONLY_INDEXES = {
    "dataset_publications": {
        "uq_dataset_publications_one_active": {
            "columns": ["dataset_id"],
            "unique": True,
            "using": "btree",
            "where": "is_active",
            "ops": {},
            "include": [],
            "nulls_not_distinct": False,
            "is_expression": False,
            "duplicates_constraint": None,
        },
    },
}


def model_table_schema(table, dialect):
    columns = {}
    for column in table.columns:
        identity = column.identity
        columns[column.name] = {
            "type": str(column.type.compile(dialect=dialect)),
            "nullable": column.nullable,
            "default": _default(_sql(column.server_default.arg, dialect)) if column.server_default is not None and identity is None else None,
            "client_default": _default(_sql(sa.literal(column.default.arg), dialect)) if column.default is not None and column.default.is_scalar else None,
            "identity": {key: getattr(identity, key) for key in ("always", "start", "increment", "minvalue", "maxvalue", "cache", "cycle") if getattr(identity, key) is not None} if identity is not None else None,
        }
    return {
        "columns": columns,
        "primary_key": list(table.primary_key.columns.keys()),
        "foreign_keys": sorted([
            {"columns": list(fk.columns.keys()),
             "target_schema": fk.elements[0].column.table.schema or "public",
             "target_table": fk.elements[0].column.table.name,
             "target_columns": [element.column.name for element in fk.elements],
             "ondelete": fk.ondelete or "NO ACTION", "onupdate": fk.onupdate or "NO ACTION",
             "deferrable": bool(fk.deferrable), "initially": fk.initially or "IMMEDIATE"}
            for fk in table.foreign_key_constraints
        ], key=lambda value: value["columns"]),
        "unique_constraints": sorted([
            {"name": constraint.name, "columns": list(constraint.columns.keys()),
             "nulls_not_distinct": bool(dict(constraint.dialect_kwargs).get("postgresql_nulls_not_distinct", False))}
            for constraint in table.constraints if isinstance(constraint, sa.UniqueConstraint)
        ], key=lambda value: value["columns"]),
        "check_constraints": sorted([
            {"name": constraint.name,
             "sqltext": _canonical_check(_sql(constraint.sqltext, dialect))}
            for constraint in table.constraints if isinstance(constraint, sa.CheckConstraint)
        ], key=lambda value: (value["name"] or "", value["sqltext"])),
        "indexes": {index.name: {
            "columns": [expression.name if isinstance(expression, sa.Column) else _sql(expression, dialect) for expression in index.expressions],
            "unique": bool(index.unique), **_index_options(dict(index.dialect_kwargs)),
            "is_expression": any(not isinstance(expression, sa.Column) for expression in index.expressions),
            "duplicates_constraint": None,
        } for index in sorted(table.indexes, key=lambda index: index.name)},
    }


def inspected_table_schema(inspector, name, schema=None):
    dialect = inspector.bind.dialect
    return {
        "columns": {column["name"]: {
            "type": str(column["type"].compile(dialect=dialect)),
            "nullable": column["nullable"], "default": _default(column.get("default")),
            "identity": column.get("identity"),
        } for column in inspector.get_columns(name, schema=schema)},
        "primary_key": inspector.get_pk_constraint(name, schema=schema)["constrained_columns"],
        "foreign_keys": sorted([
            {"columns": fk["constrained_columns"],
             "target_schema": fk.get("referred_schema") or schema or "public",
             "target_table": fk["referred_table"], "target_columns": fk["referred_columns"],
             "ondelete": fk.get("options", {}).get("ondelete") or "NO ACTION",
             "onupdate": fk.get("options", {}).get("onupdate") or "NO ACTION",
             "deferrable": bool(fk.get("options", {}).get("deferrable")),
             "initially": fk.get("options", {}).get("initially") or "IMMEDIATE"}
            for fk in inspector.get_foreign_keys(name, schema=schema)
        ], key=lambda value: value["columns"]),
        "unique_constraints": sorted([
            {"name": constraint["name"], "columns": constraint["column_names"],
             "nulls_not_distinct": bool(constraint.get("dialect_options", {}).get("postgresql_nulls_not_distinct", False))}
            for constraint in inspector.get_unique_constraints(name, schema=schema)
        ], key=lambda value: value["columns"]),
        "indexes": {index["name"]: {
            "columns": [column if column is not None else index.get("expressions", [])[i] for i, column in enumerate(index["column_names"])],
            "unique": bool(index["unique"]), **_index_options(index.get("dialect_options", {})),
            "is_expression": any(column is None for column in index["column_names"]),
            "duplicates_constraint": index.get("duplicates_constraint"),
        } for index in inspector.get_indexes(name, schema=schema)},
        "check_constraints": sorted([
            {"name": constraint["name"],
             "sqltext": _canonical_check(constraint["sqltext"])}
            for constraint in inspector.get_check_constraints(name, schema=schema)
        ], key=lambda value: (value["name"] or "", value["sqltext"])),
        "comment": inspector.get_table_comment(name, schema=schema).get("text"),
    }


def compare_table_schema(expected, actual, *, strict=False, allowed_extra_indexes=None):
    """Return errors plus explicit, non-blocking representation differences."""
    errors, observations = [], []

    def issue(kind, path, wanted, observed):
        errors.append({"kind": kind, "path": path, "expected": wanted, "actual": observed})

    for name in sorted(set(expected["columns"]) - set(actual["columns"])):
        issue("missing_column", name, expected["columns"][name], None)
    for name in sorted(set(actual["columns"]) - set(expected["columns"])):
        issue("extra_column", name, None, actual["columns"][name])
    for name in sorted(set(expected["columns"]) & set(actual["columns"])):
        wanted, observed = expected["columns"][name], actual["columns"][name]
        for key in ("type", "nullable"):
            if wanted[key] != observed[key]:
                issue(key + "_mismatch", name, wanted[key], observed[key])
        identity, live_identity = wanted["identity"], observed["identity"]
        identity_mismatch = (identity is None) != (live_identity is None)
        if identity is not None and live_identity is not None:
            identity_mismatch = any(live_identity.get(k) != v for k, v in identity.items())
        if identity_mismatch:
            issue("identity_mismatch", name, identity, observed["identity"])
        live_default = observed["default"]
        if wanted["type"] in {"SMALLINT", "INTEGER", "BIGINT"} or wanted["type"].startswith("NUMERIC"):
            if live_default and re.fullmatch(r"'-?\d+(?:\.\d+)?'", live_default):
                live_default = live_default[1:-1]
        if wanted["default"] != live_default:
            if not strict and wanted["default"] is None and wanted["client_default"] is not None and wanted["client_default"] == live_default:
                observations.append({"kind": "server_default_matches_client_default", "path": name, "actual": observed["default"]})
            else:
                issue("default_mismatch", name, wanted["default"], observed["default"])
    if expected["primary_key"] != actual["primary_key"]:
        issue("primary_key_mismatch", "primary_key", expected["primary_key"], actual["primary_key"])

    for fk in expected["foreign_keys"]:
        if fk not in actual["foreign_keys"]:
            issue("missing_or_incompatible_foreign_key", ",".join(fk["columns"]), fk, actual["foreign_keys"])
    for fk in actual["foreign_keys"]:
        if fk not in expected["foreign_keys"]:
            issue("unexpected_foreign_key", ",".join(fk["columns"]), expected["foreign_keys"], fk)

    expected_unique_signatures = {
        _unique_signature(constraint) for constraint in expected["unique_constraints"]
    }
    expected_unique_signatures.update(
        _unique_signature(index) for index in expected["indexes"].values()
        if index["unique"] and not index["where"] and not index["is_expression"]
    )
    actual_unique_signatures = {
        _unique_signature(constraint) for constraint in actual["unique_constraints"]
    }
    actual_unique_signatures.update(
        _unique_signature(index) for index in actual["indexes"].values()
        if index["unique"] and not index["where"] and not index["is_expression"]
    )
    for constraint in expected["unique_constraints"]:
        if _unique_signature(constraint) not in actual_unique_signatures:
            issue("missing_or_incompatible_unique_constraint", constraint["name"], constraint, actual["unique_constraints"])
    for constraint in actual["unique_constraints"]:
        if _unique_signature(constraint) not in expected_unique_signatures:
            issue("unexpected_unique_constraint", constraint["name"], expected["unique_constraints"], constraint)

    actual_checks = actual.get("check_constraints", [])
    used_actual_checks = set()
    for wanted in expected.get("check_constraints", []):
        candidates = [
            (index, observed) for index, observed in enumerate(actual_checks)
            if wanted["name"] is None or observed["name"] == wanted["name"]
        ]
        match = next(
            ((index, observed) for index, observed in candidates
             if observed["sqltext"] == wanted["sqltext"]),
            None,
        )
        if match is not None:
            used_actual_checks.add(match[0])
            continue
        if candidates:
            used_actual_checks.add(candidates[0][0])
        issue("missing_or_incompatible_check_constraint", wanted["name"], wanted, [value for _, value in candidates])
    for index, constraint in enumerate(actual_checks):
        if index not in used_actual_checks:
            issue("unexpected_check_constraint", constraint["name"], expected.get("check_constraints", []), constraint)

    for name, wanted in expected["indexes"].items():
        observed = actual["indexes"].get(name)
        if observed == wanted:
            continue
        # Existing migrations express unique=True,index=True as a named plain
        # index plus a separate UNIQUE constraint. Together these enforce the
        # same contract; never accept a plain or partial index alone as unique.
        if observed is not None and wanted["unique"] and observed == {**wanted, "unique": False} and _unique_signature(wanted) in actual_unique_signatures:
            observations.append({"kind": "unique_index_backed_by_unique_constraint", "path": name})
        else:
            issue("missing_or_incompatible_index", name, wanted, observed)
    allowed_extra_indexes = allowed_extra_indexes or {}
    for name in sorted(set(actual["indexes"]) - set(expected["indexes"])):
        observed = actual["indexes"][name]
        if observed.get("duplicates_constraint"):
            continue
        if allowed_extra_indexes.get(name) == observed:
            observations.append({"kind": "intentional_database_index", "path": name, "actual": observed})
        elif observed["unique"] and not observed["where"] and not observed["is_expression"] and _unique_signature(observed) in expected_unique_signatures:
            observations.append({"kind": "equivalent_unique_index", "path": name, "actual": observed})
        elif observed["unique"]:
            issue("unexpected_unique_index", name, None, observed)
        else:
            observations.append({"kind": "additional_database_index", "path": name, "actual": observed})
    return errors, observations


def audit_metadata(connection, metadata, *, strict=False):
    inspector = sa.inspect(connection)
    tables = sorted(inspector.get_table_names())
    expected = {name: model_table_schema(table, connection.dialect) for name, table in sorted(metadata.tables.items())}
    missing = sorted(set(expected) - set(tables))
    errors = [{"kind": "missing_table", "table": name} for name in missing]
    observations, actual = [], {}
    for name in sorted(set(expected) & set(tables)):
        actual[name] = inspected_table_schema(inspector, name)
        table_errors, table_observations = compare_table_schema(
            expected[name], actual[name], strict=strict,
            allowed_extra_indexes=KNOWN_DB_ONLY_INDEXES.get(name),
        )
        errors.extend({"table": name, **item} for item in table_errors)
        observations.extend({"table": name, **item} for item in table_observations)
    return {
        "model_table_count": len(expected), "database_table_count": len(tables),
        "missing_tables": missing, "extra_tables": sorted(set(tables) - set(expected)),
        "compatible": not errors, "errors": errors, "observations": observations,
        "expected": expected, "actual": actual,
    }
