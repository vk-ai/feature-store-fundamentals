"""Online lookup after materialize."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_store.schema import FeatureSchema
from feature_store.store import FeatureStore

ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CSV = ROOT / "examples" / "user_features.csv"
SCHEMA_JSON = ROOT / "schemas" / "user_features_v1.json"


@pytest.fixture()
def store(tmp_path: Path) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas")
    schema = FeatureSchema.load(SCHEMA_JSON)
    fs.register_schema(schema)
    fs.ingest_csv(schema.name, schema.version, EXAMPLE_CSV)
    fs.materialize(schema.name, schema.version)
    return fs


def test_get_online_features_lookup(store: FeatureStore) -> None:
    result = store.get_online_features("user_features", "1", ["u1", "u2", "missing"])
    assert result["u1"] is not None
    assert result["u1"]["avg_order_value"] == 42.5
    assert result["u1"]["orders_30d"] == 3
    assert result["u1"]["is_premium"] is True
    assert result["u2"]["is_premium"] is False
    assert result["missing"] is None


def test_materialize_count(store: FeatureStore) -> None:
    n = store.materialize("user_features", "1")
    assert n == 4
    assert store.online.size() == 4


def test_cli_roundtrip(tmp_path: Path) -> None:
    from feature_store.cli import main

    root = tmp_path / "data"
    schema_dir = tmp_path / "schemas"
    assert (
        main(
            [
                "--root",
                str(root),
                "--schema-dir",
                str(schema_dir),
                "register",
                "--name",
                "user_features",
                "--version",
                "1",
                "--entity-key",
                "user_id",
                "--features",
                "avg_order_value",
                "orders_30d",
                "is_premium",
                "--dtypes",
                "float",
                "int",
                "bool",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--root",
                str(root),
                "--schema-dir",
                str(schema_dir),
                "ingest",
                "--name",
                "user_features",
                "--version",
                "1",
                "--csv",
                str(EXAMPLE_CSV),
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--root",
                str(root),
                "--schema-dir",
                str(schema_dir),
                "materialize",
                "--name",
                "user_features",
                "--version",
                "1",
            ]
        )
        == 0
    )
    assert (
        main(
            [
                "--root",
                str(root),
                "--schema-dir",
                str(schema_dir),
                "get-online-features",
                "--name",
                "user_features",
                "--version",
                "1",
                "--entities",
                "u3",
            ]
        )
        == 0
    )
