"""Request-time transforms: one Python function, two places it can run.

A transform derives new features from **stored** features (and, optionally, from
**request data** that only exists at serving time, e.g. ``cart_value``).

``mode="on_read"``  (Feast: on-demand feature view, default)
    Runs inside ``get_online_features(..., request_data=...)`` *and* inside
    ``get_historical_features(entity_rows)`` (request columns come from the
    entity rows). Always fresh, costs compute on every read.

``mode="on_write"`` (Feast: ``write_to_online_store=True``)
    Runs once per entity during ``materialize``; the outputs are stored online and
    reads are a plain lookup. It **cannot** use request data — there is no request
    at materialize time — so registering one with ``request_schema`` is an error.
    ``get_historical_features`` still calls the *same* function on the PIT-joined
    rows, so training data and serving data come from one definition.

The anti-skew lesson: both paths go through :func:`apply_transform`, including the
"null in → null out" rule and request-data dtype coercion, so the offline and online
values match by construction (and :func:`check_transform_parity` proves it).

Honesty: a stdlib mental model of Feast ``@on_demand_feature_view`` — not Feast,
not Tecton, no pandas / Substrait execution, no registry persistence (transforms
are Python code, registered at process start).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping, Sequence

from feature_store.offline import _coerce
from feature_store.parity import FeatureSkew, ParityReport, _is_number, _values_close

TransformMode = Literal["on_read", "on_write"]
TransformFn = Callable[[Mapping[str, Any]], Mapping[str, Any]]


class TransformError(ValueError):
    """Invalid transform definition or transform output."""


class MissingRequestDataError(KeyError):
    """An on_read transform needs request fields that the caller did not supply."""


@dataclass(frozen=True)
class Transform:
    name: str
    feature_view: str
    version: str
    inputs: tuple[str, ...]
    outputs: tuple[str, ...]
    fn: TransformFn
    mode: TransformMode = "on_read"
    # request-only fields -> dtype (coerced the same way offline CSV values are)
    request_schema: dict[str, str] = field(default_factory=dict)
    description: str = ""


@dataclass
class TransformStats:
    """Where the compute happened — the latency trade-off made visible."""

    on_read_rows: int = 0  # rows computed at read time (online + historical)
    on_write_rows: int = 0  # rows computed during materialize


def apply_transform(
    t: Transform,
    stored: Mapping[str, Any] | None,
    request: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run ``t`` for one row. Shared by the online, materialize, and historical paths.

    - Missing request fields raise :class:`MissingRequestDataError`.
    - Request values are coerced with ``t.request_schema`` dtypes (CSV strings → numbers).
    - Null in → null out: if the entity has no stored row or any stored input is
      ``None``, every output is ``None`` and ``fn`` is not called.
    """
    request = request or {}
    missing = [k for k in t.request_schema if k not in request]
    if missing:
        raise MissingRequestDataError(
            f"transform {t.name!r} needs request data {missing} (mode={t.mode})"
        )
    if stored is None or any(stored.get(k) is None for k in t.inputs):
        return {o: None for o in t.outputs}
    row: dict[str, Any] = {k: stored.get(k) for k in t.inputs}
    for k, dtype in t.request_schema.items():
        row[k] = _coerce(request[k], dtype)
    out = dict(t.fn(row))
    if set(out) != set(t.outputs):
        raise TransformError(
            f"transform {t.name!r} returned keys {sorted(out)}; declared outputs {list(t.outputs)}"
        )
    return {o: out[o] for o in t.outputs}


class TransformRegistry:
    """In-memory registry of transforms keyed by (feature_view, version)."""

    def __init__(self) -> None:
        self._by_view: dict[tuple[str, str], list[Transform]] = {}
        self.stats: dict[str, TransformStats] = {}

    def register(self, t: Transform, *, stored_features: Sequence[str]) -> Transform:
        if t.mode not in ("on_read", "on_write"):
            raise TransformError(f"mode must be 'on_read' or 'on_write', got {t.mode!r}")
        if t.mode == "on_write" and t.request_schema:
            raise TransformError(
                f"transform {t.name!r}: on_write runs at materialize time, when no request "
                f"exists, so it cannot use request data {sorted(t.request_schema)}. "
                "Use mode='on_read' for request-time inputs."
            )
        unknown = [i for i in t.inputs if i not in stored_features]
        if unknown:
            raise TransformError(f"transform {t.name!r}: unknown stored inputs {unknown}")
        if not t.outputs:
            raise TransformError(f"transform {t.name!r}: declare at least one output")
        existing = self.for_view(t.feature_view, t.version)
        taken = set(stored_features) | {o for x in existing if x.name != t.name for o in x.outputs}
        clash = sorted(set(t.outputs) & taken)
        if clash:
            raise TransformError(f"transform {t.name!r}: outputs {clash} collide with existing features")
        key = (t.feature_view, t.version)
        self._by_view[key] = [x for x in existing if x.name != t.name] + [t]
        self.stats.setdefault(t.name, TransformStats())
        return t

    def for_view(self, feature_view: str, version: str, mode: TransformMode | None = None) -> list[Transform]:
        items = list(self._by_view.get((feature_view, version), []))
        return [t for t in items if mode is None or t.mode == mode]

    def output_names(self, feature_view: str, version: str) -> list[str]:
        return [o for t in self.for_view(feature_view, version) for o in t.outputs]


def load_transforms_file(store: Any, path: Path | str) -> None:
    """Import a Python file and call its ``register_transforms(store)`` (CLI helper)."""
    path = Path(path)
    spec = importlib.util.spec_from_file_location(f"_fs_transforms_{path.stem}", path)
    if spec is None or spec.loader is None:
        raise TransformError(f"cannot import transforms file {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    register = getattr(module, "register_transforms", None)
    if not callable(register):
        raise TransformError(f"{path} must define register_transforms(store)")
    register(store)


def check_transform_parity(
    store: Any,
    name: str,
    version: str,
    entity_rows: list[dict[str, Any]],
    *,
    rtol: float = 1e-5,
    atol: float = 1e-8,
) -> ParityReport:
    """Compare transform outputs: historical path vs online path, for the same inputs.

    ``entity_rows`` carry ``entity_key`` + ``event_timestamp`` + any request columns.
    The online side receives each row's request columns as that entity's request data.
    Pick event timestamps at/after the materialized snapshot so both paths see the
    same stored inputs.
    """
    schema = store.schemas.get(name, version)
    transforms = store.transforms.for_view(name, version)
    outputs = [o for t in transforms for o in t.outputs]
    request_keys = {k for t in transforms for k in t.request_schema}

    historical = store.get_historical_features(name, version, entity_rows)
    by_entity = {
        str(r[schema.entity_key]): {k: r[k] for k in request_keys if k in r} for r in entity_rows
    }
    online = store.get_online_features(
        name, version, list(by_entity), request_data_by_entity=by_entity
    ).features

    report = ParityReport(
        feature_view=name, version=version, sample_size=len(entity_rows), compared=0, rtol=rtol, atol=atol
    )
    for hrow in historical:
        eid = str(hrow[schema.entity_key])
        on = online.get(eid)
        if on is None:
            report.missing_online.append(eid)
            report.passed = False
            continue
        report.compared += 1
        for feat in outputs:
            ov, nv = hrow.get(feat), on.get(feat)
            if _values_close(ov, nv, rtol=rtol, atol=atol):
                continue
            abs_err = rel_err = None
            if _is_number(ov) and _is_number(nv):
                abs_err = abs(float(ov) - float(nv))
                rel_err = abs_err / max(abs(float(ov)), abs(float(nv)), 1e-12)
                report.max_abs_err = max(report.max_abs_err, abs_err)
                report.max_rel_err = max(report.max_rel_err, rel_err)
            elif (ov is None) != (nv is None):
                report.null_mismatches += 1
            report.mismatches.append(
                FeatureSkew(entity_id=eid, feature=feat, offline=ov, online=nv, abs_err=abs_err, rel_err=rel_err)
            )
            report.passed = False
    return report
