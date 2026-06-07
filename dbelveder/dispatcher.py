from typing import Any

from .drivers import get_driver
from .drivers.base import BaseDriver


class Dispatcher:
    def __init__(self) -> None:
        self._driver: BaseDriver | None = None

    async def dispatch(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        # "explore.list" → "_handle_explore_list"
        handler_name = "_handle_" + method.replace(".", "_")
        handler = getattr(self, handler_name, None)
        if handler is None:
            raise ValueError(f"Unknown method: {method!r}")
        return await handler(params)

    # ── connection ──────────────────────────────────────────────────────────

    async def _handle_connect(self, params: dict[str, Any]) -> dict[str, Any]:
        driver_name: str = params["driver"]
        if self._driver is not None:
            await self._driver.disconnect()
        self._driver = get_driver(driver_name)(params)
        await self._driver.connect()
        return {"ok": True}

    async def _handle_disconnect(self, params: dict[str, Any]) -> dict[str, Any]:
        if self._driver:
            await self._driver.disconnect()
            self._driver = None
        return {"ok": True}

    # ── query ────────────────────────────────────────────────────────────────

    async def _handle_execute(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_connected()
        sql: str = params["sql"]
        binds: list[Any] = params.get("params") or []
        assert self._driver
        columns, rows = await self._driver.execute(sql, binds)
        return {"columns": columns, "rows": rows}

    # ── exploration ──────────────────────────────────────────────────────────

    async def _handle_explore_list(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_connected()
        path: list[str] = params.get("path") or []
        assert self._driver
        items = await self._driver.explore_list(path)
        return {"items": items}

    async def _handle_explore_describe(self, params: dict[str, Any]) -> dict[str, Any]:
        self._require_connected()
        path: list[str] = params.get("path") or []
        assert self._driver
        details = await self._driver.explore_describe(path)
        return {"details": details}

    # ── helpers ──────────────────────────────────────────────────────────────

    def _require_connected(self) -> None:
        if self._driver is None:
            raise RuntimeError("Not connected — call 'connect' first")
