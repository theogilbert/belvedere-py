import asyncio
import json
import logging
import pathlib
import sys
from typing import Any, BinaryIO

from . import log
from .dispatcher import DispatchError, Dispatcher
from .drivers import SENSITIVE_PARAM_KEYS
from .drivers.base import DriverError, DriverSettings
from .log import truncate
from .protocol import (
    DecodeError,
    Method,
    Progress,
    ProgressDetail,
    Request,
    Response,
    Result,
    decode,
    encode,
)


logger = logging.getLogger(__name__)


class Server:
    """Stdio JSON server — reads requests from stdin, writes responses to stdout.

    Args:
        cache_dir: Directory for persisting explore caches between sessions.
        max_concurrency: Maximum concurrent requests allowed per connection.
        column_sample_size: Number of distinct non-null values sampled per column in describe results.
        stdin: Binary stream to read requests from (typically ``sys.stdin.buffer``).
        stdout: Binary stream to write responses to (typically ``sys.stdout.buffer``).
    """

    def __init__(
        self,
        cache_dir: pathlib.Path,
        driver_settings: DriverSettings,
        max_concurrency: int = 1,
        stdin: BinaryIO = sys.stdin.buffer,
        stdout: BinaryIO = sys.stdout.buffer,
    ) -> None:
        self._dispatcher = Dispatcher(
            max_concurrency=max_concurrency,
            cache_dir=cache_dir,
            driver_settings=driver_settings,
        )
        self._out = stdout
        self._stdin = stdin
        self._lock = asyncio.Lock()
        """Serializes concurrent response writes to stdout."""
        self._tasks: dict[int, asyncio.Task[None]] = {}
        """Maps request id to its in-flight task, enabling cancellation by id."""

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
                    f"Received {truncate(json.dumps({'id': req.id, 'method': req.method, 'params': _redact(req.params)}))}"
                )
                if req.method == Method.CANCEL:
                    asyncio.create_task(self._handle_cancel(req))
                else:
                    task = asyncio.create_task(self._handle(req))
                    self._tasks[req.id] = task
                    task.add_done_callback(lambda _: self._tasks.pop(req.id, None))
            except DecodeError as e:
                logger.warning(f"Received invalid request: {e}")
                asyncio.create_task(
                    self._send(Result(id=e.id, result=None, error=str(e)))
                )

    async def _handle(self, req: Request) -> None:
        log.request_id_var.set(req.id)

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
        except asyncio.CancelledError:
            response = Result(id=req.id, result=None, error="cancelled")
        except Exception:
            logger.exception(f"Unhandled error for request {req.id}")
            response = Result(id=req.id, result=None, error="internal error")
        await self._send(response)

    async def _handle_cancel(self, req: Request) -> None:
        target_id = req.params.get("request_id")
        if not isinstance(target_id, int):
            await self._send(
                Result(
                    id=req.id, result=None, error="Missing required param: 'request_id'"
                )
            )
            return
        task = self._tasks.get(target_id)
        if task is not None:
            task.cancel()
        await self._send(Result(id=req.id, result={"ok": True}, error=None))

    async def _send(self, msg: Response) -> None:
        data = encode(msg)
        async with self._lock:
            self._out.write(data)
            self._out.flush()
            logger.debug(f"Sent {truncate(data.decode(errors='replace').rstrip())}")


def _redact(params: dict[str, Any]) -> dict[str, Any]:
    return {k: "***" if k in SENSITIVE_PARAM_KEYS else v for k, v in params.items()}
