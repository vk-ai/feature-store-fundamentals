"""Offline feature table backed by CSV and/or SQLite."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from feature_store.schema import FeatureSchema


class OfflineStore:
    """Historical / batch feature source.

    Supports:
      - CSV files (one row per entity event)
      - SQLite tables (optional durable offline mirror)
    """

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root / "offline.sqlite"

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def ingest_csv(self, schema: FeatureSchema, csv_path: Path | str, table: str | None = None) -> int:
        """Load CSV into an offline SQLite table named after the feature view."""
        csv_path = Path(csv_path)
        table = table or f"{schema.name}_v{schema.version}"
        columns = [schema.entity_key, *schema.features]
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            rows = [{c: row.get(c) for c in columns} for row in reader]

        col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
        placeholders = ", ".join("?" for _ in columns)
        with self._connect() as conn:
            conn.execute(f'DROP TABLE IF EXISTS "{table}"')
            conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
            conn.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})',
                [tuple(r[c] for c in columns) for r in rows],
            )
            conn.commit()
        return len(rows)

    def read_rows(self, schema: FeatureSchema, table: str | None = None) -> list[dict[str, Any]]:
        table = table or f"{schema.name}_v{schema.version}"
        columns = [schema.entity_key, *schema.features]
        select = ", ".join(f'"{c}"' for c in columns)
        with self._connect() as conn:
            try:
                cur = conn.execute(f'SELECT {select} FROM "{table}"')
            except sqlite3.OperationalError as exc:
                raise FileNotFoundError(f"Offline table missing: {table}") from exc
            return [dict(row) for row in cur.fetchall()]

    def export_csv(self, schema: FeatureSchema, out_path: Path | str, table: str | None = None) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.read_rows(schema, table=table)
        columns = [schema.entity_key, *schema.features]
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns)
            writer.writeheader()
            writer.writerows(rows)
        return out_path

    def iter_entity_features(
        self, schema: FeatureSchema, table: str | None = None
    ) -> Iterable[tuple[str, dict[str, Any]]]:
        for row in self.read_rows(schema, table=table):
            entity = str(row[schema.entity_key])
            feats = {f: _coerce(row.get(f), schema.dtypes.get(f, "str")) for f in schema.features}
            yield entity, feats


def _coerce(value: Any, dtype: str) -> Any:
    if value is None or value == "":
        return None
    dtype = (dtype or "str").lower()
    try:
        if dtype in {"int", "integer"}:
            return int(float(value))
        if dtype in {"float", "double", "number"}:
            return float(value)
        if dtype in {"bool", "boolean"}:
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"1", "true", "yes", "y"}
    except (TypeError, ValueError):
        return value
    return value
