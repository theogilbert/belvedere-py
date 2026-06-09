import hashlib
import json
import logging
import pathlib
from dataclasses import asdict
from typing import Any

from .protocol import ExploreItem

logger = logging.getLogger(__name__)

_SENSITIVE_PARAMS = frozenset({"password"})


def cache_file(params: dict[str, Any], cache_dir: pathlib.Path) -> pathlib.Path:
    safe = {k: v for k, v in sorted(params.items()) if k not in _SENSITIVE_PARAMS}
    digest = hashlib.sha256(json.dumps(safe).encode()).hexdigest()[:12]
    driver = params.get("driver", "unknown")
    return cache_dir / f"{driver}_{digest}.json"


def load_cache(path: pathlib.Path) -> dict[tuple, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
        result: dict[tuple, Any] = {}
        for str_key, value in data.items():
            if str_key.startswith("_"):
                continue
            key = tuple(json.loads(str_key))
            result[key] = [ExploreItem(**item) for item in value] if key[0] == "list" else value
        return result
    except Exception:
        logger.warning(f"Discarding unreadable explore cache at {path}")
        return {}


def save_cache(path: pathlib.Path, cache: dict[tuple, Any], params: dict[str, Any]) -> None:
    try:
        data: dict[str, Any] = {
            "_connection": {k: v for k, v in params.items() if k not in _SENSITIVE_PARAMS}
        }
        for key, value in cache.items():
            data[json.dumps(list(key))] = (
                [asdict(item) for item in value] if key[0] == "list" else value
            )
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(path)
    except Exception:
        logger.warning(f"Failed to persist explore cache to {path}")
