import asyncio
import logging
import pathlib
from typing import Any

from .drivers import get_driver
from .drivers.base import BaseDriver, ConnectionLostError
from .explore_cache import cache_file, load_cache, save_cache
from .protocol import ProgressCallback

logger = logging.getLogger(__name__)


class Dispatcher:
    def __init__(self, max_concurrency: int = 1, cache_dir: pathlib.Path | None = None) -> None:
        self._connections: dict[str, BaseDriver] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._explore_cache: dict[str, dict[tuple, Any]] = {}
        self._cache_dir = cache_dir
        self._cache_files: dict[str, pathlib.Path] = {}
        self._conn_params: dict[str, dict[str, Any]] = {}
        self._max_concurrency = max_concurrency
        self._next_id: int = 0

    async def dispatch(
        self, method: str, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        handler_name = "_handle_" + method.replace(".", "_")
        handler = getattr(self, handler_name, None)
        if handler is None:
            raise ValueError(f"Unknown method: {method!r}")

        conn_id = params.get("connection_id")
        semaphore = self._semaphores.get(conn_id) if conn_id else None

        if semaphore:
            async with semaphore:
                return await self._call(method, handler, params, send_progress)
        return await self._call(method, handler, params, send_progress)

    async def _call(
        self,
        method: str,
        handler: Any,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        if method == "execute":
            return await handler(params, send_progress)
        return await handler(params)

    # ── connection ──────────────────────────────────────────────────────────

    async def _handle_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        driver = get_driver(params["driver"])(params)
        await driver.connect()
        conn_id = str(self._next_id)
        self._next_id += 1
        self._connections[conn_id] = driver
        self._semaphores[conn_id] = asyncio.Semaphore(self._max_concurrency)
        if self._cache_dir:
            path = cache_file(params, self._cache_dir)
            self._cache_files[conn_id] = path
            self._conn_params[conn_id] = params
            self._explore_cache[conn_id] = load_cache(path)
        else:
            self._explore_cache[conn_id] = {}
        return {"connection_id": conn_id}

    async def _handle_disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = params["connection_id"]
        driver = self._connections.pop(conn_id, None)
        self._semaphores.pop(conn_id, None)
        self._explore_cache.pop(conn_id, None)
        self._cache_files.pop(conn_id, None)
        self._conn_params.pop(conn_id, None)
        if driver:
            await driver.disconnect()
        return {"ok": True}

    # ── query ────────────────────────────────────────────────────────────────

    async def _handle_execute(
        self, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        driver = self._require_connection(params["connection_id"])
        sql: str = params["sql"]
        binds: list[Any] = params.get("params") or []
        try:
            columns, rows = await driver.execute(sql, binds)
        except ConnectionLostError:
            await send_progress("reconnecting", "Connection lost — reconnecting…")
            await driver.connect()
            await send_progress("executing", "Retrying query…")
            columns, rows = await driver.execute(sql, binds)
        return {"columns": columns, "rows": rows}

    # ── exploration ──────────────────────────────────────────────────────────

    async def _handle_explore_list(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = params["connection_id"]
        path: list[str] = params.get("path") or []
        cache = self._explore_cache[conn_id]
        if params.get("reset_cache"):
            cache.clear()
            if conn_id in self._cache_files:
                self._cache_files[conn_id].unlink(missing_ok=True)
        key = ("list", *path)
        if key not in cache:
            cache[key] = await self._require_connection(conn_id).explore_list(path)
            if conn_id in self._cache_files:
                save_cache(self._cache_files[conn_id], cache, self._conn_params[conn_id])
        else:
            logger.debug(f"explore.list cache hit for connection {conn_id!r}, path {path}")
        return {"items": cache[key]}

    async def _handle_explore_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = params["connection_id"]
        path: list[str] = params.get("path") or []
        cache = self._explore_cache[conn_id]
        if params.get("reset_cache"):
            cache.clear()
            if conn_id in self._cache_files:
                self._cache_files[conn_id].unlink(missing_ok=True)
        key = ("describe", *path)
        if key not in cache:
            cache[key] = await self._require_connection(conn_id).explore_describe(path)
            if conn_id in self._cache_files:
                save_cache(self._cache_files[conn_id], cache, self._conn_params[conn_id])
        else:
            logger.debug(f"explore.describe cache hit for connection {conn_id!r}, path {path}")
        return {"details": cache[key]}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _require_connection(self, conn_id: str) -> BaseDriver:
        driver = self._connections.get(conn_id)
        if driver is None:
            raise KeyError(f"Unknown connection_id: {conn_id!r}")
        return driver
