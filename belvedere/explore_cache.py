import hashlib
import json
import logging
import pathlib
from dataclasses import asdict
from typing import Any, Self

from belvedere.drivers import SENSITIVE_PARAM_KEYS

from .drivers.base import BaseDriver, ReadResult, WriteResult
from .protocol import (
    ColumnInfo,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    TableDescription,
)

logger = logging.getLogger(__name__)


class CachingDriver(BaseDriver):
    """BaseDriver decorator that intercepts explore calls for caching."""

    def __init__(self, inner: BaseDriver, cache: "ConnectionCache") -> None:
        super().__init__({})
        self._inner = inner
        self._cache = cache

    @classmethod
    async def create(cls, params: dict[str, Any]) -> Self:
        raise NotImplementedError

    async def reconnect(self) -> None:
        await self._inner.reconnect()

    async def disconnect(self) -> None:
        await self._inner.disconnect()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        return await self._inner.execute(query, binds)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        items = self._cache.get_list(path)
        if items is None:
            items = await self._inner.explore_list(path)
            self._cache.set_list(path, items)
        else:
            logger.debug(f"explore.list cache hit for path {path}")
        return items

    async def explore_preview(self, path: list[str]) -> ReadResult | None:
        return await self._inner.explore_preview(path)

    async def explore_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        if self._cache.has_describe(path):
            logger.debug(f"explore.describe cache hit for path {path}")
            return self._cache.get_describe(path)
        desc = await self._inner.explore_describe(path)
        if desc is not None:
            self._cache.set_describe(path, desc)
        return desc

    def reset_cache(self, path: list[str]) -> None:
        self._cache.reset(path)


def cache_file(params: dict[str, Any], cache_dir: pathlib.Path) -> pathlib.Path:
    """Return the cache file path derived from the connection params.

    Args:
        params: Raw connection params; sensitive fields are excluded from the hash.
        cache_dir: Directory where cache files are stored.

    Returns:
        Path of the form ``<cache_dir>/<driver>_<sha256[:12]>.json``.
    """
    safe = {k: v for k, v in sorted(params.items()) if k not in SENSITIVE_PARAM_KEYS}
    digest = hashlib.sha256(json.dumps(safe).encode()).hexdigest()[:12]
    driver = params.get("driver", "unknown")
    return cache_dir / f"{driver}_{digest}.json"


class ConnectionCache:
    """Explore result cache for a single database connection, backed by a JSON file.

    Loads from disk on construction and persists atomically after every write.
    Callers interact only through ``get_*`` / ``set_*`` / ``reset``.

    Args:
        params: Raw connection params used to populate the ``_connection``
            metadata block written to disk.
        path: Path to the JSON cache file.
    """

    def __init__(self, params: dict[str, Any], path: pathlib.Path) -> None:
        self._params = params
        """Raw connection params; sensitive fields are stripped before writing to disk."""
        self._path = path
        """Path to the backing JSON cache file."""
        self._list: dict[tuple[str, ...], list[ExploreItem]] = {}
        """In-memory cache mapping path tuples to their explore.list results."""
        self._describe: dict[tuple[str, ...], TableDescription | IndexDescription] = {}
        """In-memory cache mapping path tuples to their explore.describe results."""
        self._load()

    def reset(self, path: list[str]) -> None:
        """Clear cached results at or below path, then persist or delete the disk file.

        Args:
            path: Path prefix to reset. Entries whose keys start with this prefix
                (including the prefix itself) are removed, along with any ancestor
                list entries (since their children may have changed). An empty list
                resets the entire cache.
        """
        prefix = tuple(path)
        n = len(prefix)
        for d in (self._list, self._describe):
            for k in [k for k in d if k[:n] == prefix]:
                del d[k]
        # Evict list entries for all ancestor paths — their children may have changed.
        for i in range(n):
            self._list.pop(prefix[:i], None)
        if self._list or self._describe:
            self._persist()
        else:
            self._path.unlink(missing_ok=True)

    def get_list(self, path: list[str]) -> list[ExploreItem] | None:
        """Return cached explore.list results for path, or None on a miss.

        Args:
            path: Path segments identifying the node.

        Returns:
            Cached items, or None if the path has not been fetched yet.
        """
        return self._list.get(tuple(path))

    def set_list(self, path: list[str], items: list[ExploreItem]) -> None:
        """Store explore.list results for path and persist to disk.

        Args:
            path: Path segments identifying the node.
            items: Items to cache.
        """
        self._list[tuple(path)] = items
        self._persist()

    def has_describe(self, path: list[str]) -> bool:
        """Return True if a non-None explore.describe result for path is cached.

        Args:
            path: Path segments identifying the node.
        """
        return tuple(path) in self._describe

    def get_describe(
        self, path: list[str]
    ) -> TableDescription | IndexDescription | None:
        """Return cached explore.describe results for path, or None on a miss."""
        return self._describe.get(tuple(path))

    def set_describe(
        self, path: list[str], desc: TableDescription | IndexDescription
    ) -> None:
        """Store explore.describe results for path and persist to disk.

        Args:
            path: Path segments identifying the node.
            desc: TableDescription to cache, or None if the path is not a table.
        """
        self._describe[tuple(path)] = desc
        self._persist()

    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            data = json.loads(self._path.read_text())
            for str_path, items in data.get("list", {}).items():
                key = tuple(json.loads(str_path))
                self._list[key] = [ExploreItem(**item) for item in items]
            for str_path, desc in data.get("describe", {}).items():
                key = tuple(json.loads(str_path))
                if desc is None:
                    pass  # legacy: None was cached before; skip it
                elif desc.get("type") == "index":
                    self._describe[key] = IndexDescription(
                        index=desc["index"],
                        fields=[IndexKeyField(**f) for f in desc.get("fields", [])],
                        unique=desc.get("unique", False),
                        entity=desc.get("entity"),
                        condition=desc.get("condition"),
                    )
                else:
                    self._describe[key] = TableDescription(
                        table=desc["table"],
                        schema=desc.get("schema"),
                        columns=[ColumnInfo(**col) for col in desc.get("columns", [])],
                    )
        except Exception:
            logger.warning(f"Discarding unreadable explore cache at {self._path}")
            self._list.clear()
            self._describe.clear()

    def _persist(self) -> None:
        try:
            data: dict[str, Any] = {
                "_connection": {
                    k: v
                    for k, v in self._params.items()
                    if k not in SENSITIVE_PARAM_KEYS
                },
                "list": {
                    json.dumps(list(key)): [asdict(item) for item in items]
                    for key, items in self._list.items()
                },
                "describe": {
                    json.dumps(list(key)): asdict(desc) if desc is not None else None
                    for key, desc in self._describe.items()
                },
            }
            tmp = self._path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, indent=2))
            tmp.replace(self._path)
        except Exception:
            logger.warning(f"Failed to persist explore cache to {self._path}")
