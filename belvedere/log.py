import logging
import pathlib
from contextvars import ContextVar

request_id_var: ContextVar[int | None] = ContextVar("request_id", default=None)
"""Current request ID, set per-task so concurrent requests don't bleed into each other."""


class RequestIdFilter(logging.Filter):
    """Injects the current request ID into every log record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        record.request_id = str(rid) if rid is not None else "-"  # type: ignore[attr-defined]
        return True


def configure(path: pathlib.Path, level: int) -> None:
    """Configure the root logger to write to path with request ID in every line."""
    handler = logging.FileHandler(path)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
