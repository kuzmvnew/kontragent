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


_CHECK_OPERATORS = tuple(sorted((
    "!~~*", "->>", "#>>", "::", ">=", "<=", "<>", "!=", "||", "&&",
    "@>", "<@", "->", "#>", "~~*", "!~~", "~~", "?|", "?&",
), key=len, reverse=True))


def _check_tokens(value):
    """Lex the SQL subset emitted for CHECK constraints, or return ``None``.

    Whitespace is discarded only after token boundaries are known. Quoted
    literals and identifiers retain their exact spelling and contents.
    """
    value = str(value)
    tokens = []
    index = 0
    while index < len(value):
        character = value[index]
        if character.isspace():
            index += 1
            continue
        if value.startswith("--", index) or value.startswith("/*", index):
            return None

        if (
            value[index:index + 2].lower() == "u&"
            and index + 2 < len(value)
            and value[index + 2] == '"'
        ):
            cursor = index + 3
            while cursor < len(value):
                if value[cursor] == '"':
                    if cursor + 1 < len(value) and value[cursor + 1] == '"':
                        cursor += 2
                        continue
                    cursor += 1
                    tokens.append((
                        "unicode_quoted_identifier",
                        "u&" + value[index + 2:cursor],
                    ))
                    index = cursor
                    break
                cursor += 1
            else:
                return None
            continue

        prefix_length = 0
        if character in "eEbBxX" and index + 1 < len(value) and value[index + 1] == "'":
            prefix_length = 1
        elif value[index:index + 2].lower() == "u&" and index + 2 < len(value) and value[index + 2] == "'":
            prefix_length = 2
        quote_index = index + prefix_length
        if value[quote_index:quote_index + 1] == "'":
            cursor = quote_index + 1
            escaped_string = value[index:quote_index].lower() == "e"
            while cursor < len(value):
                if escaped_string and value[cursor] == "\\":
                    cursor += 2
                    continue
                if value[cursor] == "'":
                    if cursor + 1 < len(value) and value[cursor + 1] == "'":
                        cursor += 2
                        continue
                    cursor += 1
                    token = value[index:cursor]
                    if prefix_length == 2:
                        token = "u&" + value[index + 2:cursor]
                    tokens.append(("string", token))
                    index = cursor
                    break
                cursor += 1
            else:
                return None
            continue

        if character == '"':
            cursor = index + 1
            while cursor < len(value):
                if value[cursor] == '"':
                    if cursor + 1 < len(value) and value[cursor + 1] == '"':
                        cursor += 2
                        continue
                    cursor += 1
                    tokens.append(("quoted_identifier", value[index:cursor]))
                    index = cursor
                    break
                cursor += 1
            else:
                return None
            continue

        if character == "$":
            return None
        if character.isalpha() or character == "_":
            cursor = index + 1
            while cursor < len(value) and (
                value[cursor].isalnum() or value[cursor] in "_$"
            ):
                cursor += 1
            tokens.append(("word", value[index:cursor].lower()))
            index = cursor
            continue
        if character.isdigit() or (
            character == "." and index + 1 < len(value) and value[index + 1].isdigit()
        ):
            match = re.match(
                r"(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?",
                value[index:],
            )
            if match is None:
                return None
            token = match.group(0)
            tokens.append(("number", token))
            index += len(token)
            continue

        operator = next(
            (candidate for candidate in _CHECK_OPERATORS
             if value.startswith(candidate, index)),
            None,
        )
        if operator is not None:
            tokens.append(("operator", operator))
            index += len(operator)
            continue
        if character in "()[],.+-*/%=><~!^|&?#:@":
            token_kind = "punctuation" if character in "()[],." else "operator"
            tokens.append((token_kind, character))
            index += 1
            continue
        return None
    return tuple(tokens)


def _matching_group_end(tokens, start, opening="(", closing=")"):
    if start >= len(tokens) or tokens[start] != ("punctuation", opening):
        return None
    depth = 0
    for index in range(start, len(tokens)):
        token = tokens[index]
        if token == ("punctuation", opening):
            depth += 1
        elif token == ("punctuation", closing):
            depth -= 1
            if depth == 0:
                return index
    return None


def _strip_outer_check_parentheses(tokens):
    while tokens and tokens[0] == ("punctuation", "("):
        end = _matching_group_end(tokens, 0)
        if end != len(tokens) - 1:
            break
        tokens = tokens[1:-1]
    return tokens


def _split_check_list(tokens):
    parts = []
    start = 0
    round_depth = square_depth = 0
    for index, token in enumerate(tokens):
        if token == ("punctuation", "("):
            round_depth += 1
        elif token == ("punctuation", ")"):
            round_depth -= 1
        elif token == ("punctuation", "["):
            square_depth += 1
        elif token == ("punctuation", "]"):
            square_depth -= 1
        elif token == ("punctuation", ",") and round_depth == square_depth == 0:
            parts.append(tokens[start:index])
            start = index + 1
        if round_depth < 0 or square_depth < 0:
            return None
    if round_depth or square_depth:
        return None
    parts.append(tokens[start:])
    return parts


def _string_in_candidate(tokens):
    """Parse one side of the narrow PostgreSQL IN/ANY equivalence.

    PostgreSQL 18 deparses this project's VARCHAR ``IN ('a', ...)`` checks as
    ``(column)::text = ANY ((ARRAY['a'::character varying, ...])::text[])``.
    TEXT columns use ``column = ANY (ARRAY['a'::text, ...])``. No other casts,
    NULL elements, expressions, operators, prefixed strings, or array forms
    are accepted here. Type-dependent equivalence is decided by the schema
    comparator, never by lexical canonicalization.
    """
    tokens = _strip_outer_check_parentheses(tokens)
    if not tokens:
        return None

    # ORM form: simple_identifier IN (string_literal, ...)
    if len(tokens) >= 4 and tokens[0][0] == "word" and tokens[1] == ("word", "in"):
        end = _matching_group_end(tokens, 2)
        if end == len(tokens) - 1:
            parts = _split_check_list(tokens[3:end])
            if parts and all(
                len(part) == 1
                and part[0][0] == "string"
                and part[0][1].startswith("'")
                for part in parts
            ):
                return ("declared_in", tokens[0][1], tuple(part[0][1] for part in parts))

    cursor = 0
    lhs_cast = None
    if tokens[0] == ("punctuation", "("):
        end = _matching_group_end(tokens, 0)
        if end == 2 and tokens[1][0] == "word":
            cursor = end + 1
            if tokens[cursor:cursor + 2] != (
                ("operator", "::"), ("word", "text"),
            ):
                return None
            lhs = tokens[1][1]
            lhs_cast = "text"
            cursor += 2
        else:
            return None
    elif tokens[0][0] == "word":
        lhs = tokens[0][1]
        cursor = 1
        if tokens[cursor:cursor + 2] == (
            ("operator", "::"), ("word", "text"),
        ):
            lhs_cast = "text"
            cursor += 2
    else:
        return None
    if tokens[cursor:cursor + 2] != (("operator", "="), ("word", "any")):
        return None
    cursor += 2
    any_end = _matching_group_end(tokens, cursor)
    if any_end != len(tokens) - 1:
        return None
    array_tokens = tokens[cursor + 1:any_end]

    array_cast = None
    if array_tokens and array_tokens[0] == ("punctuation", "("):
        array_end = _matching_group_end(array_tokens, 0)
        if array_end is None:
            return None
        suffix = array_tokens[array_end + 1:]
        if suffix != (
            ("operator", "::"), ("word", "text"),
            ("punctuation", "["), ("punctuation", "]"),
        ):
            return None
        array_tokens = array_tokens[1:array_end]
        array_cast = "text[]"

    if len(array_tokens) < 3 or array_tokens[:2] != (
        ("word", "array"), ("punctuation", "["),
    ):
        return None
    bracket_end = _matching_group_end(array_tokens, 1, "[", "]")
    if bracket_end is None:
        return None
    suffix = array_tokens[bracket_end + 1:]
    if suffix:
        if suffix != (
            ("operator", "::"), ("word", "text"),
            ("punctuation", "["), ("punctuation", "]"),
        ) or array_cast is not None:
            return None
        array_cast = "text[]"
    parts = _split_check_list(array_tokens[2:bracket_end])
    if not parts:
        return None

    literal_casts = []
    literals = []
    for part in parts:
        if len(part) == 3 and part[0][0] == "string" and part[1] == ("operator", "::") and part[2][0] == "word":
            literals.append(part[0][1])
            literal_casts.append(part[2][1])
        elif len(part) == 4 and part[0][0] == "string" and part[1:] == (
            ("operator", "::"), ("word", "character"), ("word", "varying"),
        ):
            literals.append(part[0][1])
            literal_casts.append("character varying")
        elif len(part) == 6 and part[0][0] == "string" and part[1:] == (
            ("operator", "::"), ("word", "character"), ("word", "varying"),
            ("operator", "::"), ("word", "text"),
        ):
            # PostgreSQL 18 pg_dump/restore can persist a VARCHAR IN check as
            # `column::text = ANY (ARRAY['x'::varchar::text, ...])` rather
            # than putting one `::text[]` cast around the array.  Accept only
            # this exact element-wise cast chain; arbitrary nested casts stay
            # significant and fail closed.
            literals.append(part[0][1])
            literal_casts.append("character varying::text")
        else:
            return None

    text_shape = lhs_cast is None and array_cast is None and set(literal_casts) == {"text"}
    varchar_shape = (
        lhs_cast == "text" and array_cast == "text[]"
        and set(literal_casts) == {"character varying"}
    )
    restored_varchar_shape = (
        lhs_cast == "text" and array_cast is None
        and set(literal_casts) == {"character varying::text"}
    )
    if not (text_shape or varchar_shape or restored_varchar_shape):
        return None
    return ("postgres_any", lhs, tuple(literals), "text" if text_shape else "varchar")


def _check_type_family(value):
    if value is None:
        return None
    normalized = re.sub(r"\s+", " ", str(value).strip().upper())
    if normalized == "TEXT":
        return "text"
    if re.fullmatch(r"(?:VARCHAR|CHARACTER VARYING)(?:\s*\(\s*\d+\s*\))?", normalized):
        return "varchar"
    return None


def _check_column_family(column_name, expected_columns, actual_columns):
    expected = expected_columns.get(column_name)
    actual = actual_columns.get(column_name)
    if expected is None or actual is None:
        return None
    expected_family = _check_type_family(expected.get("type"))
    actual_family = _check_type_family(actual.get("type"))
    return expected_family if expected_family == actual_family else None


def _checks_equivalent(expected_sql, actual_sql, expected_columns, actual_columns):
    """Prove the sole type-dependent CHECK representation equivalence."""
    if expected_sql[0] != "tokens" or actual_sql[0] != "tokens":
        return False
    expected_candidate = _string_in_candidate(expected_sql[1])
    actual_candidate = _string_in_candidate(actual_sql[1])
    if expected_candidate is None or actual_candidate is None:
        return False

    candidates = {expected_candidate[0], actual_candidate[0]}
    if candidates != {"declared_in", "postgres_any"}:
        return False
    declared = (
        expected_candidate
        if expected_candidate[0] == "declared_in"
        else actual_candidate
    )
    deparsed = (
        expected_candidate
        if expected_candidate[0] == "postgres_any"
        else actual_candidate
    )
    if declared[1:3] != deparsed[1:3]:
        return False
    family = _check_column_family(
        declared[1], expected_columns, actual_columns,
    )
    return family is not None and family == deparsed[3]


def _canonical_check(value):
    """Return a conservative, literal-aware CHECK representation.

    Casts and all unrecognized structures remain significant. Only lexical
    whitespace, unquoted PostgreSQL case folding, and redundant outer grouping
    are normalized. Unsupported Unicode ``UESCAPE`` clauses remain opaque.
    """
    tokens = _check_tokens(value)
    if tokens is None:
        return ("opaque", str(value))
    if ("word", "uescape") in tokens:
        return ("opaque", str(value))
    tokens = _strip_outer_check_parentheses(tokens)
    return ("tokens", tokens)


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
             if observed["sqltext"] == wanted["sqltext"] or _checks_equivalent(
                 wanted["sqltext"], observed["sqltext"],
                 expected["columns"], actual["columns"],
             )),
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
