"""Schema / version mismatch behavior."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_store.schema import FeatureSchema, SchemaMismatchError
from feature_store.store import FeatureStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CSV = ROOT / "examples" / "user_features.csv"


@pytest.fixture()
def store(tmp_path: Path) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas")
    v1 = FeatureSchema(
        name="user_features",
        version="1",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium"),
        dtypes={"avg_order_value": "float", "orders_30d": "int", "is_premium": "bool"},
    )
    fs.register_schema(v1)
    fs.ingest_csv("user_features", "1", EXAMPLE_CSV)
    fs.materialize("user_features", "1")
    return fs


def test_unknown_version_raises(store: FeatureStore) -> None:
    with pytest.raises(SchemaMismatchError, match="No schema registered"):
        store.get_online_features("user_features", "99", ["u1"])


def test_expected_schema_version_mismatch(store: FeatureStore) -> None:
    wrong = FeatureSchema(
        name="user_features",
        version="2",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium"),
    )
    with pytest.raises(SchemaMismatchError, match="No schema registered|Version mismatch"):
        store.get_online_features(
            "user_features",
            "2",
            ["u1"],
            expected_schema=wrong,
        )


def test_shape_mismatch_against_expected(store: FeatureStore) -> None:
    expected = FeatureSchema(
        name="user_features",
        version="1",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d"),  # missing is_premium
    )
    with pytest.raises(SchemaMismatchError, match="Schema shape mismatch"):
        store.get_online_features(
            "user_features",
            "1",
            ["u1"],
            expected_schema=expected,
        )


def test_register_second_version_and_isolate(store: FeatureStore, tmp_path: Path) -> None:
    v2 = FeatureSchema(
        name="user_features",
        version="2",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium", "churn_risk"),
        dtypes={
            "avg_order_value": "float",
            "orders_30d": "int",
            "is_premium": "bool",
            "churn_risk": "float",
        },
    )
    store.register_schema(v2)
    # v1 still works; v2 has no online data yet
    assert store.get_online_features("user_features", "1", ["u1"])["u1"] is not None
    assert store.get_online_features("user_features", "2", ["u1"])["u1"] is None
