"""Minimal Feast-style feature store for OSS learning demos.

Not employer production software.
"""

from feature_store.schema import FeatureSchema, SchemaRegistry, SchemaMismatchError
from feature_store.store import FeatureStore

__all__ = [
    "FeatureSchema",
    "SchemaRegistry",
    "SchemaMismatchError",
    "FeatureStore",
]
__version__ = "0.1.0"
