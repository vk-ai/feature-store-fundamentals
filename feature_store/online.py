"""Online feature store: in-memory dict and/or SQLite keyed by entity."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from feature_store.schema import FeatureSchema


class OnlineStore:
    """Low-latency entity-keyed feature lookup.

    Dual backend:
      - always keeps an in-memory dict for fast reads
      - optionally persists to SQLite for durability across process restarts
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        # key: (feature_view, version, entity_id) -> feature dict
        self._mem: dict[tuple[str, str, str], dict[str, Any]] = {}
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            self._init_db()
            self._hydrate_from_db()

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS online_features (
                    feature_view TEXT NOT NULL,
                    version TEXT NOT NULL,
                    entity_id TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    PRIMARY KEY (feature_view, version, entity_id)
                )
                """
            )
            conn.commit()

    def _hydrate_from_db(self) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT feature_view, version, entity_id, payload FROM online_features"
            )
            for row in cur.fetchall():
                key = (row["feature_view"], row["version"], row["entity_id"])
                self._mem[key] = json.loads(row["payload"])

    def put(self, schema: FeatureSchema, entity_id: str, features: dict[str, Any]) -> None:
        payload = {f: features.get(f) for f in schema.features}
        key = (schema.name, schema.version, str(entity_id))
        self._mem[key] = payload
        if self.db_path is not None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO online_features (feature_view, version, entity_id, payload)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(feature_view, version, entity_id)
                    DO UPDATE SET payload = excluded.payload
                    """,
                    (schema.name, schema.version, str(entity_id), json.dumps(payload)),
                )
                conn.commit()

    def get(
        self, schema: FeatureSchema, entity_ids: list[str]
    ) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for eid in entity_ids:
            key = (schema.name, schema.version, str(eid))
            out[str(eid)] = dict(self._mem[key]) if key in self._mem else None
        return out

    def clear(self, schema: FeatureSchema | None = None) -> None:
        if schema is None:
            self._mem.clear()
            if self.db_path is not None:
                with self._connect() as conn:
                    conn.execute("DELETE FROM online_features")
                    conn.commit()
            return
        prefix = (schema.name, schema.version)
        for key in list(self._mem):
            if key[0] == prefix[0] and key[1] == prefix[1]:
                del self._mem[key]
        if self.db_path is not None:
            with self._connect() as conn:
                conn.execute(
                    "DELETE FROM online_features WHERE feature_view = ? AND version = ?",
                    prefix,
                )
                conn.commit()

    def size(self) -> int:
        return len(self._mem)
