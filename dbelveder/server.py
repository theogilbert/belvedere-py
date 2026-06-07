import asyncio
import sys
from io import BufferedWriter
from typing import Any

from .dispatcher import Dispatcher
from .protocol import Response, decode, encode


class Server:
    def __init__(self) -> None:
        self._dispatcher = Dispatcher()
        self._stdout: BufferedWriter = sys.stdout.buffer  # type: ignore[assignment]
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        reader = asyncio.StreamReader()
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            sys.stdin.buffer,
        )
        while True:
            line = await reader.readline()
            if not line:
                break
            msg: dict[str, Any] = decode(line)
            asyncio.create_task(self._handle(msg))

    async def _handle(self, msg: dict[str, Any]) -> None:
        msg_id: int | None = msg.get("id")
        method: str = msg.get("method") or ""
        params: dict[str, Any] = msg.get("params") or {}
        response: Response
        try:
            result = await self._dispatcher.dispatch(method, params)
            response = {"id": msg_id, "result": result, "error": None}
        except Exception as exc:
            response = {"id": msg_id, "result": None, "error": str(exc)}
        await self._send(response)

    async def _send(self, msg: dict[str, Any]) -> None:
        data = encode(msg)
        async with self._lock:
            self._stdout.write(data)
            self._stdout.flush()
