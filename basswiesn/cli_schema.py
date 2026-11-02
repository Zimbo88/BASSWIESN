"""Project source schema metadata without importing ORM/startup side effects."""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sqlite3

from basswiesn.cli_contract import OfflineError


def declared_schema() -> dict[str, set[str]]:
    """Project table/column presence, not migration history or index semantics.

    A parity test compares the projection with actual SQLAlchemy metadata.
    Source-based distributions include core.py; missing metadata fails closed.
    """
    try:
        source = Path(__file__).parent / "app" / "models" / "core.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        schema: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.ClassDef):
                continue
            table = ""
            columns: set[str] = set()
            for statement in node.body:
                if isinstance(statement, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__tablename__" for target in statement.targets):
                    if isinstance(statement.value, ast.Constant) and isinstance(statement.value.value, str):
                        table = statement.value.value
                if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
                    value = statement.value
                    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name) and value.func.id == "mapped_column":
                        column = statement.target.id
                        if value.args and isinstance(value.args[0], ast.Constant) and isinstance(value.args[0].value, str):
                            column = value.args[0].value
                        columns.add(column)
            if table:
                if not re.fullmatch(r"[a-z_]+", table) or not columns or table in schema:
                    raise ValueError("unsupported schema")
                schema[table] = columns
        if not {"devices", "settings", "schema_migrations"}.issubset(schema):
            raise ValueError("incomplete schema")
        return schema
    except (OSError, SyntaxError, UnicodeError, ValueError):
        raise OfflineError("schema.contract", "The installed schema metadata is unavailable or unsupported.", "Use a complete matching source distribution; no database changes were attempted.") from None


def declared_baseline() -> int:
    try:
        source = Path(__file__).parent / "app" / "db" / "migrations.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "SCHEMA_BASELINE" for target in node.targets):
                if isinstance(node.value, ast.Constant) and type(node.value.value) is int and node.value.value == 2:
                    return 2
        raise ValueError("unsupported baseline")
    except (OSError, SyntaxError, UnicodeError, ValueError):
        raise OfflineError("schema.baseline", "The installed schema baseline is unavailable or unsupported by backup v1.", "Use a matching supported distribution; the CLI never invents or upgrades a schema baseline.") from None


def schema_compatible(db: sqlite3.Connection) -> bool:
    from basswiesn.cli_storage import table_columns

    existing = table_columns(db)
    return all(columns.issubset(existing.get(table, set())) for table, columns in declared_schema().items())


def immutable_policy() -> tuple[set[str], set[str]]:
    """Project literal downstream protection constants without evaluating code."""
    try:
        source = Path(__file__).parent / "app" / "config.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        names = ("IMMUTABLE_PROTECTED_IPS", "IMMUTABLE_PROTECTED_DEVICE_IDS")
        values: dict[str, set[str]] = {}
        for node in tree.body:
            if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name) or node.target.id not in names:
                continue
            value = node.value
            if not isinstance(value, ast.Call) or not isinstance(value.func, ast.Name) or value.func.id != "frozenset" or value.keywords or len(value.args) > 1:
                raise ValueError("nonliteral protection")
            items = ast.literal_eval(value.args[0]) if value.args else ()
            if not isinstance(items, (tuple, list, set)) or any(not isinstance(item, str) for item in items):
                raise ValueError("nonliteral protection")
            values[node.target.id] = set(items)
        if set(values) != set(names):
            raise ValueError("missing protection")
        return values[names[0]], values[names[1]]
    except (OSError, SyntaxError, UnicodeError, ValueError, TypeError):
        raise OfflineError("policy.constants", "Immutable protection metadata cannot be safely inspected.", "Review the downstream protection constants locally; no target is authorized by this CLI.") from None
