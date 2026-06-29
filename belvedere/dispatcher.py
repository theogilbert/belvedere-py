import asyncio
import logging
import pathlib
import time
from collections.abc import Awaitable, Callable
from typing import Any

from .drivers import get_driver, get_driver_help, list_drivers
from .drivers.base import BaseDriver, ConnectionLostError, DriverSettings
from .explore_cache import CachingDriver, ConnectionCache, cache_file
from .protocol import Method, ProgressCallback, WriteResult

logger = logging.getLogger(__name__)


class Connection:
    """Bundles a driver and concurrency semaphore for one open connection."""

    def __init__(
        self,
        id: str,
        driver: CachingDriver,
        max_concurrency: int,
        timeout: float,
    ) -> None:
        self.id = id
        self.driver = driver

        self._idle_timer: IdleTimer | None = None
        """Idle timeout watchdog; None when idle timeout is disabled (timeout=0)."""
        if timeout > 0:
            self._idle_timer = IdleTimer(timeout, self._on_idle_expire)
        self._semaphore = asyncio.Semaphore(max_concurrency)
        """Limits concurrent access to max_concurrency requests."""

    def reset_cache(self, path: list[str]) -> None:
        self.driver.reset_cache(path)

    async def close(self) -> None:
        if self._idle_timer:
            self._idle_timer.cancel()
        await self.driver.disconnect()

    async def __aenter__(self) -> "Connection":
        if self._idle_timer:
            self._idle_timer.cancel()
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()
        if self._idle_timer:
            self._idle_timer.reset()

    async def _on_idle_expire(self, timeout: float) -> None:
        logger.info(f"Connection {self.id} idle for {timeout}s — closing")
        await self.driver.disconnect()


class DispatchError(Exception):
    """Raised for client-visible errors: unknown method/driver, bad connection id, missing param."""


class ConnectionStore:
    """Manages connection lifecycle: open, close, idle timeout, and explore cache per connection.

    Args:
        cache_dir: Directory for persisting explore caches between sessions.
        max_concurrency: Maximum concurrent requests per connection.
    """

    def __init__(self, cache_dir: pathlib.Path, max_concurrency: int) -> None:
        self._connections: dict[str, Connection] = {}
        self._max_concurrency = max_concurrency
        self._cache_dir = cache_dir
        self._next_id = 0

    def open(
        self, driver: BaseDriver, params: dict[str, Any], timeout: float | None
    ) -> Connection:
        """Register a new connection with its cache and idle watchdog. Returns the connection."""
        conn_id = str(self._next_id)
        self._next_id += 1
        cache = ConnectionCache(params, cache_file(params, self._cache_dir))
        effective_timeout = (
            timeout if timeout is not None else driver.DEFAULT_IDLE_TIMEOUT
        )
        conn = Connection(
            conn_id,
            CachingDriver(driver, cache),
            self._max_concurrency,
            effective_timeout,
        )
        self._connections[conn_id] = conn
        return conn

    async def close(self, conn_id: str) -> None:
        """Disconnect and deregister a connection. No-op if not found."""
        conn = self._connections.pop(conn_id, None)
        if conn:
            await conn.close()

    def get(self, conn_id: str) -> Connection | None:
        return self._connections.get(conn_id)


class Dispatcher:
    """Routes method calls to handlers.

    Args:
        cache_dir: Directory for persisting explore caches.
        max_concurrency: Maximum concurrent requests allowed per connection.
        column_sample_size: Number of distinct non-null values sampled per column in describe results.
    """

    def __init__(
        self,
        cache_dir: pathlib.Path,
        max_concurrency: int = 1,
        driver_settings: DriverSettings
    ) -> None:
        self._store = ConnectionStore(cache_dir, max_concurrency)
        self._driver_settings = driver_settings
        self._conn_handlers = self._build_conn_handlers()

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
            case _ if method in self._conn_handlers:
                conn = self._require_conn(params)
                async with conn:
                    return await self._conn_handlers[method](
                        conn, params, send_progress
                    )
            case _:
                raise DispatchError(f"Unknown method: {method!r}")

    def _build_conn_handlers(
        self,
    ) -> dict[Method, Callable[..., Awaitable[dict[str, Any]]]]:
        return {
            Method.EXECUTE: self._handle_execute,
            Method.EXPLORE_LIST: self._handle_explore_list,
            Method.EXPLORE_DESCRIBE: self._handle_explore_describe,
            Method.EXPLORE_PREVIEW: self._handle_explore_preview,
        }

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
        driver = await driver_cls.create(params, self._driver_settings)

        raw_timeout = params.get("idle_timeout")
        timeout = float(raw_timeout) if raw_timeout is not None else None
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
        t0 = time.perf_counter()
        result = await self._reconnect_and_retry(
            conn, lambda: conn.driver.execute(query, binds), send_progress
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        if isinstance(result, WriteResult):
            return {"rows_affected": result.rows_affected, "duration_ms": duration_ms}
        return {
            "columns": result.columns,
            "rows": result.rows,
            "rows_total": result.rows_total,
            "duration_ms": duration_ms,
        }

    async def _handle_explore_list(
        self,
        conn: Connection,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            conn.reset_cache(path)
        items = await self._reconnect_and_retry(
            conn, lambda: conn.driver.explore_list(path), send_progress
        )
        return {"items": items}

    async def _handle_explore_describe(
        self,
        conn: Connection,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            conn.reset_cache(path)
        desc = await self._reconnect_and_retry(
            conn, lambda: conn.driver.explore_describe(path), send_progress
        )
        return {"details": desc}

    async def _handle_explore_preview(
        self,
        conn: Connection,
        params: dict[str, Any],
        send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        t0 = time.perf_counter()
        result = await self._reconnect_and_retry(
            conn, lambda: conn.driver.explore_preview(path), send_progress
        )
        duration_ms = round((time.perf_counter() - t0) * 1000, 3)
        if result is None:
            return {
                "columns": None,
                "rows": None,
                "rows_total": None,
                "duration_ms": duration_ms,
            }
        return {
            "columns": result.columns,
            "rows": result.rows,
            "rows_total": result.rows_total,
            "duration_ms": duration_ms,
        }

    async def _reconnect_and_retry(
        self,
        conn: Connection,
        coro_fn: Callable[[], Awaitable[Any]],
        send_progress: ProgressCallback,
    ) -> Any:
        try:
            return await coro_fn()
        except ConnectionLostError:
            await send_progress("reconnecting", "Connection lost — reconnecting…")
            await conn.driver.reconnect()
            return await coro_fn()

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
    """Manages a connection's idle timeout watchdogs.

    Args:
        timeout: Number of seconds after which a connection should be closed.
        on_expire: The operation to execute once the connection has been idle too long.
    """

    def __init__(
        self, timeout: float, on_expire: Callable[[float], Awaitable[None]]
    ) -> None:
        self._on_expire = on_expire
        """Callback invoked when the idle watchdog fires."""
        self._timeout: float = timeout
        """Registered timeout duration in seconds."""
        self._task: asyncio.Task = asyncio.create_task(self._watchdog())
        """Active watchdog task."""

    def reset(self) -> None:
        """Restart the idle timer."""
        self._task.cancel()
        self._task = asyncio.create_task(self._watchdog())

    def cancel(self) -> None:
        """Cancel the idle timer."""
        self._task.cancel()

    async def _watchdog(self) -> None:
        await asyncio.sleep(self._timeout)
        await self._on_expire(self._timeout)
