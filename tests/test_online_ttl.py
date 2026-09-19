"""Toy online TTL: expire-on-read after materialized_at + online_ttl_seconds."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from feature_store.schema import FeatureSchema
from feature_store.store import FeatureStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CSV = ROOT / "examples" / "user_features.csv"


def _store_with_ttl(tmp_path: Path, ttl: int | None) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas", persist_online=False)
    schema = FeatureSchema(
        name="user_features",
        version="1",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium"),
        dtypes={"avg_order_value": "float", "orders_30d": "int", "is_premium": "bool"},
        online_ttl_seconds=ttl,
    )
    fs.register_schema(schema)
    fs.ingest_csv("user_features", "1", EXAMPLE_CSV)
    return fs


def test_fresh_within_ttl(tmp_path: Path) -> None:
    fs = _store_with_ttl(tmp_path, ttl=3600)
    stamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fs.materialize("user_features", "1", materialized_at=stamp.isoformat())
    now = stamp + timedelta(seconds=10)
    result = fs.get_online_features("user_features", "1", ["u1", "u2"], now=now)
    assert result.expired == 0
    assert result.features["u1"] is not None
    assert result.features["u2"] is not None


def test_expired_dropped_and_counted(tmp_path: Path) -> None:
    fs = _store_with_ttl(tmp_path, ttl=60)
    stamp = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
    fs.materialize("user_features", "1", materialized_at=stamp.isoformat())
    now = stamp + timedelta(seconds=120)  # past TTL
    result = fs.get_online_features("user_features", "1", ["u1", "u2", "missing"], now=now)
    assert result.expired == 2  # u1 and u2 present but stale; missing never existed
    assert result.features["u1"] is None
    assert result.features["u2"] is None
    assert result.features["missing"] is None


def test_no_ttl_never_expires(tmp_path: Path) -> None:
    fs = _store_with_ttl(tmp_path, ttl=None)
    stamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    fs.materialize("user_features", "1", materialized_at=stamp.isoformat())
    now = datetime(2026, 9, 19, tzinfo=timezone.utc)
    result = fs.get_online_features("user_features", "1", ["u1"], now=now)
    assert result.expired == 0
    assert result.features["u1"] is not None


def test_schema_roundtrip_online_ttl(tmp_path: Path) -> None:
    schema = FeatureSchema(
        name="user_features",
        version="2",
        entity_key="user_id",
        features=("avg_order_value",),
        online_ttl_seconds=120,
    )
    path = tmp_path / "user_features_v2.json"
    schema.save(path)
    loaded = FeatureSchema.load(path)
    assert loaded.online_ttl_seconds == 120
