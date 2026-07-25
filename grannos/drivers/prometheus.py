"""Prometheus driver — requires: pip install aiohttp"""

import asyncio
import base64
import re
import time
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from ..protocol import (
    DescribeResult,
    DriverParam,
    DriverParamChoice,
    EntityDescription,
    ExploreItem,
    FieldDescription,
    Language,
    ParamType,
    ReadResult,
    WriteResult,
)
from .base import BaseDriver, ConnectionLostError, DriverError, DriverSettings

_DEFAULT_URL = "http://localhost:9090"

_DURATION_UNITS = {
    "ms": 0.001,
    "s": 1,
    "m": 60,
    "h": 3600,
    "d": 86400,
    "w": 604800,
    "y": 365 * 86400,
}
_DURATION_RE = re.compile(r"^(-?\d+(?:\.\d+)?)(ms|s|m|h|d|w|y)$")


class PrometheusDriver(BaseDriver):
    """Prometheus driver backed by the HTTP query API.

    Args:
        params: Connect request fields (``url``, ``username``, ``password``). ``query_mode``
            is a SESSION_PARAMS setting, not a connect param — see :attr:`SESSION_PARAMS`.
        session: Open aiohttp session. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "Prometheus"

    LANGUAGES = [Language.PROMQL]

    PARAMS: list[DriverParam] = [
        DriverParam(
            key="url", type=ParamType.STRING, label="URL", default=_DEFAULT_URL
        ),
        DriverParam(
            key="username", type=ParamType.STRING, label="Username", required=False
        ),
        DriverParam(
            key="password",
            type=ParamType.STRING,
            label="Password",
            secret=True,
            required=False,
        ),
    ]

    SESSION_PARAMS: list[DriverParam] = [
        DriverParam(
            key="query_mode",
            type=ParamType.ENUM,
            label="Query Mode",
            choices=[
                DriverParamChoice(value="instant", label="Instant"),
                DriverParamChoice(value="range", label="Range"),
            ],
            default="instant",
        ),
    ]

    HELP: str = """\
## Prometheus

**Queries:** PromQL, evaluated via the connection's `query_mode` session setting
(instant/range — change it any time via `session.set`, no reconnect needed).

*Instant mode* (default) — a plain PromQL expression, evaluated at the current time:

```
rate(http_requests_total[5m])
```

```
sum by (job) (up)
```

*Range mode* — prefix with `<start>,<end>,<step> | ` giving the evaluation window.
`start`/`end` accept `now`, a relative offset (`-1h`, `-30m`, `-15s`), an RFC3339
timestamp, or a raw Unix timestamp. `step` is a Prometheus duration (`15s`, `1m`).

```
-1h,now,15s | rate(http_requests_total[5m])
```

```
2024-01-01T00:00:00Z,2024-01-01T01:00:00Z,30s | up
```

Vector/matrix results are flattened to one row per series (range queries emit one
row per series per timestamp), with a column per label plus `timestamp` and `value`.
Scalar/string results return a single `timestamp`/`value` row.

**Resources:**

```
(root)
└── metrics
    └── <metric>
        └── <label>
```

Requires Prometheus >= 2.24 (`/api/v1/labels` with `match[]` support).

`explore.describe` on `["metrics", metric]` returns label metadata (name, up to
3 sampled values), with `kind` and `comment` populated from `/api/v1/metadata`
(type and help text) when available.
"""

    def __init__(
        self,
        params: dict[str, Any],
        session: aiohttp.ClientSession,
        settings: DriverSettings,
    ) -> None:
        super().__init__(params, settings)
        self._http = session
        self._url = str(params.get("url") or _DEFAULT_URL).rstrip("/")
        self._ever_connected = False
        self._session_values: dict[str, Any] = {
            p.key: p.default for p in self.SESSION_PARAMS
        }
        """Runtime SESSION_PARAMS values, seeded from their declared defaults."""

    @classmethod
    async def create(
        cls, params: dict[str, Any], settings: DriverSettings
    ) -> "PrometheusDriver":
        return cls(params, cls._open(params), settings)

    @staticmethod
    def _open(params: dict[str, Any]) -> aiohttp.ClientSession:
        headers = {}
        username = params.get("username")
        password = params.get("password")
        if username and password:
            token = base64.b64encode(f"{username}:{password}".encode()).decode()
            headers["Authorization"] = f"Basic {token}"
        return aiohttp.ClientSession(headers=headers)

    async def reconnect(self) -> None:
        await self._http.close()
        self._http = self._open(self.params)
        self._ever_connected = False

    async def disconnect(self) -> None:
        await self._http.close()

    async def execute(
        self,
        query: str,
        binds: list[Any],
        diagram_captions: dict[str, str] | None = None,
    ) -> ReadResult | WriteResult:
        """Run a PromQL query.

        Args:
            query: A PromQL expression (instant mode), or a range query prefixed
                with ``<start>,<end>,<step> | `` (range mode).
            binds: Unused for Prometheus.
            diagram_captions: Unused for Prometheus (not a graph driver).
        """
        mode = self._session_values.get("query_mode", "instant")
        if mode == "instant":
            return await self._execute_instant(query)
        if mode == "range":
            return await self._execute_range(query)
        raise DriverError(f"Unknown query_mode: {mode!r}")

    async def set_session(self, values: dict[str, Any]) -> None:
        if "query_mode" in values:
            mode = values["query_mode"]
            if mode not in ("instant", "range"):
                raise DriverError(f"Unknown query_mode: {mode!r}")
            self._session_values["query_mode"] = mode

    def get_session(self) -> dict[str, Any]:
        return dict(self._session_values)

    async def _execute_instant(self, query: str) -> ReadResult:
        data = await self._get("/api/v1/query", {"query": query.strip()})
        return _data_to_result(data)

    async def _execute_range(self, query: str) -> ReadResult:
        start, end, step, promql = _parse_range_query(query)
        now = time.time()
        data = await self._get(
            "/api/v1/query_range",
            {
                "query": promql,
                "start": _resolve_time(start, now),
                "end": _resolve_time(end, now),
                "step": step,
            },
        )
        return _data_to_result(data)

    async def _get(self, path: str, params: dict[str, str]) -> Any:
        try:
            async with self._http.get(f"{self._url}{path}", params=params) as resp:
                body = await resp.json(content_type=None)
        except Exception as exc:
            if isinstance(exc, (aiohttp.ClientConnectionError, TimeoutError)):
                if self._ever_connected:
                    raise ConnectionLostError(str(exc)) from exc
                raise DriverError(str(exc)) from exc
            raise DriverError(str(exc)) from exc
        if not isinstance(body, dict) or body.get("status") != "success":
            raise DriverError(_format_error(body))
        self._ever_connected = True
        return body["data"]

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                return [ExploreItem(name="metrics", type="group", expandable=True)]
            case ["metrics"]:
                names = await self._get("/api/v1/label/__name__/values", {})
                return [
                    ExploreItem(name=name, type="metric", expandable=True)
                    for name in sorted(names)
                ]
            case ["metrics", metric]:
                labels = await self._get("/api/v1/labels", {"match[]": metric})
                return [
                    ExploreItem(name=label, type="label", expandable=False)
                    for label in sorted(labels)
                    if label != "__name__"
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> DescribeResult:
        match path:
            case ["metrics", metric]:
                labels = await self._get("/api/v1/labels", {"match[]": metric})
                label_names = sorted(label for label in labels if label != "__name__")
                metadata = await self._metric_metadata(metric)
                properties = [
                    FieldDescription(
                        name=label,
                        types=["label"],
                        sample=await self._label_values_sample(metric, label),
                    )
                    for label in label_names
                ]
                return EntityDescription(
                    name=metric,
                    kind=metadata.get("type", "metric"),
                    properties=properties,
                    comment=metadata.get("help"),
                )
            case _:
                return None

    async def _metric_metadata(self, metric: str) -> dict[str, str]:
        try:
            data = await self._get("/api/v1/metadata", {"metric": metric})
        except DriverError:
            return {}
        entries = data.get(metric) or []
        if not entries:
            return {}
        entry = entries[0]
        result = {"type": entry.get("type", "metric")}
        if entry.get("help"):
            result["help"] = entry["help"]
        return result

    async def _label_values_sample(self, metric: str, label: str) -> list[Any]:
        try:
            return await asyncio.wait_for(
                self._fetch_label_values_sample(metric, label),
                timeout=self._settings.column_sample_timeout,
            )
        except asyncio.TimeoutError:
            return []

    async def _fetch_label_values_sample(self, metric: str, label: str) -> list[Any]:
        values = await self._get(
            f"/api/v1/label/{quote(label, safe='')}/values", {"match[]": metric}
        )
        return values[: self._settings.column_sample_size]


def _parse_range_query(query: str) -> tuple[str, str, str, str]:
    if " | " not in query:
        raise DriverError(
            "Range query must be in the format: <start>,<end>,<step> | <promql>\n"
            "Example: -1h,now,15s | rate(http_requests_total[5m])"
        )
    header, _, promql = query.partition(" | ")
    parts = [p.strip() for p in header.split(",")]
    if len(parts) != 3:
        raise DriverError(
            "Range header must have 3 comma-separated fields: <start>,<end>,<step>"
        )
    start, end, step = parts
    return start, end, step, promql.strip()


def _resolve_time(value: str, now: float) -> str:
    value = value.strip()
    if value == "now":
        return str(now)
    match = _DURATION_RE.match(value)
    if match:
        amount, unit = match.groups()
        return str(now + float(amount) * _DURATION_UNITS[unit])
    return value


def _format_error(body: Any) -> str:
    if not isinstance(body, dict):
        return f"Prometheus error: {body}"
    error = body.get("error", body)
    error_type = body.get("errorType")
    if error_type:
        return f"Prometheus error ({error_type}): {error}"
    return f"Prometheus error: {error}"


def _format_timestamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=UTC).isoformat()


def _data_to_result(data: dict[str, Any]) -> ReadResult:
    result_type = data.get("resultType")
    result = data.get("result", [])
    if result_type == "vector":
        return _series_to_result(result, ranged=False)
    if result_type == "matrix":
        return _series_to_result(result, ranged=True)
    if result_type in ("scalar", "string"):
        ts, value = result
        return ReadResult(
            columns=["timestamp", "value"],
            rows=[[_format_timestamp(float(ts)), value]],
            rows_total=1,
        )
    raise DriverError(f"Unsupported Prometheus result type: {result_type!r}")


def _series_to_result(series: list[dict[str, Any]], ranged: bool) -> ReadResult:
    label_names = sorted(
        {k for s in series for k in s.get("metric", {}) if k != "__name__"}
    )
    has_name = any(s.get("metric", {}).get("__name__") for s in series)
    columns = (["__name__"] if has_name else []) + label_names + ["timestamp", "value"]
    rows: list[list[Any]] = []
    for s in series:
        metric = s.get("metric", {})
        base: list[Any] = [metric.get("__name__")] if has_name else []
        base += [metric.get(name) for name in label_names]
        if ranged:
            for ts, value in s["values"]:
                rows.append([*base, _format_timestamp(float(ts)), _parse_value(value)])
        else:
            ts, value = s["value"]
            rows.append([*base, _format_timestamp(float(ts)), _parse_value(value)])
    return ReadResult(columns=columns, rows=rows, rows_total=len(rows))


def _parse_value(value: str) -> Any:
    try:
        return float(value)
    except TypeError, ValueError:
        return value
