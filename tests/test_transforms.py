"""Request-time transforms: on_read vs on_write, one function for both paths."""

from __future__ import annotations

import csv
import json
from pathlib import Path

import pytest

from feature_store.cli import main as cli_main
from feature_store.schema import FeatureSchema
from feature_store.store import FeatureStore
from feature_store.transforms import (
    MissingRequestDataError,
    TransformError,
    check_transform_parity,
    load_transforms_file,
)

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_JSON = ROOT / "schemas" / "user_features_v1.json"
TS_CSV = ROOT / "examples" / "user_features_ts.csv"
ENTITY_REQ_CSV = ROOT / "examples" / "entity_df_request.csv"
DEMO_TRANSFORMS = ROOT / "examples" / "transforms_demo.py"


def _entity_rows() -> list[dict]:
    with ENTITY_REQ_CSV.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture()
def store(tmp_path: Path) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas")
    schema = FeatureSchema.load(SCHEMA_JSON)
    fs.register_schema(schema)
    fs.ingest_csv(schema.name, schema.version, TS_CSV)
    load_transforms_file(fs, DEMO_TRANSFORMS)
    fs.materialize(schema.name, schema.version)
    return fs


def test_on_read_uses_request_data_online(store: FeatureStore) -> None:
    res = store.get_online_features("user_features", "1", ["u1"], request_data={"cart_value": 85})
    assert res.features["u1"]["cart_to_aov_ratio"] == 2.0  # 85 / 42.5


def test_on_write_is_computed_at_materialize_and_stored(store: FeatureStore) -> None:
    stats = store.transforms.stats
    assert stats["orders_per_week"].on_write_rows == 4  # once per entity at materialize
    assert stats["orders_per_week"].on_read_rows == 0
    # Stored online: visible even via the raw online store, no transform at read time.
    schema = store.schemas.get("user_features", "1")
    raw, _ = store.online.get(schema, ["u3"])
    assert raw["u3"]["orders_per_week"] == round(8 * 7 / 30, 4)
    assert "cart_to_aov_ratio" not in raw["u3"]  # on_read is never stored


def test_historical_and_online_paths_give_identical_values(store: FeatureStore) -> None:
    """The anti-skew lesson: one function feeds training rows and serving rows."""
    rows = _entity_rows()
    historical = {r["user_id"]: r for r in store.get_historical_features("user_features", "1", rows)}
    by_entity = {r["user_id"]: {"cart_value": r["cart_value"]} for r in rows}
    online = store.get_online_features(
        "user_features", "1", list(by_entity), request_data_by_entity=by_entity
    ).features
    for eid in by_entity:
        for feat in ("cart_to_aov_ratio", "orders_per_week"):
            assert historical[eid][feat] == online[eid][feat], (eid, feat)
    # u4 has avg_order_value == 0 → transform returns None on both paths
    assert historical["u4"]["cart_to_aov_ratio"] is None and online["u4"]["cart_to_aov_ratio"] is None


def test_transform_parity_report_passes(store: FeatureStore) -> None:
    report = check_transform_parity(store, "user_features", "1", _entity_rows())
    assert report.passed is True
    assert report.compared == 4
    assert report.mismatches == []


def test_transform_parity_detects_skewed_on_write_value(store: FeatureStore) -> None:
    schema = store.schemas.get("user_features", "1")
    # Simulate a hand-rolled "online reimplementation" that drifted.
    store.online.put(
        schema,
        "u2",
        {"avg_order_value": 19.0, "orders_30d": 1, "is_premium": False},
        extra={"orders_per_week": 0.25},
    )
    report = check_transform_parity(store, "user_features", "1", _entity_rows())
    assert report.passed is False
    assert [(m.entity_id, m.feature) for m in report.mismatches] == [("u2", "orders_per_week")]


def test_on_write_rejects_request_inputs(store: FeatureStore) -> None:
    with pytest.raises(TransformError, match="on_write runs at materialize"):
        store.register_transform(
            "bad",
            feature_view="user_features",
            version="1",
            inputs=["avg_order_value"],
            request_schema={"cart_value": "float"},
            outputs=["bad_out"],
            fn=lambda row: {"bad_out": 1},
            mode="on_write",
        )


def test_missing_request_data_raises(store: FeatureStore) -> None:
    with pytest.raises(MissingRequestDataError):
        store.get_online_features("user_features", "1", ["u1"])
    with pytest.raises(MissingRequestDataError):
        store.get_historical_features(
            "user_features", "1", [{"user_id": "u1", "event_timestamp": "2026-01-02T00:00:00Z"}]
        )


def test_registration_validation(store: FeatureStore) -> None:
    common = dict(feature_view="user_features", version="1", fn=lambda row: {})
    with pytest.raises(TransformError, match="unknown stored inputs"):
        store.register_transform("x", inputs=["nope"], outputs=["x_out"], **common)
    with pytest.raises(TransformError, match="collide"):
        store.register_transform("y", inputs=["orders_30d"], outputs=["orders_30d"], **common)
    with pytest.raises(TransformError, match="collide"):
        store.register_transform("z", inputs=["orders_30d"], outputs=["orders_per_week"], **common)


def test_output_keys_must_match_declaration(store: FeatureStore) -> None:
    store.register_transform(
        "wrong_keys",
        feature_view="user_features",
        version="1",
        inputs=["orders_30d"],
        outputs=["declared"],
        fn=lambda row: {"something_else": 1},
    )
    with pytest.raises(TransformError, match="declared outputs"):
        store.get_online_features("user_features", "1", ["u1"], request_data={"cart_value": 1})


def test_null_in_null_out_for_unmatched_historical_row(store: FeatureStore) -> None:
    rows = store.get_historical_features(
        "user_features",
        "1",
        [{"user_id": "u1", "event_timestamp": "2025-01-01T00:00:00Z", "cart_value": "10"}],
    )
    assert rows[0]["avg_order_value"] is None  # PIT join: nothing known yet
    assert rows[0]["cart_to_aov_ratio"] is None
    assert rows[0]["orders_per_week"] is None


def test_on_read_counts_compute_per_read(store: FeatureStore) -> None:
    before = store.transforms.stats["cart_to_aov_ratio"].on_read_rows
    store.get_online_features("user_features", "1", ["u1", "u2"], request_data={"cart_value": 5})
    assert store.transforms.stats["cart_to_aov_ratio"].on_read_rows == before + 2


def test_existing_paths_unchanged_without_transforms(tmp_path: Path) -> None:
    fs = FeatureStore(root=tmp_path / "d", schema_dir=tmp_path / "s")
    schema = FeatureSchema.load(SCHEMA_JSON)
    fs.register_schema(schema)
    fs.ingest_csv(schema.name, schema.version, TS_CSV)
    fs.materialize(schema.name, schema.version)
    out = fs.get_online_features("user_features", "1", ["u1"]).features["u1"]
    assert set(out) == {"avg_order_value", "orders_30d", "is_premium"}


def test_cli_request_json_and_transforms(tmp_path: Path, capsys) -> None:
    base = ["--root", str(tmp_path / "data"), "--schema-dir", str(tmp_path / "schemas")]
    assert cli_main(base + ["register", "--name", "user_features", "--version", "1",
                            "--entity-key", "user_id", "--features", "avg_order_value",
                            "orders_30d", "is_premium", "--dtypes", "float", "int", "bool"]) == 0
    assert cli_main(base + ["ingest", "--name", "user_features", "--version", "1", "--csv", str(TS_CSV)]) == 0
    assert cli_main(base + ["materialize", "--name", "user_features", "--version", "1",
                            "--transforms", str(DEMO_TRANSFORMS)]) == 0
    capsys.readouterr()
    assert cli_main(base + ["get-online-features", "--name", "user_features", "--version", "1",
                            "--entities", "u1", "--transforms", str(DEMO_TRANSFORMS),
                            "--request-json", '{"cart_value": 85}']) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["features"]["u1"]["cart_to_aov_ratio"] == 2.0
    assert data["features"]["u1"]["orders_per_week"] == 0.7
    # Missing request data → clear error, exit 2
    assert cli_main(base + ["get-online-features", "--name", "user_features", "--version", "1",
                            "--entities", "u1", "--transforms", str(DEMO_TRANSFORMS)]) == 2
