"""CLI: register, ingest, materialize (+ --incremental), get-online-features, check-parity, freshness."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from feature_store.schema import FeatureSchema, SchemaMismatchError
from feature_store.store import FeatureStore
from feature_store.transforms import MissingRequestDataError, load_transforms_file


def _store(args: argparse.Namespace) -> FeatureStore:
    store = FeatureStore(root=args.root, schema_dir=args.schema_dir)
    transforms_file = getattr(args, "transforms", None)
    if transforms_file:
        load_transforms_file(store, transforms_file)
    return store


def _add_transforms_arg(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--transforms",
        type=Path,
        default=None,
        help="Python file defining register_transforms(store) (request-time transforms)",
    )


def cmd_register(args: argparse.Namespace) -> int:
    store = _store(args)
    schema = FeatureSchema(
        name=args.name,
        version=args.version,
        entity_key=args.entity_key,
        features=tuple(args.features),
        dtypes=dict(zip(args.features, args.dtypes)) if args.dtypes else {},
        description=args.description or "",
        online_ttl_seconds=args.online_ttl_seconds,
    )
    path = store.register_schema(schema, overwrite=args.overwrite)
    print(f"Registered {schema.name}@{schema.version} -> {path}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = _store(args)
    n = store.ingest_csv(args.name, args.version, args.csv, append=args.append)
    verb = "Appended" if args.append else "Ingested"
    print(f"{verb} {n} rows into offline store for {args.name}@{args.version}")
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    store = _store(args)
    if args.incremental:
        try:
            res = store.materialize_incremental(args.name, args.version, end=args.end)
        except ValueError as exc:
            print(f"ERROR: {exc}", file=sys.stderr)
            return 2
        print(json.dumps(res.to_dict(), indent=2))
        return 0
    if args.end:
        print("ERROR: --end only applies with --incremental", file=sys.stderr)
        return 2
    n = store.materialize(args.name, args.version)
    print(f"Materialized {n} entities to online store for {args.name}@{args.version}")
    return 0


def cmd_freshness(args: argparse.Namespace) -> int:
    """Report how stale the online store is (exit 1 if over --max-staleness-seconds)."""
    store = _store(args)
    if args.prometheus:
        print(store.freshness_metrics(), end="")
        return 0
    schemas = store.list_schemas(args.name)
    if args.version:
        schemas = [s for s in schemas if s.version == args.version]
    items = [
        store.freshness(s.name, s.version, max_staleness_seconds=args.max_staleness_seconds)
        for s in schemas
    ]
    print(json.dumps([f.to_dict() for f in items], indent=2))
    return 1 if args.max_staleness_seconds is not None and any(f.stale for f in items) else 0


def cmd_get(args: argparse.Namespace) -> int:
    store = _store(args)
    request_data = json.loads(args.request_json) if args.request_json else None
    try:
        result = store.get_online_features(
            args.name, args.version, args.entities, request_data=request_data
        )
    except SchemaMismatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    except MissingRequestDataError as exc:
        print(f"ERROR: {exc.args[0]} (pass --request-json)", file=sys.stderr)
        return 2
    print(json.dumps(result.to_dict(), indent=2, default=str))
    return 0



def cmd_get_historical(args: argparse.Namespace) -> int:
    """Join entity CSV against offline features (optional created-time as-of)."""
    import csv

    store = _store(args)
    with Path(args.entity_csv).open(newline="", encoding="utf-8") as fh:
        entity_rows = list(csv.DictReader(fh))
    rows = store.get_historical_features(
        args.name,
        args.version,
        entity_rows,
        as_of_known_time=bool(args.as_of_known_time),
    )
    print(json.dumps({"as_of_known_time": bool(args.as_of_known_time), "rows": rows}, indent=2, default=str))
    return 0


def cmd_check_parity(args: argparse.Namespace) -> int:
    """Compare online vs offline values for sampled entities; write optional report."""
    store = _store(args)
    report = store.check_parity(
        args.name,
        args.version,
        args.entities,
        rtol=args.rtol,
        atol=args.atol,
    )
    if args.report:
        out = report.write_json(args.report)
        print(f"Wrote parity report -> {out}")
    print(json.dumps(report.to_dict(), indent=2, default=str))
    return 0 if report.passed else 1

def cmd_list(args: argparse.Namespace) -> int:
    store = _store(args)
    for schema in store.list_schemas(args.name):
        print(f"{schema.name}@{schema.version} entity={schema.entity_key} features={list(schema.features)}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="feature-store",
        description="Minimal Feast-style feature store (OSS learning demo; not employer production).",
    )
    p.add_argument("--root", default="data", help="Data root directory (default: data)")
    p.add_argument(
        "--schema-dir",
        default=None,
        help="Schema JSON directory (default: <root>/schemas)",
    )
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("register", help="Register a feature view schema")
    r.add_argument("--name", required=True)
    r.add_argument("--version", required=True)
    r.add_argument("--entity-key", required=True)
    r.add_argument("--features", nargs="+", required=True)
    r.add_argument("--dtypes", nargs="*", default=None, help="Optional dtypes aligned to --features")
    r.add_argument("--description", default="")
    r.add_argument(
        "--online-ttl-seconds",
        type=int,
        default=None,
        help="Optional toy online TTL (expire-on-read); not Feast/Redis TTL",
    )
    r.add_argument("--overwrite", action="store_true")
    r.set_defaults(func=cmd_register)

    i = sub.add_parser("ingest", help="Ingest CSV into offline store")
    i.add_argument("--name", required=True)
    i.add_argument("--version", required=True)
    i.add_argument("--csv", required=True, type=Path)
    i.add_argument(
        "--append", action="store_true", help="Append rows (new data) instead of replacing the table"
    )
    i.set_defaults(func=cmd_ingest)

    m = sub.add_parser("materialize", help="Materialize offline -> online")
    m.add_argument("--name", required=True)
    m.add_argument("--version", required=True)
    m.add_argument(
        "--incremental",
        action="store_true",
        help="Only rows with watermark < event_timestamp <= end; then advance the watermark",
    )
    m.add_argument(
        "--end", default=None, help="ISO-8601 end for --incremental (default: now; future rejected)"
    )
    _add_transforms_arg(m)
    m.set_defaults(func=cmd_materialize)

    fr = sub.add_parser(
        "freshness", help="How stale is online? now - last materialized end (per view)"
    )
    fr.add_argument("--name", default=None)
    fr.add_argument("--version", default=None)
    fr.add_argument(
        "--max-staleness-seconds",
        type=float,
        default=None,
        help="Exit 1 if any selected view is staler than this (or never materialized)",
    )
    fr.add_argument(
        "--prometheus", action="store_true", help="Print the freshness gauge in Prometheus text format"
    )
    fr.set_defaults(func=cmd_freshness)

    g = sub.add_parser("get-online-features", help="Lookup online features by entity id")
    g.add_argument("--name", required=True)
    g.add_argument("--version", required=True)
    g.add_argument("--entities", nargs="+", required=True)
    _add_transforms_arg(g)
    g.add_argument(
        "--request-json",
        default=None,
        help='Request-time data for on_read transforms, e.g. \'{"cart_value": 50}\'',
    )
    g.set_defaults(func=cmd_get)

    l = sub.add_parser("list-schemas", help="List registered schemas")
    l.add_argument("--name", default=None)
    l.set_defaults(func=cmd_list)


    h = sub.add_parser(
        "get-historical-features",
        help="Toy PIT / as-of join (entity CSV × offline); not Feast ASOF",
    )
    h.add_argument("--name", required=True)
    h.add_argument("--version", required=True)
    h.add_argument("--entity-csv", required=True, type=Path, help="CSV with entity_key + event_timestamp")
    h.add_argument(
        "--as-of-known-time",
        action="store_true",
        help="Also require created_timestamp <= entity event_timestamp (leak-free mode)",
    )
    _add_transforms_arg(h)
    h.set_defaults(func=cmd_get_historical)


    cp = sub.add_parser(
        "check-parity",
        help="Online↔offline sampled value parity (not schema-only validate)",
    )
    cp.add_argument("--name", required=True)
    cp.add_argument("--version", required=True)
    cp.add_argument("--entities", nargs="+", required=True)
    cp.add_argument("--rtol", type=float, default=1e-5)
    cp.add_argument("--atol", type=float, default=1e-8)
    cp.add_argument("--report", type=Path, default=None, help="Write JSON report path")
    cp.set_defaults(func=cmd_check_parity)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
