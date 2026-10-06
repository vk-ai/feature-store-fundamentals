"""Incremental materialize with a stored watermark + freshness gauge."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from feature_store.cli import main
from feature_store.freshness import METRIC_NAME, Watermark, WatermarkStore, prometheus_text
from feature_store.schema import FeatureSchema
from feature_store.store import FeatureStore

ROOT = Path(__file__).resolve().parents[1]
DAY1 = ROOT / "examples" / "user_features_ts.csv"  # all rows at 2026-01-01T10:00Z
DAY2 = ROOT / "examples" / "user_features_day2.csv"  # new rows on 2026-01-02
PLAIN = ROOT / "examples" / "user_features.csv"  # no event_timestamp

UTC = timezone.utc
T_DAY1_END = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
T_DAY2_END = datetime(2026, 1, 2, 12, 0, tzinfo=UTC)
NOW = datetime(2026, 1, 3, 0, 0, tzinfo=UTC)


def _schema(ttl: int | None = None) -> FeatureSchema:
    return FeatureSchema(
        name="user_features",
        version="1",
        entity_key="user_id",
        features=("avg_order_value", "orders_30d", "is_premium"),
        dtypes={"avg_order_value": "float", "orders_30d": "int", "is_premium": "bool"},
        online_ttl_seconds=ttl,
    )


def _store(tmp_path: Path, *, persist: bool = False, csv: Path = DAY1) -> FeatureStore:
    fs = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas", persist_online=persist)
    fs.register_schema(_schema(), overwrite=True)
    fs.ingest_csv("user_features", "1", csv)
    return fs


def test_first_run_covers_everything_up_to_end(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    res = fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    assert res.start is None
    assert res.end == T_DAY1_END.isoformat()
    assert res.entities_written == 4
    assert fs.watermarks.get("user_features", "1").last_materialized_end == T_DAY1_END.isoformat()
    got = fs.get_online_features("user_features", "1", ["u1"], now=NOW).features
    assert got["u1"]["avg_order_value"] == 42.5


def test_second_run_only_picks_rows_after_watermark(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    fs.ingest_csv("user_features", "1", DAY2, append=True)
    res = fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    assert res.start == T_DAY1_END.isoformat()
    assert res.rows_in_window == 4
    assert sorted(res.entity_ids) == ["u1", "u3", "u5"]  # u2/u4 had no new rows
    feats = fs.get_online_features("user_features", "1", ["u1", "u2", "u3", "u5"], now=NOW).features
    assert feats["u1"]["avg_order_value"] == 47.5  # latest event in the window wins
    assert feats["u1"]["orders_30d"] == 5
    assert feats["u2"]["avg_order_value"] == 19.0  # untouched, kept from day 1
    assert feats["u3"]["avg_order_value"] == 90.0
    assert feats["u5"]["is_premium"] is False  # new entity


def test_window_end_is_inclusive_and_start_exclusive(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    exactly = datetime(2026, 1, 1, 10, 0, tzinfo=UTC)
    assert fs.materialize_incremental("user_features", "1", end=exactly, now=NOW).entities_written == 4
    fs.ingest_csv("user_features", "1", DAY2, append=True)
    # rows at exactly the old watermark are not re-read; 09:00 day-2 row is (start, end]
    res = fs.materialize_incremental(
        "user_features", "1", end=datetime(2026, 1, 2, 9, 0, tzinfo=UTC), now=NOW
    )
    assert res.entity_ids == ["u1"]
    assert fs.get_online_features("user_features", "1", ["u1"], now=NOW).features["u1"]["avg_order_value"] == 45.0


def test_future_end_is_rejected_and_watermark_unchanged(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    with pytest.raises(ValueError, match="future"):
        fs.materialize_incremental("user_features", "1", end=NOW + timedelta(days=30), now=NOW)
    assert fs.watermarks.get("user_features", "1").last_materialized_end == T_DAY1_END.isoformat()


def test_end_defaults_to_now(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    res = fs.materialize_incremental("user_features", "1", now=NOW)
    assert res.end == NOW.isoformat()


def test_end_before_watermark_is_noop(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    res = fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    assert res.noop is True
    assert res.entities_written == 0
    assert fs.watermarks.get("user_features", "1").last_materialized_end == T_DAY2_END.isoformat()


def test_requires_event_timestamp(tmp_path: Path) -> None:
    fs = _store(tmp_path, csv=PLAIN)
    with pytest.raises(ValueError, match="event_timestamp"):
        fs.materialize_incremental("user_features", "1", now=NOW)


def test_full_materialize_sets_watermark_then_incremental_continues(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.materialize("user_features", "1", materialized_at=T_DAY1_END.isoformat())
    wm = fs.watermarks.get("user_features", "1")
    assert wm.mode == "full" and wm.last_materialized_end == T_DAY1_END.isoformat()
    fs.ingest_csv("user_features", "1", DAY2, append=True)
    res = fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    assert res.start == T_DAY1_END.isoformat()
    assert res.entities_written == 3


def test_watermark_persists_across_processes(tmp_path: Path) -> None:
    fs = _store(tmp_path, persist=True)
    fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    reopened = FeatureStore(root=tmp_path / "data", schema_dir=tmp_path / "schemas")
    wm = reopened.watermarks.get("user_features", "1")
    assert wm is not None and wm.mode == "incremental"
    assert wm.last_materialized_end == T_DAY1_END.isoformat()


def test_on_write_transforms_run_for_incremental_rows(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.register_transform(
        "aov_x2",
        feature_view="user_features",
        version="1",
        inputs=["avg_order_value"],
        outputs=["aov_x2"],
        fn=lambda row: {"aov_x2": row["avg_order_value"] * 2},
        mode="on_write",
    )
    fs.materialize_incremental("user_features", "1", end=T_DAY1_END, now=NOW)
    assert fs.get_online_features("user_features", "1", ["u1"], now=NOW).features["u1"]["aov_x2"] == 85.0


# --- freshness -----------------------------------------------------------------


def test_freshness_is_now_minus_watermark(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    never = fs.freshness("user_features", "1", now=NOW)
    assert never.freshness_seconds is None and never.stale is True
    fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    f = fs.freshness("user_features", "1", now=NOW, max_staleness_seconds=3600)
    assert f.freshness_seconds == 12 * 3600
    assert f.stale is True
    assert fs.freshness("user_features", "1", now=NOW, max_staleness_seconds=86400).stale is False


def test_lookup_reports_freshness(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    res = fs.get_online_features("user_features", "1", ["u1"], now=NOW)
    assert res.freshness_seconds == 12 * 3600
    assert res.to_dict()["freshness_seconds"] == 12 * 3600


def test_prometheus_gauge(tmp_path: Path) -> None:
    fs = _store(tmp_path)
    fs.register_schema(
        FeatureSchema(name="other", version="2", entity_key="id", features=("x",)), overwrite=True
    )
    fs.materialize_incremental("user_features", "1", end=T_DAY2_END, now=NOW)
    text = fs.freshness_metrics(now=NOW)
    assert f"# TYPE {METRIC_NAME} gauge" in text
    assert f'{METRIC_NAME}{{feature_view="user_features",version="1"}} 43200.0' in text
    assert f'{METRIC_NAME}{{feature_view="other",version="2"}} NaN' in text


def test_watermark_store_memory_roundtrip() -> None:
    ws = WatermarkStore(None)
    assert ws.get("v", "1") is None
    ws.set(Watermark("v", "1", "2026-01-01T00:00:00+00:00", "x", "full"))
    ws.set(Watermark("v", "1", "2026-01-02T00:00:00+00:00", "y", "incremental"))
    assert ws.get("v", "1").mode == "incremental"
    assert [w.feature_view for w in ws.all()] == ["v"]
    assert prometheus_text([]).startswith("# HELP")


def test_append_rejects_unknown_columns(tmp_path: Path) -> None:
    fs = _store(tmp_path, csv=PLAIN)  # table without event_timestamp
    with pytest.raises(ValueError, match="cannot append"):
        fs.ingest_csv("user_features", "1", DAY2, append=True)


# --- CLI -----------------------------------------------------------------------------


def test_cli_incremental_and_freshness(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    base = ["--root", str(tmp_path / "data"), "--schema-dir", str(tmp_path / "schemas")]
    assert main(base + ["register", "--name", "user_features", "--version", "1",
                        "--entity-key", "user_id", "--features", "avg_order_value",
                        "orders_30d", "is_premium", "--dtypes", "float", "int", "bool"]) == 0
    assert main(base + ["ingest", "--name", "user_features", "--version", "1", "--csv", str(DAY1)]) == 0
    assert main(base + ["materialize", "--name", "user_features", "--version", "1",
                        "--incremental", "--end", "2026-01-01T12:00:00Z"]) == 0
    assert main(base + ["ingest", "--name", "user_features", "--version", "1",
                        "--csv", str(DAY2), "--append"]) == 0
    capsys.readouterr()
    assert main(base + ["materialize", "--name", "user_features", "--version", "1",
                        "--incremental", "--end", "2026-01-02T12:00:00Z"]) == 0
    assert '"entities_written": 3' in capsys.readouterr().out
    # future end → exit 2
    assert main(base + ["materialize", "--name", "user_features", "--version", "1",
                        "--incremental", "--end", "2999-01-01T00:00:00Z"]) == 2
    # watermark is in 2026 → way staler than 60s → exit 1; generous bound → 0
    assert main(base + ["freshness", "--name", "user_features", "--max-staleness-seconds", "60"]) == 1
    assert main(base + ["freshness", "--name", "user_features", "--max-staleness-seconds", "1e12"]) == 0
    capsys.readouterr()
    assert main(base + ["freshness", "--prometheus"]) == 0
    assert METRIC_NAME in capsys.readouterr().out
    assert main(base + ["materialize", "--name", "user_features", "--version", "1",
                        "--end", "2026-01-01T12:00:00Z"]) == 2  # --end needs --incremental
