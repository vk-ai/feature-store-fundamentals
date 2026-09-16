"""FeatureStore facade: register schemas, materialize, online lookup."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from feature_store.offline import OfflineStore
from feature_store.online import OnlineStore
from feature_store.schema import FeatureSchema, SchemaMismatchError, SchemaRegistry


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

    def materialize(self, name: str, version: str) -> int:
        """Copy offline rows into the online store for low-latency lookup."""
        schema = self.schemas.get(name, version)
        self.online.clear(schema)
        count = 0
        for entity_id, features in self.offline.iter_entity_features(schema):
            self.online.put(schema, entity_id, features)
            count += 1
        return count

    def get_online_features(
        self,
        name: str,
        version: str,
        entity_ids: list[str],
        *,
        expected_schema: FeatureSchema | None = None,
    ) -> dict[str, dict[str, Any] | None]:
        """Look up features for entities; raise on version / shape mismatch."""
        schema = self.schemas.require(name, version, expected=expected_schema)
        return self.online.get(schema, entity_ids)

    def list_schemas(self, name: str | None = None) -> list[FeatureSchema]:
        return self.schemas.list_versions(name)
