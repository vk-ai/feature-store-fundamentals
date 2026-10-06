"""Materialization watermarks + feature freshness ("how stale is online?").

Incremental materialization only copies offline rows whose ``event_timestamp``
falls in ``(watermark, end]`` and then advances the watermark to ``end``.
The watermark is stored per feature view (SQLite next to the online store, or
in memory), so a later run (or another process) continues from where the
last one stopped.

**Freshness** of a view is ``now - last_materialized_end``: how far the
online store lags behind the offline source. It is exposed as a gauge in the
Prometheus text format, the same idea as Feast's
``feast_feature_freshness_seconds`` metric (OSS/learning re-implementation,
not Feast code).

Teaching stub: no scheduler and no distributed locking. Late-arriving rows
(``event_timestamp <= watermark`` ingested after the run) are **not** picked
up by later incremental runs; run a full ``materialize`` (or backfill) for
those. Feast has the same caveat.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from feature_store.online import _parse_iso, _utcnow

METRIC_NAME = "feature_store_feature_freshness_seconds"


@dataclass(frozen=True)
class Watermark:
    feature_view: str
    version: str
    last_materialized_end: str  # ISO-8601 UTC (event-time high-water mark)
    updated_at: str  # wall-clock time of the run that set it
    mode: str  # "full" | "incremental"


@dataclass(frozen=True)
class Freshness:
    feature_view: str
    version: str
    last_materialized_end: str | None
    freshness_seconds: float | None  # None = never materialized
    max_staleness_seconds: float | None = None

    @property
    def stale(self) -> bool:
        if self.freshness_seconds is None:
            return True
        if self.max_staleness_seconds is None:
            return False
        return self.freshness_seconds > self.max_staleness_seconds

    def to_dict(self) -> dict:
        return {
            "feature_view": self.feature_view,
            "version": self.version,
            "last_materialized_end": self.last_materialized_end,
            "freshness_seconds": self.freshness_seconds,
            "max_staleness_seconds": self.max_staleness_seconds,
            "stale": self.stale,
        }


class WatermarkStore:
    """Per-view watermark table (SQLite when ``db_path`` is given, else memory)."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path else None
        self._mem: dict[tuple[str, str], Watermark] = {}
        if self.db_path is not None:
            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect() as conn:
                conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS materialization_watermarks (
                        feature_view TEXT NOT NULL,
                        version TEXT NOT NULL,
                        last_materialized_end TEXT NOT NULL,
                        updated_at TEXT NOT NULL,
                        mode TEXT NOT NULL,
                        PRIMARY KEY (feature_view, version)
                    )
                    """
                )
                conn.commit()

    def _connect(self) -> sqlite3.Connection:
        assert self.db_path is not None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def get(self, feature_view: str, version: str) -> Watermark | None:
        if self.db_path is None:
            return self._mem.get((feature_view, version))
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM materialization_watermarks WHERE feature_view = ? AND version = ?",
                (feature_view, version),
            ).fetchone()
        return Watermark(**dict(row)) if row else None

    def set(self, wm: Watermark) -> None:
        if self.db_path is None:
            self._mem[(wm.feature_view, wm.version)] = wm
            return
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO materialization_watermarks
                    (feature_view, version, last_materialized_end, updated_at, mode)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(feature_view, version) DO UPDATE SET
                    last_materialized_end = excluded.last_materialized_end,
                    updated_at = excluded.updated_at,
                    mode = excluded.mode
                """,
                (wm.feature_view, wm.version, wm.last_materialized_end, wm.updated_at, wm.mode),
            )
            conn.commit()

    def all(self) -> list[Watermark]:
        if self.db_path is None:
            return sorted(self._mem.values(), key=lambda w: (w.feature_view, w.version))
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM materialization_watermarks ORDER BY feature_view, version"
            ).fetchall()
        return [Watermark(**dict(r)) for r in rows]


def to_utc(ts: datetime | str) -> datetime:
    """Parse ISO strings (``Z`` ok) / naive datetimes as UTC."""
    if isinstance(ts, str):
        return _parse_iso(ts)
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def freshness_for(
    wm: Watermark | None,
    feature_view: str,
    version: str,
    *,
    now: datetime | None = None,
    max_staleness_seconds: float | None = None,
) -> Freshness:
    now = to_utc(now or _utcnow())
    if wm is None:
        return Freshness(feature_view, version, None, None, max_staleness_seconds)
    age = (now - to_utc(wm.last_materialized_end)).total_seconds()
    return Freshness(
        feature_view,
        version,
        wm.last_materialized_end,
        round(max(age, 0.0), 3),
        max_staleness_seconds,
    )


def prometheus_text(items: Iterable[Freshness]) -> str:
    """Prometheus text exposition for the freshness gauge (one sample per view).

    Never-materialized views are reported as ``NaN`` so dashboards show a gap
    instead of a misleading 0.
    """
    lines = [
        f"# HELP {METRIC_NAME} Seconds since the online store's last materialized event-time end.",
        f"# TYPE {METRIC_NAME} gauge",
    ]
    for f in items:
        val = "NaN" if f.freshness_seconds is None else repr(float(f.freshness_seconds))
        lines.append(f'{METRIC_NAME}{{feature_view="{f.feature_view}",version="{f.version}"}} {val}')
    return "\n".join(lines) + "\n"
