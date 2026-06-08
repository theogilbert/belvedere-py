import asyncio
import sys
from typing import BinaryIO

from .dispatcher import Dispatcher
from .protocol import (
    Progress,
    ProgressDetail,
    Request,
    Response,
    Result,
    decode,
    encode,
)


class Server:
    def __init__(self) -> None:
        self._dispatcher = Dispatcher()
        self._stdout: BinaryIO = sys.stdout.buffer
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
            msg: Request = decode(line)
            asyncio.create_task(self._handle(msg))

    async def _handle(self, msg: Request) -> None:
        async def send_progress(status: str, message: str) -> None:
            await self._send(
                Progress(
                    id=msg.id, progress=ProgressDetail(status=status, message=message)
                )
            )

        response: Result
        try:
            result = await self._dispatcher.dispatch(
                msg.method or "", msg.params or {}, send_progress
            )
            response = Result(id=msg.id, result=result, error=None)
        except Exception as exc:
            response = Result(id=msg.id, result=None, error=str(exc))
        await self._send(response)

    async def _send(self, msg: Response) -> None:
        data = encode(msg)
        async with self._lock:
            self._stdout.write(data)
            self._stdout.flush()
