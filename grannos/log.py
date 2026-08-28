import logging
import logging.handlers
import pathlib
from collections.abc import Mapping, Sequence
from contextvars import ContextVar
from typing import Any

request_id_var: ContextVar[int | None] = ContextVar("request_id", default=None)
"""Current request ID, set per-task so concurrent requests don't bleed into each other."""


class RequestIdFilter(logging.Filter):
    """Injects the current request ID into every log record as ``request_id``."""

    def filter(self, record: logging.LogRecord) -> bool:
        rid = request_id_var.get()
        record.request_id = str(rid) if rid is not None else "-"  # type: ignore[attr-defined]
        return True


LOG_CAP = 512
"""Longest single value written to a log line; anything beyond is elided. Keeps
one oversized query or payload from burying the rest of the file."""

_BIND_CAP = 200
"""Binds are context, not the subject of the line, so they get a tighter cap."""


def truncate(text: str, cap: int = LOG_CAP) -> str:
    """Return *text* cut to *cap* characters, marking the cut with an ellipsis."""
    return text[:cap] + "…" if len(text) > cap else text


def log_query(
    logger: logging.Logger,
    statement: str,
    binds: Sequence[Any] | Mapping[str, Any] | None = None,
) -> None:
    """Log one statement a driver is about to send to its database, at DEBUG.

    Whitespace is collapsed so a multi-line statement stays a single grep-able
    log line.

    Args:
        logger: The calling driver's logger, so the record carries its module.
        statement: Query text, or an API operation name for a non-SQL backend.
        binds: Bind values, logged only when passed. Drivers pass them for their
            own catalog queries, whose parameters are schema and object names —
            never for the user's statement, whose binds are user data.
    """
    if not logger.isEnabledFor(logging.DEBUG):
        return
    text = truncate(" ".join(statement.split()))
    if binds:
        # dict() rather than list() for a mapping, which would render keys alone.
        rendered = repr(dict(binds) if isinstance(binds, Mapping) else list(binds))
        logger.debug("query %s -- binds %s", text, truncate(rendered, _BIND_CAP))
    else:
        logger.debug("query %s", text)


_10_MB = 10 * 1024 * 1024


def configure(path: pathlib.Path, level: int) -> None:
    """Configure the root logger to write to path with request ID in every line."""
    handler = logging.handlers.RotatingFileHandler(path, maxBytes=_10_MB, backupCount=1)
    handler.addFilter(RequestIdFilter())
    handler.setFormatter(
        logging.Formatter("%(asctime)s - %(levelname)s - [%(request_id)s] %(message)s")
    )
    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)
