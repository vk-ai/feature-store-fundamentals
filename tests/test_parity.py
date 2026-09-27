"""Online↔offline sampled parity check."""

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


def test_parity_passes_after_materialize(store: FeatureStore, tmp_path: Path) -> None:
    report = store.check_parity("user_features", "1", ["u1", "u2"])
    assert report.passed is True
    assert report.compared == 2
    assert report.mismatches == []
    out = report.write_json(tmp_path / "parity_report.json")
    assert out.exists()
    assert '"passed": true' in out.read_text().lower().replace("True", "true")


def test_parity_detects_online_skew(store: FeatureStore) -> None:
    schema = store.schemas.get("user_features", "1")
    # Corrupt online value for u1 after materialize
    store.online.put(
        schema,
        "u1",
        {
            "avg_order_value": 99999.0,  # skewed
            "orders_30d": 1,
            "is_premium": True,
        },
    )
    report = store.check_parity("user_features", "1", ["u1", "u2"])
    assert report.passed is False
    assert any(m.feature == "avg_order_value" and m.entity_id == "u1" for m in report.mismatches)
    assert report.max_abs_err > 0


def test_parity_missing_online(store: FeatureStore) -> None:
    schema = store.schemas.get("user_features", "1")
    store.online.clear(schema)
    report = store.check_parity("user_features", "1", ["u1"])
    assert report.passed is False
    assert "u1" in report.missing_online
