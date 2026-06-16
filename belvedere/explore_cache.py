import hashlib
import json
import logging
import pathlib
from dataclasses import asdict
from typing import Any

from .protocol import (
    ColumnInfo,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    TableDescription,
)

logger = logging.getLogger(__name__)

_SENSITIVE_PARAMS = frozenset({"password"})


def cache_file(params: dict[str, Any], cache_dir: pathlib.Path) -> pathlib.Path:
    """Return the cache file path derived from the connection params.

    Args:
        params: Raw connection params; sensitive fields are excluded from the hash.
        cache_dir: Directory where cache files are stored.

    Returns:
        Path of the form ``<cache_dir>/<driver>_<sha256[:12]>.json``.
    """
    safe = {k: v for k, v in sorted(params.items()) if k not in _SENSITIVE_PARAMS}
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

    def reset(self) -> None:
        """Clear all cached results and delete the disk file."""
        self._list.clear()
        self._describe.clear()
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
                    k: v for k, v in self._params.items() if k not in _SENSITIVE_PARAMS
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
