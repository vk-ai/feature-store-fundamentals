"""Minimal Feast-style feature store for OSS learning demos.

Not employer production software.
"""

from feature_store.schema import FeatureSchema, SchemaRegistry, SchemaMismatchError
from feature_store.store import FeatureStore, OnlineFeaturesResult

__all__ = [
    "FeatureSchema",
    "SchemaRegistry",
    "SchemaMismatchError",
    "FeatureStore",
    "OnlineFeaturesResult",
]
__version__ = "0.1.0"
