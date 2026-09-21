"""Minimal Feast-style feature store for OSS learning demos.

Not employer production software.
"""

from feature_store.schema import FeatureSchema, SchemaRegistry, SchemaMismatchError
from feature_store.store import FeatureStore, OnlineFeaturesResult
from feature_store.pit import asof_join, asof_join_row

__all__ = [
    "FeatureSchema",
    "SchemaRegistry",
    "SchemaMismatchError",
    "FeatureStore",
    "OnlineFeaturesResult",
    "asof_join",
    "asof_join_row",
]
__version__ = "0.1.0"
