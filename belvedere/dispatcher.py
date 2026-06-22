import asyncio
import logging
import pathlib
from collections.abc import Awaitable, Callable
from typing import Any

from .drivers import get_driver, get_driver_help, list_drivers
from .drivers.base import BaseDriver, ConnectionLostError
from .explore_cache import ConnectionCache, cache_file
from .protocol import WriteResult, Method, ProgressCallback

logger = logging.getLogger(__name__)


class Connection:
    """Bundles a driver with its connection id and concurrency semaphore."""

    def __init__(self, id: str, driver: BaseDriver, max_concurrency: int) -> None:
        self.id = id
        """The connection id assigned by the dispatcher."""
        self.driver = driver
        """The underlying database driver."""
        self._semaphore = asyncio.Semaphore(max_concurrency)
        """Limits the number of concurrent requests on this connection."""

    async def __aenter__(self) -> "Connection":
        await self._semaphore.acquire()
        return self

    async def __aexit__(self, *_: object) -> None:
        self._semaphore.release()


class DispatchError(Exception):
    """Raised for client-visible errors: unknown method/driver, bad connection id, missing param."""


_Handler = Callable[
    [Connection | None, ConnectionCache | None, dict[str, Any], ProgressCallback],
    Awaitable[dict[str, Any]],
]
"""Function which handles a request to produce the content of a response."""

_DEFAULT_IDLE_TIMEOUT = 600.0
_CONNECTION_REQUIRED = frozenset(
    {Method.EXECUTE, Method.EXPLORE_LIST, Method.EXPLORE_DESCRIBE}
)


class Dispatcher:
    """Routes method calls to handlers and manages per-connection state.

    Args:
        cache_dir: Directory for persisting explore caches.
        max_concurrency: Maximum concurrent requests allowed per connection.
    """

    def __init__(self, cache_dir: pathlib.Path, max_concurrency: int = 1) -> None:
        self._connections: dict[str, Connection] = {}
        """Active connections keyed by connection_id."""
        self._caches = CacheStore(cache_dir)
        """Explore caches for all connections."""
        self._idle_timer = IdleTimer(self._on_idle_expire)
        """Manages idle timeout watchdogs for all connections."""
        self._max_concurrency = max_concurrency
        """Maximum concurrent requests allowed per connection."""
        self._next_id: int = 0
        """Monotonic counter used to generate unique connection IDs."""

    async def dispatch(
        self, method: Method, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        """Dispatch a method call to its handler, serialized per connection.

        Args:
            method: Method name (e.g. ``"execute"``, ``"explore.list"``).
            params: Method parameters; most include a ``connection_id``.
            send_progress: Callback for emitting progress notifications.

        Returns:
            Method-specific result dict.

        Raises:
            DispatchError: If the method is unknown, the ``connection_id`` does not
                refer to an open connection, or a required param is missing.
        """
        handler = self._route(method)

        conn_id = params.get("connection_id")
        conn = self._connections.get(conn_id) if conn_id else None

        if conn_id and conn is None and method in _CONNECTION_REQUIRED:
            raise DispatchError(f"Unknown connection_id: {conn_id!r}")

        if conn:
            cache = self._caches[conn.id]
            self._idle_timer.reset(conn.id)
            async with conn:
                return await handler(conn, cache, params, send_progress)
        else:
            return await handler(None, None, params, send_progress)

    def _route(self, method: Method) -> Callable[..., Awaitable[dict[str, Any]]]:
        match method:
            case Method.CAPABILITIES:
                return self._handle_capabilities
            case Method.DRIVER_HELP:
                return self._handle_driver_help
            case Method.CONNECT:
                return self._handle_connect
            case Method.DISCONNECT:
                return self._handle_disconnect
            case Method.EXECUTE:
                return self._handle_execute
            case Method.EXPLORE_LIST:
                return self._handle_explore_list
            case Method.EXPLORE_DESCRIBE:
                return self._handle_explore_describe
            case _:
                raise DispatchError(f"Unknown method: {method!r}")

    async def _handle_capabilities(
        self,
        _conn: None,
        _cache: None,
        _params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        return {"server": "belvedere", "drivers": list_drivers()}

    async def _handle_driver_help(
        self,
        _conn: None,
        _cache: None,
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
        _conn: None,
        _cache: None,
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
        conn_id = str(self._next_id)
        self._next_id += 1
        self._connections[conn_id] = Connection(conn_id, driver, self._max_concurrency)
        self._caches.open(conn_id, params)
        timeout = float(params.get("idle_timeout", _DEFAULT_IDLE_TIMEOUT))
        self._idle_timer.start(conn_id, timeout)
        return {"connection_id": conn_id}

    async def _handle_disconnect(
        self,
        conn: "Connection | None",
        _cache: ConnectionCache | None,
        _params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        if conn:
            self._connections.pop(conn.id, None)
            self._caches.close(conn.id)
            self._idle_timer.cancel(conn.id)
            await conn.driver.disconnect()
        return {"ok": True}

    async def _handle_execute(
        self,
        conn: "Connection",
        _cache: ConnectionCache | None,
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
        conn: "Connection",
        cache: ConnectionCache,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            cache.reset()
        items = cache.get_list(path)
        if items is None:
            items = await conn.driver.explore_list(path)
            cache.set_list(path, items)
        else:
            logger.debug(
                f"explore.list cache hit for connection {conn.id!r}, path {path}"
            )
        return {"items": items}

    async def _handle_explore_describe(
        self,
        conn: "Connection",
        cache: ConnectionCache,
        params: dict[str, Any],
        _send_progress: ProgressCallback,
    ) -> dict[str, Any]:
        path: list[str] = params.get("path") or []
        if params.get("reset_cache"):
            cache.reset()
        if cache.has_describe(path):
            logger.debug(
                f"explore.describe cache hit for connection {conn.id!r}, path {path}"
            )
            return {"details": cache.get_describe(path)}
        desc = await conn.driver.explore_describe(path)
        if desc is not None:
            cache.set_describe(path, desc)
        return {"details": desc}

    def _require_param(self, params: dict[str, Any], key: str) -> Any:
        if key not in params:
            raise DispatchError(f"Missing required param: {key!r}")
        return params[key]

    async def _on_idle_expire(self, conn_id: str, timeout: float) -> None:
        logger.info(f"Connection {conn_id!r} idle for {timeout}s — closing")
        conn = self._connections.pop(conn_id, None)
        self._caches.close(conn_id)
        if conn:
            await conn.driver.disconnect()


class CacheStore:
    """Creates and tracks per-connection explore caches backed by a shared directory."""

    def __init__(self, cache_dir: pathlib.Path) -> None:
        self._cache_dir = cache_dir
        """Directory where cache files are persisted."""
        self._caches: dict[str, ConnectionCache] = {}
        """Active caches keyed by connection_id."""

    def open(self, conn_id: str, params: dict[str, Any]) -> None:
        """Create a cache for conn_id, loading any existing data from disk."""
        self._caches[conn_id] = ConnectionCache(
            params, cache_file(params, self._cache_dir)
        )

    def close(self, conn_id: str) -> None:
        """Remove the cache for conn_id (does not delete the disk file)."""
        self._caches.pop(conn_id, None)

    def __getitem__(self, conn_id: str) -> ConnectionCache:
        return self._caches[conn_id]


class IdleTimer:
    """Manages per-connection idle timeout watchdogs."""

    def __init__(self, on_expire: Callable[[str, float], Awaitable[None]]) -> None:
        self._on_expire = on_expire
        """Callback invoked with (conn_id, timeout) when a connection expires."""
        self._timeouts: dict[str, float] = {}
        """Idle timeout in seconds keyed by connection_id."""
        self._tasks: dict[str, asyncio.Task] = {}
        """Running watchdog tasks keyed by connection_id."""

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
