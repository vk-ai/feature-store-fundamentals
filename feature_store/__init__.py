"""Minimal Feast-style feature store for OSS learning demos.

Not employer production software.
"""

from feature_store.schema import FeatureSchema, SchemaRegistry, SchemaMismatchError
from feature_store.store import FeatureStore, IncrementalMaterializeResult, OnlineFeaturesResult
from feature_store.freshness import Freshness, Watermark
from feature_store.pit import asof_join, asof_join_row
from feature_store.transforms import (
    MissingRequestDataError,
    Transform,
    TransformError,
    apply_transform,
    check_transform_parity,
)

__all__ = [
    "FeatureSchema",
    "SchemaRegistry",
    "SchemaMismatchError",
    "FeatureStore",
    "OnlineFeaturesResult",
    "IncrementalMaterializeResult",
    "Freshness",
    "Watermark",
    "asof_join",
    "asof_join_row",
    "Transform",
    "TransformError",
    "MissingRequestDataError",
    "apply_transform",
    "check_transform_parity",
]
__version__ = "0.1.0"
