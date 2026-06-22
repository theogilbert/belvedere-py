import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

from .drivers import get_driver, get_driver_help, list_drivers
from .drivers.base import BaseDriver, ConnectionLostError
from .explore_cache import CachingDriver, ConnectionCache, cache_file
from .protocol import Method, ProgressCallback, WriteResult

logger = logging.getLogger(__name__)


class Connection:
    """Bundles a driver and concurrency semaphore for one open connection."""

    def __init__(self, id: str, driver: CachingDriver, max_concurrency: int) -> None:
        self.id = id
        self.driver = driver
        self._semaphore = asyncio.Semaphore(max_concurrency)

    def reset_cache(self) -> None:
        self.driver.reset_cache()

    async def __aenter__(self) -> "Connection":
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()


class DispatchError(Exception):
    """Raised for client-visible errors: unknown method/driver, bad connection id, missing param."""


_DEFAULT_IDLE_TIMEOUT = 600.0


class ConnectionStore:
    """Manages connection lifecycle: open, close, idle timeout, and explore cache per connection.

    Args:
        cache_dir: Directory for persisting explore caches between sessions.
        max_concurrency: Maximum concurrent requests per connection.
    """

    def __init__(self, cache_dir: pathlib.Path, max_concurrency: int) -> None:
        self._connections: dict[str, Connection] = {}
        self._idle_timer = IdleTimer(self._on_idle_expire)
        self._max_concurrency = max_concurrency
        self._cache_dir = cache_dir
        self._next_id = 0

    def open(
        self, driver: BaseDriver, params: dict[str, Any], timeout: float
    ) -> Connection:
        """Register a new connection with its cache and idle watchdog. Returns the connection."""
        conn_id = str(self._next_id)
        self._next_id += 1
        cache = ConnectionCache(params, cache_file(params, self._cache_dir))
        conn = Connection(conn_id, CachingDriver(driver, cache), self._max_concurrency)
        self._connections[conn_id] = conn
        self._idle_timer.start(conn_id, timeout)
        return conn

    async def close(self, conn_id: str) -> None:
        """Disconnect and deregister a connection. No-op if not found."""
        conn = self._connections.pop(conn_id, None)
        self._idle_timer.cancel(conn_id)
        if conn:
            await conn.driver.disconnect()

    def get(self, conn_id: str) -> Connection | None:
        return self._connections.get(conn_id)

    def reset_idle(self, conn_id: str) -> None:
        self._idle_timer.reset(conn_id)

    async def _on_idle_expire(self, conn_id: str, timeout: float) -> None:
        logger.info(f"Connection {conn_id!r} idle for {timeout}s — closing")
        conn = self._connections.pop(conn_id, None)
        if conn:
            await conn.driver.disconnect()


class Dispatcher:
    """Routes method calls to handlers.

    Args:
        cache_dir: Directory for persisting explore caches.
        max_concurrency: Maximum concurrent requests allowed per connection.
    """

    def __init__(self, cache_dir: pathlib.Path, max_concurrency: int = 1) -> None:
        self._store = ConnectionStore(cache_dir, max_concurrency)

    async def dispatch(
        self, method: Method, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        """Dispatch a method call to its handler, serialized per connection.

        Args:
            method: Method name.
            params: Method parameters; most include a ``connection_id``.
            send_progress: Callback for emitting progress notifications.

        Returns:
            Method-specific result dict.

        Raises:
            DispatchError: If the method is unknown, the ``connection_id`` does not
                refer to an open connection, or a required param is missing.
        """
        match method:
            case Method.CAPABILITIES:
                return await self._handle_capabilities(params, send_progress)
            case Method.DRIVER_HELP:
                return await self._handle_driver_help(params, send_progress)
            case Method.CONNECT:
                return await self._handle_connect(params, send_progress)
            case Method.DISCONNECT:
                return await self._handle_disconnect(params, send_progress)
            case Method.EXECUTE | Method.EXPLORE_LIST | Method.EXPLORE_DESCRIBE:
                conn = self._require_conn(params)
                self._store.reset_idle(conn.id)
                async with conn:
                    return await self._dispatch_conn(
                        conn, method, params, send_progress
                    )
            case _:
                raise DispatchError(f"Unknown method: {method!r}")

    async def _dispatch_conn(
        self,
        conn: Connection,
        method: Method,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        match method:
            case Method.EXECUTE:
                return await self._handle_execute(conn, params, send_progress)
            case Method.EXPLORE_LIST:
                return await self._handle_explore_list(conn, params, send_progress)
            case Method.EXPLORE_DESCRIBE:
                return await self._handle_explore_describe(conn, params, send_progress)
            case _:
                raise AssertionError(f"unreachable: {method!r}")

    async def _handle_capabilities(
        self,
        _params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        return {"server": "belvedere", "drivers": list_drivers()}

    async def _handle_driver_help(
        self,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        driver_name = self._require_param(params, "driver")
        try:
            return {"content": get_driver_help(driver_name)}
        except ValueError as exc:
            raise DispatchError(str(exc)) from exc

    async def _handle_connect(
        self,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        driver_name = self._require_param(params, "driver")
        try:
            driver_cls = get_driver(driver_name)
        except ValueError as exc:
            raise DispatchError(str(exc)) from exc
        missing = [
            p.label for p in driver_cls.PARAMS if p.required and not params.get(p.key)
        ]
        if missing:
            raise DispatchError(f"Missing required parameter(s): {', '.join(missing)}")
        driver = await driver_cls.create(params)
        timeout = float(params.get("idle_timeout", _DEFAULT_IDLE_TIMEOUT))
        conn = self._store.open(driver, params, timeout)
        return {"connection_id": conn.id}

    async def _handle_disconnect(
        self,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        conn_id = params.get("connection_id") or ""
        await self._store.close(conn_id)
        return {"ok": True}

    async def _handle_execute(
        self,
        conn: Connection,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        query: str = self._require_param(params, "query")
        binds: list[Any] = params.get("params") or []
        try:
            result = await conn.driver.execute(query, binds)
        except ConnectionLostError:
            await send_progress("reconnecting", "Connection lost — reconnecting…")
            await conn.driver.reconnect()
            await send_progress("executing", "Retrying query…")
            result = await conn.driver.execute(query, binds)
        if isinstance(result, WriteResult):
            return {"rows_affected": result.rows_affected}
        return {
            "columns": result.columns,
            "rows": result.rows,
            "rows_total": result.rows_total,
        }

    async def _handle_explore_list(
        self,
        conn: Connection,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            conn.reset_cache()
        items = await conn.driver.explore_list(path)
        return {"items": items}

    async def _handle_explore_describe(
        self,
        conn: Connection,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            conn.reset_cache()
        desc = await conn.driver.explore_describe(path)
        return {"details": desc}

    def _require_conn(self, params: dict[str, Any]) -> Connection:
        conn_id = params.get("connection_id") or ""
        conn = self._store.get(conn_id)
        if conn is None:
            raise DispatchError(f"Unknown connection_id: {conn_id!r}")
        return conn

    def _require_param(self, params: dict[str, Any], key: str) -> Any:
        if key not in params:
            raise DispatchError(f"Missing required param: {key!r}")
        return params[key]


class IdleTimer:
    """Manages per-connection idle timeout watchdogs."""

    def __init__(self, on_expire: Callable[[str, float], Awaitable[None]]) -> None:
        self._on_expire = on_expire
        """Callback invoked when a connection's idle watchdog fires."""
        self._timeouts: dict[str, float] = {}
        """Registered timeout durations keyed by connection id."""
        self._tasks: dict[str, asyncio.Task] = {}
        """Active watchdog tasks keyed by connection id."""

    def start(self, conn_id: str, timeout: float) -> None:
        """Register conn_id and start its idle watchdog."""
        self._timeouts[conn_id] = timeout
        self._tasks[conn_id] = asyncio.create_task(self._watchdog(conn_id, timeout))

    def reset(self, conn_id: str) -> None:
        """Restart the idle timer for conn_id, if registered."""
        task = self._tasks.pop(conn_id, None)
        if task:
            task.cancel()
        timeout = self._timeouts.get(conn_id)
        if timeout is not None:
            self._tasks[conn_id] = asyncio.create_task(self._watchdog(conn_id, timeout))

    def cancel(self, conn_id: str) -> None:
        """Cancel and remove the idle timer for conn_id."""
        task = self._tasks.pop(conn_id, None)
        if task:
            task.cancel()
        self._timeouts.pop(conn_id, None)

    async def _watchdog(self, conn_id: str, timeout: float) -> None:
        await asyncio.sleep(timeout)
        self._tasks.pop(conn_id, None)
        self._timeouts.pop(conn_id, None)
        await self._on_expire(conn_id, timeout)
