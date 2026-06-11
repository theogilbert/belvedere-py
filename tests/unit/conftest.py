import io
import os

import pytest
from pytest import MonkeyPatch


@pytest.fixture
def mock_stdin(monkeypatch: MonkeyPatch) -> io.RawIOBase:
    """Replace sys.stdin with a pipe; yields the write end as a BinaryIO.

    Write lines to it; Server.run() reads from the other end and returns on EOF.
    """
    r, w = os.pipe()

    class _Buf:
        def fileno(self) -> int: return r
        def close(self) -> None: pass

    class _Stdin:
        buffer = _Buf()

    monkeypatch.setattr("sys.stdin", _Stdin())
    stdin_write = os.fdopen(w, "wb", buffering=0)
    yield stdin_write
    stdin_write.close()  # idempotent if the test already closed it
