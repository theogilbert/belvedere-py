import asyncio
import json
import logging
import pathlib
import sys
from typing import Any, BinaryIO

from .dispatcher import DispatchError, Dispatcher
from .drivers import SENSITIVE_PARAM_KEYS
from .drivers.base import DriverError
from .protocol import (
    DecodeError,
    Progress,
    ProgressDetail,
    Request,
    Response,
    Result,
    decode,
    encode,
)


_LOG_CAP = 512

logger = logging.getLogger(__name__)


class Server:
    """Stdio JSON server — reads requests from stdin, writes responses to out.

    Args:
        out: Binary stream to write responses to (typically ``sys.stdout.buffer``).
        cache_dir: Directory for persisting explore caches between sessions.
        max_concurrency: Maximum concurrent requests allowed per connection.
    """

    def __init__(
        self,
        cache_dir: pathlib.Path,
        max_concurrency: int = 1,
        stdin: BinaryIO = sys.stdin.buffer,
        stdout: BinaryIO = sys.stdout.buffer,
    ) -> None:
        self._dispatcher = Dispatcher(
            max_concurrency=max_concurrency, cache_dir=cache_dir
        )
        self._out = stdout
        self._stdin = stdin
        self._lock = asyncio.Lock()
        """Serializes concurrent response writes to stdout."""

    async def run(self) -> None:
        """Start the read loop.

        Reads newline-delimited JSON requests from stdin and dispatches each as
        a concurrent task. Returns when stdin is closed.
        """
        reader = asyncio.StreamReader()
        loop = asyncio.get_event_loop()
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader),
            self._stdin,
        )
        logger.info("Server ready")
        while True:
            line = await reader.readline()
            if not line:
                logger.info("Server exiting")
                break
            try:
                req: Request = decode(line)
                logger.debug(
                    f"Received {_truncate(json.dumps({'id': req.id, 'method': req.method, 'params': _redact(req.params)}))}"
                )
                asyncio.create_task(self._handle(req))
            except DecodeError as e:
                logger.warning(f"Received invalid request: {e}")
                asyncio.create_task(
                    self._send(Result(id=e.id, result=None, error=str(e)))
                )

    async def _handle(self, req: Request) -> None:
        async def send_progress(status: str, message: str) -> None:
            await self._send(
                Progress(
                    id=req.id,
                    progress=ProgressDetail(status=status, message=message),
                )
            )

        response: Result
        try:
            result = await self._dispatcher.dispatch(
                req.method, req.params, send_progress
            )
            response = Result(id=req.id, result=result, error=None)
        except (DispatchError, DriverError) as exc:
            response = Result(id=req.id, result=None, error=str(exc))
        except Exception:
            logger.exception(f"Unhandled error for request {req.id}")
            response = Result(id=req.id, result=None, error="internal error")
        await self._send(response)

    async def _send(self, msg: Response) -> None:
        data = encode(msg)
        async with self._lock:
            logger.debug(f"Sent {_truncate(data.decode(errors='replace').rstrip())}")
            self._out.write(data)
            self._out.flush()


def _truncate(text: str) -> str:
    return text[:_LOG_CAP] + "…" if len(text) > _LOG_CAP else text


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: "***" if k in SENSITIVE_PARAM_KEYS else v for k, v in params.items()}
