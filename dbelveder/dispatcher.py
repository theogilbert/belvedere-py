from typing import Any

from .drivers import get_driver
from .drivers.base import BaseDriver, ConnectionLostError
from .protocol import ProgressCallback


class Dispatcher:
    def __init__(self) -> None:
        self._connections: dict[str, BaseDriver] = {}
        self._next_id: int = 0

    async def dispatch(
        self, method: str, params: dict[str, Any], send_progress: ProgressCallback
    ) -> dict[str, Any]:
        handler_name = "_handle_" + method.replace(".", "_")
        handler = getattr(self, handler_name, None)
        if handler is None:
            raise ValueError(f"Unknown method: {method!r}")
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
        return {"connection_id": conn_id}

    async def _handle_disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        conn_id: str = params["connection_id"]
        driver = self._connections.pop(conn_id, None)
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
        driver = self._require_connection(params["connection_id"])
        path: list[str] = params.get("path") or []
        items = await driver.explore_list(path)
        return {"items": items}

    async def _handle_explore_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        driver = self._require_connection(params["connection_id"])
        path: list[str] = params.get("path") or []
        details = await driver.explore_describe(path)
        return {"details": details}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _require_connection(self, conn_id: str) -> BaseDriver:
        driver = self._connections.get(conn_id)
        if driver is None:
            raise KeyError(f"Unknown connection_id: {conn_id!r}")
        return driver
