import asyncio
import logging
import pathlib
from typing import Any

from .drivers import get_driver, list_drivers
from .drivers.base import BaseDriver, ConnectionLostError
from .explore_cache import ConnectionCache, cache_file
from .protocol import DMLResult, ProgressCallback

logger = logging.getLogger(__name__)

_DEFAULT_IDLE_TIMEOUT = 600.0


class Dispatcher:
    """Routes method calls to handlers and manages per-connection state.

    Args:
        cache_dir: Directory for persisting explore caches.
        max_concurrency: Maximum concurrent requests allowed per connection.
    """

    def __init__(self, cache_dir: pathlib.Path, max_concurrency: int = 1) -> None:
        self._connections: dict[str, BaseDriver] = {}
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._caches: dict[str, ConnectionCache] = {}
        self._idle_timeouts: dict[str, float] = {}
        self._idle_tasks: dict[str, asyncio.Task] = {}
        self._cache_dir = cache_dir
        self._max_concurrency = max_concurrency
        self._next_id: int = 0

    async def dispatch(
        self, method: str, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        """Dispatch a method call to its handler, serialized per connection.

        Args:
            method: Method name (e.g. ``"execute"``, ``"explore.list"``).
            params: Method parameters; most include a ``connection_id``.
            send_progress: Callback for emitting progress notifications.

        Returns:
            Method-specific result dict.

        Raises:
            ValueError: If the method name is unknown.
            KeyError: If the ``connection_id`` does not refer to an open connection.
        """
        handler_name = "_handle_" + method.replace(".", "_")
        handler = getattr(self, handler_name, None)
        if handler is None:
            raise ValueError(f"Unknown method: {method!r}")

        conn_id = params.get("connection_id")
        semaphore = self._semaphores.get(conn_id) if conn_id else None

        if conn_id:
            self._reset_idle_timer(conn_id)

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

    async def _handle_capabilities(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"server": "dbelveder", "drivers": list_drivers()}

    async def _handle_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        driver = await get_driver(params["driver"]).create(params)
        conn_id = str(self._next_id)
        self._next_id += 1
        self._connections[conn_id] = driver
        self._semaphores[conn_id] = asyncio.Semaphore(self._max_concurrency)
        self._caches[conn_id] = ConnectionCache(
            params, cache_file(params, self._cache_dir)
        )
        timeout = float(params.get("idle_timeout", _DEFAULT_IDLE_TIMEOUT))
        self._idle_timeouts[conn_id] = timeout
        self._idle_tasks[conn_id] = asyncio.create_task(
            self._idle_watchdog(conn_id, timeout)
        )
        return {"connection_id": conn_id}

    async def _handle_disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = self._require_param(params, "connection_id")
        driver = self._connections.pop(conn_id, None)
        self._semaphores.pop(conn_id, None)
        self._caches.pop(conn_id, None)
        self._idle_timeouts.pop(conn_id, None)
        task = self._idle_tasks.pop(conn_id, None)
        if task:
            task.cancel()
        if driver:
            await driver.disconnect()
        return {"ok": True}

    async def _handle_execute(
        self, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        driver = self._require_connection(self._require_param(params, "connection_id"))
        sql: str = self._require_param(params, "sql")
        binds: list[Any] = params.get("params") or []
        try:
            result = await driver.execute(sql, binds)
        except ConnectionLostError:
            await send_progress("reconnecting", "Connection lost — reconnecting…")
            await driver.reconnect()
            await send_progress("executing", "Retrying query…")
            result = await driver.execute(sql, binds)
        if isinstance(result, DMLResult):
            return {"rows_affected": result.rows_affected}
        return {"columns": result.columns, "rows": result.rows}

    async def _handle_explore_list(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = self._require_param(params, "connection_id")
        path: list[str] = params.get("path") or []
        cache = self._caches[conn_id]
        if params.get("reset_cache"):
            cache.reset()
        items = cache.get_list(path)
        if items is None:
            items = await self._require_connection(conn_id).explore_list(path)
            cache.set_list(path, items)
        else:
            logger.debug(
                f"explore.list cache hit for connection {conn_id!r}, path {path}"
            )
        return {"items": items}

    async def _handle_explore_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = self._require_param(params, "connection_id")
        path: list[str] = params.get("path") or []
        cache = self._caches[conn_id]
        if params.get("reset_cache"):
            cache.reset()
        if cache.has_describe(path):
            logger.debug(
                f"explore.describe cache hit for connection {conn_id!r}, path {path}"
            )
            return {"details": cache.get_describe(path)}
        desc = await self._require_connection(conn_id).explore_describe(path)
        cache.set_describe(path, desc)
        return {"details": desc}

    def _require_param(self, params: dict[str, Any], key: str) -> Any:
        if key not in params:
            raise ValueError(f"Missing required param: {key!r}")
        return params[key]

    def _require_connection(self, conn_id: str) -> BaseDriver:
        driver = self._connections.get(conn_id)
        if driver is None:
            raise KeyError(f"Unknown connection_id: {conn_id!r}")
        return driver

    def _reset_idle_timer(self, conn_id: str) -> None:
        task = self._idle_tasks.pop(conn_id, None)
        if task:
            task.cancel()
        timeout = self._idle_timeouts.get(conn_id)
        if timeout is not None:
            self._idle_tasks[conn_id] = asyncio.create_task(
                self._idle_watchdog(conn_id, timeout)
            )

    async def _idle_watchdog(self, conn_id: str, timeout: float) -> None:
        await asyncio.sleep(timeout)
        logger.info(f"Connection {conn_id!r} idle for {timeout}s — closing")
        driver = self._connections.pop(conn_id, None)
        self._semaphores.pop(conn_id, None)
        self._caches.pop(conn_id, None)
        self._idle_timeouts.pop(conn_id, None)
        self._idle_tasks.pop(conn_id, None)
        if driver:
            await driver.disconnect()
