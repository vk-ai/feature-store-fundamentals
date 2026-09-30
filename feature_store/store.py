"""FeatureStore facade: register schemas, materialize, online lookup."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from feature_store.offline import OfflineStore
from feature_store.online import OnlineStore, _utcnow
from feature_store.schema import FeatureSchema, SchemaMismatchError, SchemaRegistry
from feature_store.transforms import (
    Transform,
    TransformFn,
    TransformMode,
    TransformRegistry,
    apply_transform,
)


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
        self.transforms = TransformRegistry()

    def register_schema(self, schema: FeatureSchema, overwrite: bool = False) -> Path:
        return self.schemas.register(schema, overwrite=overwrite)

    def register_transform(
        self,
        name: str,
        *,
        feature_view: str,
        version: str,
        inputs: Sequence[str],
        outputs: Sequence[str],
        fn: TransformFn,
        mode: TransformMode = "on_read",
        request_schema: Mapping[str, str] | None = None,
        description: str = "",
    ) -> Transform:
        """Register a request-time transform (see ``feature_store.transforms``).

        ``on_read`` runs at lookup time (online + historical); ``on_write`` runs at
        ``materialize`` and its outputs are stored online. Both call the same ``fn``.
        """
        schema = self.schemas.get(feature_view, version)
        t = Transform(
            name=name,
            feature_view=feature_view,
            version=version,
            inputs=tuple(inputs),
            outputs=tuple(outputs),
            fn=fn,
            mode=mode,
            request_schema=dict(request_schema or {}),
            description=description,
        )
        return self.transforms.register(t, stored_features=schema.features)

    def _apply(
        self,
        t: Transform,
        stored: Mapping[str, Any] | None,
        request: Mapping[str, Any] | None,
        *,
        at: str,
    ) -> dict[str, Any]:
        out = apply_transform(t, stored, request)
        stats = self.transforms.stats[t.name]
        if at == "write":
            stats.on_write_rows += 1
        else:
            stats.on_read_rows += 1
        return out

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
        ``on_write`` transforms for this view run here, once per entity, and their
        outputs are stored alongside the raw features.
        """
        schema = self.schemas.get(name, version)
        write_transforms = self.transforms.for_view(name, version, mode="on_write")
        self.online.clear(schema)
        stamp = materialized_at or _utcnow().isoformat()
        count = 0
        for entity_id, features in self.offline.iter_entity_features(schema):
            derived: dict[str, Any] = {}
            for t in write_transforms:
                derived.update(self._apply(t, features, None, at="write"))
            self.online.put(
                schema, entity_id, features, materialized_at=stamp, extra=derived or None
            )
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
        request_data: Mapping[str, Any] | None = None,
        request_data_by_entity: Mapping[str, Mapping[str, Any]] | None = None,
    ) -> OnlineFeaturesResult:
        """Look up features for entities; drop TTL-expired rows; raise on mismatch.

        ``on_read`` transforms run here. Supply their request fields either as
        ``request_data`` (same values for every entity) or ``request_data_by_entity``
        (``{entity_id: {...}}``; overrides ``request_data`` per key). ``on_write``
        outputs were computed at materialize and are simply read back.
        """
        schema = self.schemas.require(name, version, expected=expected_schema)
        features, expired = self.online.get(schema, entity_ids, now=now)
        read_transforms = self.transforms.for_view(name, version, mode="on_read")
        if read_transforms:
            for eid, feats in features.items():
                if feats is None:
                    continue  # missing / expired entity: nothing to transform
                req = {**(request_data or {}), **((request_data_by_entity or {}).get(eid) or {})}
                for t in read_transforms:
                    feats.update(self._apply(t, feats, req, at="read"))
        return OnlineFeaturesResult(features=features, expired=expired)


    def get_historical_features(
        self,
        name: str,
        version: str,
        entity_rows: list[dict[str, Any]],
        *,
        as_of_known_time: bool = False,
        apply_transforms: bool = True,
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

        With ``apply_transforms`` (default), every registered transform for the view
        (``on_read`` *and* ``on_write``) runs on the PIT-joined row via the same
        function the online/materialize paths use. Request columns (e.g.
        ``cart_value``) are read from each entity row.
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
        transforms = self.transforms.for_view(name, version) if apply_transforms else []
        for row, entity_row in zip(joined, entity_rows):
            matched = row.get("_matched_event_timestamp") is not None
            stored = {f: row.get(f) for f in schema.features} if matched else None
            for t in transforms:
                row.update(self._apply(t, stored, entity_row, at="read"))
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
