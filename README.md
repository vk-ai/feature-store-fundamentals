# feature-store-fundamentals

**OSS / learning only.** This repository is a small educational demo of Feast-style feature-store fundamentals (offline tables, online entity lookup, schema versioning). It is **not** employer production software, not a production feature platform, and does **not** represent any employer’s systems or data.

## What you get

A minimal, CPU-only Python package (stdlib runtime: `sqlite3`, `csv`, `json`) that demonstrates:

| Concept | Implementation |
| --- | --- |
| Offline feature table | CSV ingest → SQLite offline tables |
| Online lookup | In-memory dict + optional SQLite, keyed by `(feature_view, version, entity_id)` |
| Schema versioning | Feature-view schemas as JSON (`name` + `version` + feature list / dtypes) |
| Materialization | `materialize` copies offline → online (stamps `materialized_at`) |
| Toy online TTL | Optional `online_ttl_seconds`; expire-on-read + `expired` count |
| Serving API / CLI | `get_online_features` / `feature-store get-online-features` |
| Online↔offline parity | `check_parity` / `feature-store check-parity` + JSON report |
| Incremental materialize + freshness | `materialize --incremental` with a stored per-view watermark; `freshness_seconds` + Prometheus gauge |
| Request-time transforms | `register_transform(mode="on_read" / "on_write")`: one fn for historical + online; `check_transform_parity` |

No Feast dependency, no GPU, no heavy ML stack.

## Quick start

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Register schema, ingest sample CSV, materialize, lookup
feature-store --root data --schema-dir schemas register \
  --name user_features --version 1 --entity-key user_id \
  --features avg_order_value orders_30d is_premium \
  --dtypes float int bool

feature-store --root data --schema-dir schemas ingest \
  --name user_features --version 1 --csv examples/user_features.csv

feature-store --root data --schema-dir schemas materialize \
  --name user_features --version 1

feature-store --root data --schema-dir schemas get-online-features \
  --name user_features --version 1 --entities u1 u2
```

Or use the Python API:

```python
from pathlib import Path
from feature_store import FeatureStore, FeatureSchema

store = FeatureStore(root="data", schema_dir="schemas")
schema = FeatureSchema.load("schemas/user_features_v1.json")
store.register_schema(schema, overwrite=True)
store.ingest_csv("user_features", "1", "examples/user_features.csv")
store.materialize("user_features", "1")
print(store.get_online_features("user_features", "1", ["u1", "u2"]).to_dict())
```

## Tests

```bash
pytest -q
```

Coverage includes successful online lookup after materialize and schema / version mismatch errors.

## Layout

```
feature_store/     # schema, offline, online, store, transforms, freshness, cli
schemas/           # example feature-view JSON
examples/          # sample CSVs + transforms_demo.py
tests/             # pytest
ci/github-actions.yml  # CI workflow mirror (copy to .github/workflows/ci.yml if token has workflow scope)
```

## Toy online TTL (expire-on-read)

Optional schema field `online_ttl_seconds` + `materialized_at` stamp on materialize:

- `materialize` writes an ISO-UTC `materialized_at` on every online row
- `get_online_features` **drops** entities whose age exceeds the TTL and reports `expired` count
- Example: `OnlineFeaturesResult(features={...}, expired=2)`

```python
schema = FeatureSchema(
    name="user_features",
    version="1",
    entity_key="user_id",
    features=("avg_order_value", "orders_30d", "is_premium"),
    online_ttl_seconds=60,  # optional
)
```

**Honesty vs Feast TTL:** this demo TTL means “discard stale online rows on read.”
Real Feast `FeatureView.ttl` semantics are subtler (historical lookback / materialize
windows; see [feast#4133](https://github.com/feast-dev/feast/issues/4133)). This is
**not** Redis `EXPIRE`, not Feast `key_ttl_seconds`, and not offline point-in-time joins.
OSS/learning only — not employer production.


## Toy point-in-time / as-of join (created_timestamp)

Offline training joins need more than “latest feature before event time.” If a row’s
**created_timestamp** is after the training example’s timestamp, using it is **label leakage**.

This demo adds:

- Optional CSV columns `event_timestamp` + `created_timestamp` (preserved on ingest)
- `FeatureStore.get_historical_features(entity_rows, as_of_known_time=...)`
- CLI: `feature-store get-historical-features ... [--as-of-known-time]`
- Fixture: `examples/user_features_pit.csv` (deliberate leak) + `examples/entity_df_pit.csv`

```text
entity @ T=11:00
  feature A  event=10:00  created=09:00  → eligible always
  feature B  event=10:00  created=12:00  → eligible if event-time only (LEAK)
                                         → dropped if --as-of-known-time
```

**Honesty vs Feast:** real Feast supports `filter_by_created_timestamp`
([feast#6615](https://github.com/feast-dev/feast/issues/6615) /
[#6617](https://github.com/feast-dev/feast/pull/6617), v0.66+). This repo is a
**pandas mental-model stub** (stdlib CSV/SQLite) — not Feast SQL templates, not
streaming watermarks, not employer production. Online TTL from Round 1 is unchanged.

## Design notes (learning)

1. **Offline** holds historical / batch features (CSV → SQLite).
2. **Online** is optimized for entity-key reads at serving time.
3. **Materialize** is the bridge: batch job that publishes the latest offline snapshot online (full, or incremental from a stored watermark).
4. **Schema versions** let you evolve feature views without silently mixing incompatible payloads; mismatches raise `SchemaMismatchError`.

This mirrors ideas popularized by Feast and similar stores, stripped down for teaching.

## CI

A GitHub Actions workflow is checked in as [`ci/github-actions.yml`](ci/github-actions.yml) (mirror). Enabling it under `.github/workflows/` requires a token with the `workflow` scope.


## Online ↔ offline sampled parity check

Schema-only validate is **not** enough. After materialize, sample entity keys and
compare online KV values to the offline table (per-feature equality within
`rtol`/`atol`, null mismatches, missing keys). Emit `parity_report.json`.

```bash
feature-store --root data --schema-dir schemas check-parity \
  --name user_features --version 1 --entities u1 u2 \
  --report parity_report.json
```

```python
report = store.check_parity("user_features", "1", ["u1", "u2"])
assert report.passed
report.write_json("parity_report.json")
```

Intentional skew (tests): mutate online after materialize → `passed=False` with
per-feature abs/rel error.

> **Honesty:** Feast `validate` is schema/connectivity — **value parity is always
> custom**. This toy store teaches the contract; it is not Feast, Redis, Spark, or
> production skew monitoring. Refs: [Neural Base offline-online mismatch](https://theneuralbase.com/feature-store/learn/beginner/offline-online-mismatch/),
> [LabHub Feast ops / parity rate](https://www.youngju.dev/blog/ai-platform/2026-03-07-ai-platform-feast-feature-store-real-time-serving.en),
> [feature-store-skew pytest demo](https://github.com/vishnup22/feature-store-skew).

## Incremental materialize: watermark + freshness

A full `materialize` re-copies every offline row. `materialize --incremental` copies only rows
with `watermark < event_timestamp <= end`, upserts them online, and then **advances a stored
watermark** to `end`. The watermark is kept per feature view, in the
`materialization_watermarks` table next to the online rows, so the next run (even from another
process) continues from there.

```bash
# day 1: rows at 2026-01-01T10:00Z
feature-store --root data --schema-dir schemas ingest --name user_features --version 1 --csv examples/user_features_ts.csv
feature-store --root data --schema-dir schemas materialize --name user_features --version 1 --incremental --end 2026-01-01T12:00:00Z
# day 2: new rows arrive (append), only u1/u3/u5 are rewritten
feature-store --root data --schema-dir schemas ingest --name user_features --version 1 --csv examples/user_features_day2.csv --append
feature-store --root data --schema-dir schemas materialize --name user_features --version 1 --incremental --end 2026-01-02T12:00:00Z
# how stale is online?  exit 1 if over the bound (or never materialized)
feature-store --root data --schema-dir schemas freshness --name user_features --max-staleness-seconds 3600
feature-store --root data --schema-dir schemas freshness --prometheus
```

Rules the code and tests pin:

- `end` defaults to now. **A future `end` is rejected** (exit 2): moving the watermark past now
  would silently skip rows that arrive later. This is the classic incremental trap from
  [feast#4222](https://github.com/feast-dev/feast/issues/4222).
- The window is start-exclusive and end-inclusive. Per entity, the latest `event_timestamp` in the
  window wins. Entities with no new rows keep their online value. `on_write` transforms run as
  usual. The watermark never moves backwards: `end <= watermark` is a no-op.
- A full `materialize` also records its stamp as the watermark, so incremental runs can follow it.

**Freshness** = `now - last_materialized_end`: how far online lags the offline source. It is
returned by `store.freshness(...)`, as `freshness_seconds` on every `get_online_features`
result, and as a Prometheus gauge:

```text
# TYPE feature_store_feature_freshness_seconds gauge
feature_store_feature_freshness_seconds{feature_view="user_features",version="1"} 43200.0
```

Never-materialized views report `NaN` (a gap), not `0`. Freshness is about the *view*;
the Round-1 TTL is about individual rows. Note that incremental runs only re-stamp the entities
they write, so unchanged entities can still TTL-expire.

> **Honesty:** a stdlib re-implementation of the *idea* behind Feast's `materialize-incremental`
> and its `feast_feature_freshness_seconds` gauge
> ([feast commit 2c6be18](https://github.com/feast-dev/feast/commit/2c6be18bfee9d4bf18ae59490160282b090d3b62)).
> It has no scheduler, no locking, and no windowed/chunked backfill
> ([feast#6307](https://github.com/feast-dev/feast/issues/6307) is the OOM story that motivates
> chunking). Late-arriving rows (`event_timestamp <= watermark`) are not picked up by later
> incremental runs; use a full `materialize` for those. Not employer production.

## Request-time transforms: on-read vs on-write

Storage, TTL, PIT, and parity leave one fundamental open: **where does feature logic run?** A transform derives features from stored features, and optionally from **request data** that only exists at serving time (e.g. `cart_value`). The same Python function feeds both the historical path and the online path.

| `mode` | Runs in | Can use request data? | Trade-off |
|---|---|---|---|
| `on_read` (default) | `get_online_features(..., request_data=…)` **and** `get_historical_features(entity_rows)` | yes | always fresh; compute on every read |
| `on_write` | `materialize` (outputs stored online) **and** `get_historical_features` | **no**: there is no request at materialize time, so registering one with `request_schema` raises `TransformError` | cheap reads; values only as fresh as the last materialize |

```python
store.register_transform(
    "cart_to_aov_ratio",
    feature_view="user_features", version="1",
    inputs=["avg_order_value"], request_schema={"cart_value": "float"},
    outputs=["cart_to_aov_ratio"],
    fn=lambda r: {"cart_to_aov_ratio": round(r["cart_value"] / r["avg_order_value"], 4)},
    mode="on_read",
)
store.get_online_features("user_features", "1", ["u1"], request_data={"cart_value": 85})
store.get_historical_features("user_features", "1", entity_rows)  # rows carry cart_value
```

Both paths go through one helper, `apply_transform`, which also owns request dtype coercion and the rule "null in → null out" (no stored row → outputs `None`). `check_transform_parity(store, name, version, entity_rows)` compares historical vs online transform outputs and returns the same `ParityReport` shape as `check_parity`. `store.transforms.stats` shows where compute happened (`on_read_rows` vs `on_write_rows`).

```bash
feature-store --root data --schema-dir schemas ingest --name user_features --version 1 --csv examples/user_features_ts.csv
feature-store --root data --schema-dir schemas materialize --name user_features --version 1 \
  --transforms examples/transforms_demo.py            # on_write: orders_per_week stored online
feature-store --root data --schema-dir schemas get-online-features --name user_features --version 1 \
  --entities u1 --transforms examples/transforms_demo.py --request-json '{"cart_value": 85}'
feature-store --root data --schema-dir schemas get-historical-features --name user_features --version 1 \
  --entity-csv examples/entity_df_request.csv --transforms examples/transforms_demo.py
pytest tests/test_transforms.py -q
```

Transforms are Python code, so they are registered at process start (CLI: `--transforms file.py` defining `register_transforms(store)`) rather than persisted to JSON. Re-run `materialize` after changing an `on_write` transform.

> **Honesty:** a stdlib mental model of Feast `@on_demand_feature_view` / `write_to_online_store`. It is not Feast, not Tecton, and has no pandas/Substrait execution. The motivation is the [Feast blog on on-write transforms](https://feast.dev/blog/feature-transformation-latency/), [feast#4584](https://github.com/feast-dev/feast/issues/4584) (making transform timing explicit), and [feast#4376](https://github.com/feast-dev/feast/issues/4376). OSS/learning only, not employer production.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Built for open-source learning and portfolio demos by [vk-ai](https://github.com/vk-ai). Not affiliated with, endorsed by, or derived from any employer production feature store.
