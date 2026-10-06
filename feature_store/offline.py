"""Offline feature table backed by CSV and/or SQLite."""

from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from feature_store.schema import FeatureSchema

# Optional PIT columns preserved on ingest when present in the CSV header.
TIMESTAMP_COLUMNS = ("event_timestamp", "created_timestamp")


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

    def ingest_csv(
        self,
        schema: FeatureSchema,
        csv_path: Path | str,
        table: str | None = None,
        *,
        append: bool = False,
    ) -> int:
        """Load CSV into an offline SQLite table named after the feature view.

        Default replaces the table. ``append=True`` adds rows to an existing table
        (new data arriving for incremental materialize); columns missing from the
        CSV are stored as NULL.
        """
        csv_path = Path(csv_path)
        table = table or f"{schema.name}_v{schema.version}"
        with csv_path.open(newline="", encoding="utf-8") as fh:
            reader = csv.DictReader(fh)
            fieldnames = list(reader.fieldnames or [])
            ts_cols = [c for c in TIMESTAMP_COLUMNS if c in fieldnames]
            columns = [schema.entity_key, *schema.features, *ts_cols]
            rows = [{c: row.get(c) for c in columns} for row in reader]

        with self._connect() as conn:
            existing = [
                r["name"] for r in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            ]
            if append and existing:
                extra = [c for c in columns if c not in existing]
                if extra:
                    raise ValueError(
                        f"cannot append to {table}: CSV has columns {extra} not in the table"
                    )
                columns = existing
                rows = [{c: r.get(c) for c in columns} for r in rows]
            else:
                col_defs = ", ".join(f'"{c}" TEXT' for c in columns)
                conn.execute(f'DROP TABLE IF EXISTS "{table}"')
                conn.execute(f'CREATE TABLE "{table}" ({col_defs})')
            placeholders = ", ".join("?" for _ in columns)
            conn.executemany(
                f'INSERT INTO "{table}" VALUES ({placeholders})',
                [tuple(r[c] for c in columns) for r in rows],
            )
            conn.commit()
        return len(rows)

    def read_rows(self, schema: FeatureSchema, table: str | None = None) -> list[dict[str, Any]]:
        table = table or f"{schema.name}_v{schema.version}"
        with self._connect() as conn:
            try:
                # Prefer SELECT * so optional PIT timestamp columns round-trip.
                cur = conn.execute(f'SELECT * FROM "{table}"')
            except sqlite3.OperationalError as exc:
                raise FileNotFoundError(f"Offline table missing: {table}") from exc
            return [dict(row) for row in cur.fetchall()]

    def export_csv(self, schema: FeatureSchema, out_path: Path | str, table: str | None = None) -> Path:
        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        rows = self.read_rows(schema, table=table)
        columns = [schema.entity_key, *schema.features]
        if rows:
            for c in TIMESTAMP_COLUMNS:
                if c in rows[0] and c not in columns:
                    columns.append(c)
        with out_path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=columns, extrasaction="ignore")
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
