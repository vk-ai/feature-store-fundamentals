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

    def list_schemas(self, name: str | None = None) -> list[FeatureSchema]:
        return self.schemas.list_versions(name)
