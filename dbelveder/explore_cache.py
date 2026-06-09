import hashlib
import json
import logging
import pathlib
from dataclasses import asdict, dataclass, field
from typing import Any

from .protocol import ColumnInfo, ExploreItem, TableDescription

logger = logging.getLogger(__name__)

_SENSITIVE_PARAMS = frozenset({"password"})


@dataclass
class ExploreCache:
    list_results: dict[tuple[str, ...], list[ExploreItem]] = field(default_factory=dict)
    describe_results: dict[tuple[str, ...], TableDescription | None] = field(default_factory=dict)

    def clear(self) -> None:
        self.list_results.clear()
        self.describe_results.clear()


def cache_file(params: dict[str, Any], cache_dir: pathlib.Path) -> pathlib.Path:
    safe = {k: v for k, v in sorted(params.items()) if k not in _SENSITIVE_PARAMS}
    digest = hashlib.sha256(json.dumps(safe).encode()).hexdigest()[:12]
    driver = params.get("driver", "unknown")
    return cache_dir / f"{driver}_{digest}.json"


def load_cache(path: pathlib.Path) -> ExploreCache:
    if not path.exists():
        return ExploreCache()
    try:
        data = json.loads(path.read_text())
        cache = ExploreCache()
        for str_path, items in data.get("list", {}).items():
            key = tuple(json.loads(str_path))
            cache.list_results[key] = [ExploreItem(**item) for item in items]
        for str_path, desc in data.get("describe", {}).items():
            key = tuple(json.loads(str_path))
            cache.describe_results[key] = (
                TableDescription(
                    table=desc["table"],
                    schema=desc.get("schema"),
                    columns=[ColumnInfo(**col) for col in desc.get("columns", [])],
                )
                if desc is not None
                else None
            )
        return cache
    except Exception:
        logger.warning(f"Discarding unreadable explore cache at {path}")
        return ExploreCache()


def save_cache(path: pathlib.Path, cache: ExploreCache, params: dict[str, Any]) -> None:
    try:
        data: dict[str, Any] = {
            "_connection": {k: v for k, v in params.items() if k not in _SENSITIVE_PARAMS},
            "list": {
                json.dumps(list(key)): [asdict(item) for item in items]
                for key, items in cache.list_results.items()
            },
            "describe": {
                json.dumps(list(key)): asdict(desc) if desc is not None else None
                for key, desc in cache.describe_results.items()
            },
        }
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except Exception:
        logger.warning(f"Failed to persist explore cache to {path}")
