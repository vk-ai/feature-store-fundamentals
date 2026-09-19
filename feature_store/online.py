"""Online feature store: in-memory dict and/or SQLite keyed by entity."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feature_store.schema import FeatureSchema


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _parse_iso(ts: str) -> datetime:
    # Accept trailing Z
    if ts.endswith("Z"):
        ts = ts[:-1] + "+00:00"
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


@dataclass
class OnlineRecord:
    features: dict[str, Any]
    materialized_at: str  # ISO-8601 UTC


class OnlineStore:
    """Low-latency entity-keyed feature lookup.

    Dual backend:
      - always keeps an in-memory dict for fast reads
      - optionally persists to SQLite for durability across process restarts

    Each row is stamped with ``materialized_at`` so toy online TTL can expire
    stale entities on read (teaching stub — not Redis EXPIRE / Feast key TTL).
    """

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        # key: (feature_view, version, entity_id) -> OnlineRecord
        self._mem: dict[tuple[str, str, str], OnlineRecord] = {}
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
                    materialized_at TEXT,
                    PRIMARY KEY (feature_view, version, entity_id)
                )
                """
            )
            # Migrate older DBs that lack materialized_at
            cols = {
                row["name"]
                for row in conn.execute("PRAGMA table_info(online_features)").fetchall()
            }
            if "materialized_at" not in cols:
                conn.execute(
                    "ALTER TABLE online_features ADD COLUMN materialized_at TEXT"
                )
            conn.commit()

    def _hydrate_from_db(self) -> None:
        with self._connect() as conn:
            cur = conn.execute(
                "SELECT feature_view, version, entity_id, payload, materialized_at "
                "FROM online_features"
            )
            for row in cur.fetchall():
                key = (row["feature_view"], row["version"], row["entity_id"])
                mat = row["materialized_at"] or datetime.fromtimestamp(
                    0, tz=timezone.utc
                ).isoformat()
                self._mem[key] = OnlineRecord(
                    features=json.loads(row["payload"]),
                    materialized_at=mat,
                )

    def put(
        self,
        schema: FeatureSchema,
        entity_id: str,
        features: dict[str, Any],
        *,
        materialized_at: str | None = None,
    ) -> None:
        payload = {f: features.get(f) for f in schema.features}
        mat = materialized_at or _utcnow().isoformat()
        key = (schema.name, schema.version, str(entity_id))
        self._mem[key] = OnlineRecord(features=payload, materialized_at=mat)
        if self.db_path is not None:
            with self._connect() as conn:
                conn.execute(
                    """
                    INSERT INTO online_features
                        (feature_view, version, entity_id, payload, materialized_at)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(feature_view, version, entity_id)
                    DO UPDATE SET
                        payload = excluded.payload,
                        materialized_at = excluded.materialized_at
                    """,
                    (
                        schema.name,
                        schema.version,
                        str(entity_id),
                        json.dumps(payload),
                        mat,
                    ),
                )
                conn.commit()

    def get(
        self,
        schema: FeatureSchema,
        entity_ids: list[str],
        *,
        now: datetime | None = None,
        drop_expired: bool = True,
    ) -> tuple[dict[str, dict[str, Any] | None], int]:
        """Return features for entities and count of TTL-expired rows dropped.

        When ``schema.online_ttl_seconds`` is set and ``drop_expired`` is True,
        entities whose ``materialized_at + ttl`` is before ``now`` are omitted
        (value ``None``) and counted in the expired total.
        """
        now = now or _utcnow()
        ttl = schema.online_ttl_seconds
        out: dict[str, dict[str, Any] | None] = {}
        expired = 0
        for eid in entity_ids:
            key = (schema.name, schema.version, str(eid))
            if key not in self._mem:
                out[str(eid)] = None
                continue
            rec = self._mem[key]
            if (
                drop_expired
                and ttl is not None
                and (_parse_iso(rec.materialized_at).timestamp() + float(ttl))
                < now.timestamp()
            ):
                out[str(eid)] = None
                expired += 1
                continue
            out[str(eid)] = dict(rec.features)
        return out, expired

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
