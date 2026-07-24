"""
Integration tests for the Prometheus driver.

Requires a running Prometheus instance whose default config self-scrapes
(job "prometheus", instance "localhost:9090") — the stock behaviour of the
official `prom/prometheus` image. Configure via environment variable:
  PROMETHEUS_URL  (default: http://localhost:9090)

Tests are skipped automatically when aiohttp is not installed or the server
is unreachable.
"""

import os
from collections.abc import AsyncGenerator

import pytest

from grannos.drivers.base import DriverError, DriverSettings
from grannos.drivers.prometheus import PrometheusDriver
from grannos.protocol import EntityDescription, ReadResult

pytestmark = pytest.mark.external


def _params(**overrides: str) -> dict:
    return {
        "url": os.environ.get("PROMETHEUS_URL", "http://localhost:9090"),
        **overrides,
    }


@pytest.fixture
async def driver() -> AsyncGenerator[PrometheusDriver, None]:
    pytest.importorskip("aiohttp")
    d = await PrometheusDriver.create(_params(), DriverSettings())
    yield d
    await d.disconnect()


@pytest.fixture
async def range_driver() -> AsyncGenerator[PrometheusDriver, None]:
    pytest.importorskip("aiohttp")
    d = await PrometheusDriver.create(_params(query_mode="range"), DriverSettings())
    yield d
    await d.disconnect()


class TestExecuteInstant:
    async def test_returns_columns_and_rows(self, driver: PrometheusDriver) -> None:
        result = await driver.execute("up", [])
        assert isinstance(result, ReadResult)
        assert "job" in result.columns
        assert "instance" in result.columns
        assert "timestamp" in result.columns
        assert "value" in result.columns
        assert result.rows_total >= 1

    async def test_no_match_returns_empty(self, driver: PrometheusDriver) -> None:
        result = await driver.execute("nonexistent_metric_xyz", [])
        assert isinstance(result, ReadResult)
        assert result.rows == []

    async def test_invalid_promql_raises(self, driver: PrometheusDriver) -> None:
        with pytest.raises(DriverError):
            await driver.execute("up(((", [])


class TestExecuteRange:
    async def test_returns_rows_across_the_window(
        self, range_driver: PrometheusDriver
    ) -> None:
        result = await range_driver.execute("-1m,now,15s | up", [])
        assert isinstance(result, ReadResult)
        assert "timestamp" in result.columns
        assert result.rows_total >= 1

    async def test_missing_separator_raises(
        self, range_driver: PrometheusDriver
    ) -> None:
        with pytest.raises(DriverError, match="format"):
            await range_driver.execute("up", [])


class TestExploreList:
    async def test_root_lists_metrics(self, driver: PrometheusDriver) -> None:
        items = await driver.explore_list([])
        names = [i.name for i in items]
        assert "up" in names
        assert all(i.type == "metric" and i.expandable for i in items)

    async def test_metric_lists_labels(self, driver: PrometheusDriver) -> None:
        items = await driver.explore_list(["up"])
        names = [i.name for i in items]
        assert "job" in names
        assert "instance" in names
        assert "__name__" not in names

    async def test_label_lists_values(self, driver: PrometheusDriver) -> None:
        items = await driver.explore_list(["up", "job"])
        names = [i.name for i in items]
        assert "prometheus" in names

    async def test_unknown_path_returns_empty(self, driver: PrometheusDriver) -> None:
        items = await driver.explore_list(["up", "job", "prometheus", "extra"])
        assert items == []


class TestExploreDescribe:
    async def test_returns_entity_description_for_metric(
        self, driver: PrometheusDriver
    ) -> None:
        result = await driver.explore_describe(["up"])
        assert isinstance(result, EntityDescription)
        assert result.name == "up"
        label_names = [f.name for f in result.properties]
        assert "job" in label_names
        assert "instance" in label_names

    async def test_labels_carry_sampled_values(self, driver: PrometheusDriver) -> None:
        result = await driver.explore_describe(["up"])
        assert isinstance(result, EntityDescription)
        job_field = next(f for f in result.properties if f.name == "job")
        assert "prometheus" in job_field.sample

    async def test_returns_none_for_unknown_path(
        self, driver: PrometheusDriver
    ) -> None:
        assert await driver.explore_describe([]) is None
