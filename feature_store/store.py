"""FeatureStore facade: register schemas, materialize, online lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from feature_store.offline import OfflineStore
from feature_store.online import OnlineStore, _utcnow
from feature_store.schema import FeatureSchema, SchemaMismatchError, SchemaRegistry


@dataclass
class OnlineFeaturesResult:
    """Online lookup result including toy TTL expiry stats."""

    features: dict[str, dict[str, Any] | None]
    expired: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {"features": self.features, "expired": self.expired}


class FeatureStore:
    """Tiny Feast-style feature store for learning demos."""

    def __init__(
        self,
        root: Path | str = "data",
        *,
        schema_dir: Path | str | None = None,
        persist_online: bool = True,
    ) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.schemas = SchemaRegistry(schema_dir or self.root / "schemas")
        self.offline = OfflineStore(self.root / "offline")
        online_db = (self.root / "online" / "online.sqlite") if persist_online else None
        self.online = OnlineStore(online_db)

    def register_schema(self, schema: FeatureSchema, overwrite: bool = False) -> Path:
        return self.schemas.register(schema, overwrite=overwrite)

    def ingest_csv(self, name: str, version: str, csv_path: Path | str) -> int:
        schema = self.schemas.get(name, version)
        return self.offline.ingest_csv(schema, csv_path)

    def materialize(
        self,
        name: str,
        version: str,
        *,
        materialized_at: str | None = None,
    ) -> int:
        """Copy offline rows into the online store for low-latency lookup.

        Each online row is stamped with ``materialized_at`` (ISO UTC) for toy TTL.
        """
        schema = self.schemas.get(name, version)
        self.online.clear(schema)
        stamp = materialized_at or _utcnow().isoformat()
        count = 0
        for entity_id, features in self.offline.iter_entity_features(schema):
            self.online.put(schema, entity_id, features, materialized_at=stamp)
            count += 1
        return count

    def get_online_features(
        self,
        name: str,
        version: str,
        entity_ids: list[str],
        *,
        expected_schema: FeatureSchema | None = None,
        now: datetime | None = None,
    ) -> OnlineFeaturesResult:
        """Look up features for entities; drop TTL-expired rows; raise on mismatch."""
        schema = self.schemas.require(name, version, expected=expected_schema)
        features, expired = self.online.get(schema, entity_ids, now=now)
        return OnlineFeaturesResult(features=features, expired=expired)


    def get_historical_features(
        self,
        name: str,
        version: str,
        entity_rows: list[dict[str, Any]],
        *,
        as_of_known_time: bool = False,
    ) -> list[dict[str, Any]]:
        """
        Toy point-in-time / as-of join for an entity dataframe.

        Each ``entity_rows`` dict needs ``schema.entity_key`` + ``event_timestamp``.
        Offline rows should include ``event_timestamp`` and (for leak demos)
        ``created_timestamp``.

        When ``as_of_known_time`` is False (default): filter only by event time
        (can leak features whose ``created_timestamp`` is after the entity time).
        When True: also require ``created_timestamp <= entity.event_timestamp``.

        Teaching stub only — not Feast ``get_historical_features`` / ASOF SQL.
        Real Feast: ``filter_by_created_timestamp`` (v0.66+, feast#6615/#6617).
        """
        from feature_store.offline import _coerce
        from feature_store.pit import asof_join

        schema = self.schemas.get(name, version)
        raw_rows = self.offline.read_rows(schema)
        feature_rows: list[dict[str, Any]] = []
        for row in raw_rows:
            coerced = {
                schema.entity_key: str(row[schema.entity_key]),
            }
            for f in schema.features:
                coerced[f] = _coerce(row.get(f), schema.dtypes.get(f, "str"))
            if "event_timestamp" in row:
                coerced["event_timestamp"] = row["event_timestamp"]
            if "created_timestamp" in row:
                coerced["created_timestamp"] = row["created_timestamp"]
            feature_rows.append(coerced)
        joined = asof_join(
            entity_rows,
            feature_rows,
            entity_key=schema.entity_key,
            feature_names=schema.features,
            as_of_known_time=as_of_known_time,
        )
        return joined


    def check_parity(
        self,
        name: str,
        version: str,
        entity_ids: list[str],
        *,
        rtol: float = 1e-5,
        atol: float = 1e-8,
    ):
        """Sampled online↔offline value parity (see ``feature_store.parity``)."""
        from feature_store.parity import check_parity

        return check_parity(self, name, version, entity_ids, rtol=rtol, atol=atol)

    def list_schemas(self, name: str | None = None) -> list[FeatureSchema]:
        return self.schemas.list_versions(name)
