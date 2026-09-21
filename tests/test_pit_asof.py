"""Toy PIT / as-of join: event-time only vs created-time (leak-free) mode."""

from __future__ import annotations

from pathlib import Path

import pytest

from feature_store.schema import FeatureSchema
from feature_store.store import FeatureStore

ROOT = Path(__file__).resolve().parents[1]
PIT_CSV = ROOT / "examples" / "user_features_pit.csv"
ENTITY_CSV = ROOT / "examples" / "entity_df_pit.csv"


@pytest.fixture()
def pit_store(tmp_path: Path) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas", persist_online=False)
    schema = FeatureSchema(
        name="user_features",
        version="1",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium"),
        dtypes={"avg_order_value": "float", "orders_30d": "int", "is_premium": "bool"},
        description="PIT leak fixture",
    )
    fs.register_schema(schema)
    n = fs.ingest_csv("user_features", "1", PIT_CSV)
    assert n == 4
    return fs


def test_event_time_only_returns_leaked_value(pit_store: FeatureStore) -> None:
    """Without created-time filter, the late-created row (99.0) wins — classic leak."""
    entity_rows = [{"user_id": "u1", "event_timestamp": "2026-01-01T11:00:00Z"}]
    rows = pit_store.get_historical_features(
        "user_features", "1", entity_rows, as_of_known_time=False
    )
    assert len(rows) == 1
    assert rows[0]["avg_order_value"] == 99.0
    assert rows[0]["orders_30d"] == 9
    assert rows[0]["is_premium"] is True
    assert rows[0]["_matched_created_timestamp"] == "2026-01-01T12:00:00Z"


def test_as_of_known_time_blocks_leak(pit_store: FeatureStore) -> None:
    """With created_timestamp <= entity time, the pre-leak row (10.0) is selected."""
    entity_rows = [{"user_id": "u1", "event_timestamp": "2026-01-01T11:00:00Z"}]
    rows = pit_store.get_historical_features(
        "user_features", "1", entity_rows, as_of_known_time=True
    )
    assert len(rows) == 1
    assert rows[0]["avg_order_value"] == 10.0
    assert rows[0]["orders_30d"] == 1
    assert rows[0]["is_premium"] is False
    assert rows[0]["_matched_created_timestamp"] == "2026-01-01T09:00:00Z"


def test_cli_as_of_known_time_flag(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
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
                str(PIT_CSV),
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
                "get-historical-features",
                "--name",
                "user_features",
                "--version",
                "1",
                "--entity-csv",
                str(ENTITY_CSV),
                "--as-of-known-time",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert '"as_of_known_time": true' in out
    assert "10.0" in out
    assert "99.0" not in out
