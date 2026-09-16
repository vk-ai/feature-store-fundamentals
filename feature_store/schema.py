"""Feature view schema + version registry (JSON on disk)."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any


class SchemaMismatchError(ValueError):
    """Raised when requested schema version does not match registered schema."""


@dataclass(frozen=True)
class FeatureSchema:
    """Describes one versioned feature view."""

    name: str
    version: str
    entity_key: str
    features: tuple[str, ...]
    dtypes: dict[str, str] = field(default_factory=dict)
    description: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> FeatureSchema:
        return cls(
            name=data["name"],
            version=str(data["version"]),
            entity_key=data["entity_key"],
            features=tuple(data["features"]),
            dtypes=dict(data.get("dtypes") or {}),
            description=str(data.get("description") or ""),
        )

    def save(self, path: Path | str) -> Path:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return path

    @classmethod
    def load(cls, path: Path | str) -> FeatureSchema:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls.from_dict(data)


class SchemaRegistry:
    """Load / register feature schemas from a directory of JSON files."""

    def __init__(self, root: Path | str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._cache: dict[tuple[str, str], FeatureSchema] = {}
        self.reload()

    def reload(self) -> None:
        self._cache.clear()
        for path in sorted(self.root.glob("*.json")):
            schema = FeatureSchema.load(path)
            self._cache[(schema.name, schema.version)] = schema

    def register(self, schema: FeatureSchema, overwrite: bool = False) -> Path:
        key = (schema.name, schema.version)
        if key in self._cache and not overwrite:
            raise ValueError(f"Schema already registered: {schema.name}@{schema.version}")
        path = self.root / f"{schema.name}_v{schema.version}.json"
        schema.save(path)
        self._cache[key] = schema
        return path

    def get(self, name: str, version: str) -> FeatureSchema:
        key = (name, str(version))
        if key not in self._cache:
            raise SchemaMismatchError(
                f"No schema registered for {name}@{version}. "
                f"Known: {sorted(f'{n}@{v}' for n, v in self._cache)}"
            )
        return self._cache[key]

    def require(self, name: str, version: str, expected: FeatureSchema | None = None) -> FeatureSchema:
        """Fetch schema and optionally assert it matches an expected definition."""
        schema = self.get(name, version)
        if expected is not None:
            if schema.name != expected.name or schema.version != expected.version:
                raise SchemaMismatchError(
                    f"Version mismatch: requested {expected.name}@{expected.version}, "
                    f"registry has {schema.name}@{schema.version}"
                )
            if schema.features != expected.features or schema.entity_key != expected.entity_key:
                raise SchemaMismatchError(
                    f"Schema shape mismatch for {name}@{version}: "
                    f"registry features={list(schema.features)} entity={schema.entity_key}; "
                    f"expected features={list(expected.features)} entity={expected.entity_key}"
                )
        return schema

    def list_versions(self, name: str | None = None) -> list[FeatureSchema]:
        items = list(self._cache.values())
        if name is not None:
            items = [s for s in items if s.name == name]
        return sorted(items, key=lambda s: (s.name, s.version))
