import logging

import pytest

from grannos.log import LOG_CAP, RequestIdFilter, log_query, request_id_var, truncate


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


class TestTruncate:
    def test_leaves_text_at_the_cap_untouched(self) -> None:
        assert truncate("x" * LOG_CAP) == "x" * LOG_CAP

    def test_marks_the_cut_with_an_ellipsis(self) -> None:
        assert truncate("x" * (LOG_CAP + 10)) == "x" * LOG_CAP + "…"

    def test_honours_a_tighter_cap(self) -> None:
        assert truncate("abcdef", 3) == "abc…"


class TestLogQuery:
    """One log line per statement a driver sends, so a debug log shows exactly
    what reached the database."""

    def test_logs_the_statement(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "SELECT 1 FROM dual")
        assert "query SELECT 1 FROM dual" in caplog.text

    def test_collapses_whitespace_to_one_line(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """A multi-line statement has to stay a single grep-able record."""
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "SELECT 1\n  FROM   dual\n")
        assert "query SELECT 1 FROM dual" in caplog.text
        assert "\n" not in caplog.records[0].getMessage()

    def test_logs_sequence_binds(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "SELECT 1", ["HR", "EMPLOYEES"])
        assert "binds ['HR', 'EMPLOYEES']" in caplog.text

    def test_renders_mapping_binds_with_their_values(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """list() on a mapping would log the keys and drop every value."""
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "MATCH (n)", {"label": "Person"})
        assert "binds {'label': 'Person'}" in caplog.text

    def test_omits_the_binds_clause_when_there_are_none(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "SELECT 1", [])
        assert "binds" not in caplog.text

    def test_truncates_a_long_statement(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.DEBUG):
            log_query(logging.getLogger("t"), "S" * (LOG_CAP + 50))
        assert "…" in caplog.text
        assert len(caplog.records[0].getMessage()) < LOG_CAP + 50

    def test_is_silent_above_debug(self, caplog: pytest.LogCaptureFixture) -> None:
        with caplog.at_level(logging.INFO):
            log_query(logging.getLogger("t"), "SELECT 1")
        assert caplog.text == ""
