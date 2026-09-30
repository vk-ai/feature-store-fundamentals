"""Example request-time transforms for ``user_features@1`` (used by README + CLI).

Load with ``feature-store ... --transforms examples/transforms_demo.py``.
OSS/learning demo only.
"""

from __future__ import annotations

from typing import Any, Mapping


def cart_to_aov_ratio(row: Mapping[str, Any]) -> dict[str, Any]:
    """on_read: needs request-time ``cart_value`` → must run at read time."""
    aov = float(row["avg_order_value"])
    ratio = None if aov == 0 else round(float(row["cart_value"]) / aov, 4)
    return {"cart_to_aov_ratio": ratio}


def orders_per_week(row: Mapping[str, Any]) -> dict[str, Any]:
    """on_write: stored inputs only → can be precomputed at materialize."""
    return {"orders_per_week": round(int(row["orders_30d"]) * 7 / 30, 4)}


def register_transforms(store: Any) -> None:
    store.register_transform(
        "cart_to_aov_ratio",
        feature_view="user_features",
        version="1",
        inputs=["avg_order_value"],
        request_schema={"cart_value": "float"},
        outputs=["cart_to_aov_ratio"],
        fn=cart_to_aov_ratio,
        mode="on_read",
    )
    store.register_transform(
        "orders_per_week",
        feature_view="user_features",
        version="1",
        inputs=["orders_30d"],
        outputs=["orders_per_week"],
        fn=orders_per_week,
        mode="on_write",
    )
