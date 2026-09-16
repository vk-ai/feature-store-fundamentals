"""CLI: register, ingest, materialize, get-online-features."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from feature_store.schema import FeatureSchema, SchemaMismatchError
from feature_store.store import FeatureStore


def _store(args: argparse.Namespace) -> FeatureStore:
    return FeatureStore(root=args.root, schema_dir=args.schema_dir)


def cmd_register(args: argparse.Namespace) -> int:
    store = _store(args)
    schema = FeatureSchema(
        name=args.name,
        version=args.version,
        entity_key=args.entity_key,
        features=tuple(args.features),
        dtypes=dict(zip(args.features, args.dtypes)) if args.dtypes else {},
        description=args.description or "",
    )
    path = store.register_schema(schema, overwrite=args.overwrite)
    print(f"Registered {schema.name}@{schema.version} -> {path}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    store = _store(args)
    n = store.ingest_csv(args.name, args.version, args.csv)
    print(f"Ingested {n} rows into offline store for {args.name}@{args.version}")
    return 0


def cmd_materialize(args: argparse.Namespace) -> int:
    store = _store(args)
    n = store.materialize(args.name, args.version)
    print(f"Materialized {n} entities to online store for {args.name}@{args.version}")
    return 0


def cmd_get(args: argparse.Namespace) -> int:
    store = _store(args)
    try:
        result = store.get_online_features(args.name, args.version, args.entities)
    except SchemaMismatchError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, default=str))
    return 0


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
    r.add_argument("--overwrite", action="store_true")
    r.set_defaults(func=cmd_register)

    i = sub.add_parser("ingest", help="Ingest CSV into offline store")
    i.add_argument("--name", required=True)
    i.add_argument("--version", required=True)
    i.add_argument("--csv", required=True, type=Path)
    i.set_defaults(func=cmd_ingest)

    m = sub.add_parser("materialize", help="Materialize offline -> online")
    m.add_argument("--name", required=True)
    m.add_argument("--version", required=True)
    m.set_defaults(func=cmd_materialize)

    g = sub.add_parser("get-online-features", help="Lookup online features by entity id")
    g.add_argument("--name", required=True)
    g.add_argument("--version", required=True)
    g.add_argument("--entities", nargs="+", required=True)
    g.set_defaults(func=cmd_get)

    l = sub.add_parser("list-schemas", help="List registered schemas")
    l.add_argument("--name", default=None)
    l.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
