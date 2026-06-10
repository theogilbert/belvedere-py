import asyncio
import json
import logging
import pathlib
import sys
from typing import Any, BinaryIO

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


_LOG_CAP = 512
_SENSITIVE_KEYS = frozenset({"password"})

logger = logging.getLogger(__name__)


def _truncate(text: str) -> str:
    return text[:_LOG_CAP] + "…" if len(text) > _LOG_CAP else text


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: "***" if k in _SENSITIVE_KEYS else v for k, v in params.items()}


class Server:
    """Stdio JSON server — reads requests from stdin, writes responses to out.

    Args:
        out: Binary stream to write responses to (typically ``sys.stdout.buffer``).
        cache_dir: Directory for persisting explore caches between sessions.
        max_concurrency: Maximum concurrent requests allowed per connection.
    """

    def __init__(self, out: BinaryIO, cache_dir: pathlib.Path, max_concurrency: int = 1) -> None:
        self._dispatcher = Dispatcher(max_concurrency=max_concurrency, cache_dir=cache_dir)
        self._out = out
        self._lock = asyncio.Lock()

    async def run(self) -> None:
        """Start the read loop.

        Reads newline-delimited JSON requests from stdin and dispatches each as
        a concurrent task. Returns when stdin is closed.
        """
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
            try:
                msg: Request = decode(line)
                logger.debug(f"Received {_truncate(json.dumps({'id': msg.id, 'method': msg.method, 'params': _redact(msg.params)}))}")
                asyncio.create_task(self._handle(msg))
            except json.JSONDecodeError:
                asyncio.create_task(
                    self._send(Result(id=None, result=None, error="decode error"))
                )
            except TypeError:
                asyncio.create_task(
                    self._send(Result(id=None, result=None, error="invalid request"))
                )

    async def _handle(self, msg: Request) -> None:
        async def send_progress(status: str, message: str) -> None:
            await self._send(
                Progress(
                    id=msg.id,
                    progress=ProgressDetail(status=status, message=message),
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
            logger.debug(f"Sent {_truncate(data.decode(errors='replace').rstrip())}")
            self._out.write(data)
            self._out.flush()
