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
feature_store/     # schema, offline, online, store, cli
schemas/           # example feature-view JSON
examples/          # sample CSV
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

## Design notes (learning)

1. **Offline** holds historical / batch features (CSV → SQLite).
2. **Online** is optimized for entity-key reads at serving time.
3. **Materialize** is the bridge: batch job that publishes the latest offline snapshot online.
4. **Schema versions** let you evolve feature views without silently mixing incompatible payloads; mismatches raise `SchemaMismatchError`.

This mirrors ideas popularized by Feast and similar stores, stripped down for teaching.

## CI

A GitHub Actions workflow is checked in as [`ci/github-actions.yml`](ci/github-actions.yml) (mirror). Enabling it under `.github/workflows/` requires a token with the `workflow` scope.

## License

MIT — see [LICENSE](LICENSE).

## Disclaimer

Built for open-source learning and portfolio demos by [vk-ai](https://github.com/vk-ai). Not affiliated with, endorsed by, or derived from any employer production feature store.
