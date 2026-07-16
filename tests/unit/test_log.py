import logging

import pytest

from grannos.log import RequestIdFilter, request_id_var


@pytest.fixture
def record() -> logging.LogRecord:
    return logging.LogRecord("test", logging.DEBUG, "", 0, "msg", (), None)


@pytest.fixture
def filt() -> RequestIdFilter:
    return RequestIdFilter()


def test_filter_sets_dash_when_no_request_id(
    filt: RequestIdFilter, record: logging.LogRecord
) -> None:
    filt.filter(record)
    assert record.request_id == "-"  # ty: ignore[unresolved-attribute]


def test_filter_sets_request_id_when_set(
    filt: RequestIdFilter, record: logging.LogRecord
) -> None:
    token = request_id_var.set(42)
    try:
        filt.filter(record)
        assert record.request_id == "42"  # ty: ignore[unresolved-attribute]
    finally:
        request_id_var.reset(token)


def test_filter_always_returns_true(
    filt: RequestIdFilter, record: logging.LogRecord
) -> None:
    assert filt.filter(record) is True
