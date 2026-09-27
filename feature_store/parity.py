"""Online ↔ offline sampled value parity check (CI-friendly teaching stub).

Schema-only validation is not enough — community Feast ops guides emphasize
value-level parity after materialize (Neural Base offline-online mismatch,
LabHub skew_prevention_test patterns). This module compares online KV values
to the offline table for sampled entity keys and emits a small report.

Honesty: toy store sharing one write path will usually be exact; production
skew monitoring is not claimed from a laptop KV. Not Feast ``validate``.
"""

from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from feature_store.schema import FeatureSchema


def _is_number(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool)


def _values_close(a: Any, b: Any, *, rtol: float, atol: float) -> bool:
    if a is None and b is None:
        return True
    if a is None or b is None:
        return False
    if _is_number(a) and _is_number(b):
        return math.isclose(float(a), float(b), rel_tol=rtol, abs_tol=atol)
    return a == b


@dataclass
class FeatureSkew:
    entity_id: str
    feature: str
    offline: Any
    online: Any
    abs_err: float | None = None
    rel_err: float | None = None


@dataclass
class ParityReport:
    feature_view: str
    version: str
    sample_size: int
    compared: int
    mismatches: list[FeatureSkew] = field(default_factory=list)
    missing_online: list[str] = field(default_factory=list)
    missing_offline: list[str] = field(default_factory=list)
    null_mismatches: int = 0
    max_abs_err: float = 0.0
    max_rel_err: float = 0.0
    passed: bool = True
    rtol: float = 1e-5
    atol: float = 1e-8
    checked_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    def write_json(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=str) + "\n")
        return path


def check_parity(
    store: Any,
    name: str,
    version: str,
    entity_ids: list[str],
    *,
    rtol: float = 1e-5,
    atol: float = 1e-8,
) -> ParityReport:
    """Compare online store values vs offline rows for sampled entity ids."""
    schema: FeatureSchema = store.schemas.get(name, version)
    offline_map = {
        eid: feats for eid, feats in store.offline.iter_entity_features(schema)
    }
    online_result = store.get_online_features(name, version, entity_ids)
    online_map = online_result.features

    report = ParityReport(
        feature_view=name,
        version=version,
        sample_size=len(entity_ids),
        compared=0,
        rtol=rtol,
        atol=atol,
    )

    for eid in entity_ids:
        eid_s = str(eid)
        off = offline_map.get(eid_s)
        on = online_map.get(eid_s)

        if off is None:
            report.missing_offline.append(eid_s)
            report.passed = False
            continue
        if on is None:
            report.missing_online.append(eid_s)
            report.passed = False
            continue

        report.compared += 1
        for feat in schema.features:
            ov = off.get(feat)
            nv = on.get(feat)
            if (ov is None) != (nv is None):
                report.null_mismatches += 1
                report.mismatches.append(
                    FeatureSkew(entity_id=eid_s, feature=feat, offline=ov, online=nv)
                )
                report.passed = False
                continue
            if not _values_close(ov, nv, rtol=rtol, atol=atol):
                abs_err = None
                rel_err = None
                if _is_number(ov) and _is_number(nv):
                    abs_err = abs(float(ov) - float(nv))
                    denom = max(abs(float(ov)), abs(float(nv)), 1e-12)
                    rel_err = abs_err / denom
                    report.max_abs_err = max(report.max_abs_err, abs_err)
                    report.max_rel_err = max(report.max_rel_err, rel_err)
                report.mismatches.append(
                    FeatureSkew(
                        entity_id=eid_s,
                        feature=feat,
                        offline=ov,
                        online=nv,
                        abs_err=abs_err,
                        rel_err=rel_err,
                    )
                )
                report.passed = False

    return report
