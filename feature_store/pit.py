"""Toy point-in-time / as-of join (pandas mental model — not Feast SQL ASOF).

Honesty
-------
Real Feast now supports ``filter_by_created_timestamp`` (v0.66+, feast#6615 / #6617).
This module is a tiny stdlib teaching stub so learners can see *why* created-time
matters for leak-free training joins. Not Feast, not Spark ASOF, not production.
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Iterable, Sequence

EVENT_TS = "event_timestamp"
CREATED_TS = "created_timestamp"


def parse_ts(value: Any) -> datetime:
    """Parse ISO-8601 (accepts trailing Z) into aware UTC datetime."""
    if isinstance(value, datetime):
        dt = value
    else:
        s = str(value).strip()
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        dt = datetime.fromisoformat(s)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _feature_payload(row: dict[str, Any], feature_names: Sequence[str]) -> dict[str, Any]:
    return {name: row.get(name) for name in feature_names}


def asof_join_row(
    entity_row: dict[str, Any],
    feature_rows: Iterable[dict[str, Any]],
    *,
    entity_key: str,
    feature_names: Sequence[str],
    event_ts_col: str = EVENT_TS,
    created_ts_col: str = CREATED_TS,
    as_of_known_time: bool = False,
) -> dict[str, Any]:
    """
    For one entity request, pick the latest eligible feature row.

    Eligibility:
      - same entity id
      - feature.event_timestamp <= entity.event_timestamp
      - if ``as_of_known_time``: also feature.created_timestamp <= entity.event_timestamp

    Among eligible rows, choose max (event_timestamp, created_timestamp).
    """
    if entity_key not in entity_row:
        raise KeyError(f"entity row missing {entity_key!r}")
    if event_ts_col not in entity_row:
        raise KeyError(f"entity row missing {event_ts_col!r}")

    entity_id = str(entity_row[entity_key])
    entity_ts = parse_ts(entity_row[event_ts_col])

    best: tuple[datetime, datetime, dict[str, Any]] | None = None
    for raw in feature_rows:
        if str(raw.get(entity_key)) != entity_id:
            continue
        if event_ts_col not in raw or raw.get(event_ts_col) in (None, ""):
            continue
        feat_event = parse_ts(raw[event_ts_col])
        if feat_event > entity_ts:
            continue
        created_raw = raw.get(created_ts_col)
        feat_created = parse_ts(created_raw) if created_raw not in (None, "") else feat_event
        if as_of_known_time and feat_created > entity_ts:
            continue  # leak filter: not yet known at entity time
        key = (feat_event, feat_created)
        if best is None or key > (best[0], best[1]):
            best = (feat_event, feat_created, raw)

    out: dict[str, Any] = {
        entity_key: entity_id,
        event_ts_col: entity_row[event_ts_col],
    }
    if best is None:
        for name in feature_names:
            out[name] = None
        out["_matched_event_timestamp"] = None
        out["_matched_created_timestamp"] = None
        return out

    _, _, row = best
    out.update(_feature_payload(row, feature_names))
    out["_matched_event_timestamp"] = row.get(event_ts_col)
    out["_matched_created_timestamp"] = row.get(created_ts_col)
    return out


def asof_join(
    entity_rows: Sequence[dict[str, Any]],
    feature_rows: Sequence[dict[str, Any]],
    *,
    entity_key: str,
    feature_names: Sequence[str],
    event_ts_col: str = EVENT_TS,
    created_ts_col: str = CREATED_TS,
    as_of_known_time: bool = False,
) -> list[dict[str, Any]]:
    """Point-in-time join for an entity dataframe (list of row dicts)."""
    return [
        asof_join_row(
            er,
            feature_rows,
            entity_key=entity_key,
            feature_names=feature_names,
            event_ts_col=event_ts_col,
            created_ts_col=created_ts_col,
            as_of_known_time=as_of_known_time,
        )
        for er in entity_rows
    ]
