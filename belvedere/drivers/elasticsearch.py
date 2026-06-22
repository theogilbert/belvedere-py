"""Elasticsearch driver — requires: pip install elasticsearch aiohttp"""

import json
from typing import Any

import elasticsearch

from ..protocol import (
    ColumnInfo,
    DriverParam,
    DriverParamChoice,
    ExploreItem,
    ParamType,
    ReadResult,
    TableDescription,
    WriteResult,
)
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError, DriverError

_DEFAULT_SEARCH_SIZE = 1000


class ElasticsearchDriver(BaseDriver):
    """Elasticsearch driver backed by the official elasticsearch-py client.

    Args:
        params: Connect request fields (``host``, ``port``, ``username``, ``password``,
            ``query_mode``).
        client: Open Elasticsearch client. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "Elasticsearch"

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type=ParamType.STRING, label="Host"),
        DriverParam(key="port", type=ParamType.INTEGER, label="Port", default=9200),
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
        DriverParam(
            key="query_mode",
            type=ParamType.ENUM,
            label="Query Mode",
            choices=[
                DriverParamChoice(value="lucene", label="Lucene"),
                DriverParamChoice(value="dev_tools", label="Dev Tools"),
            ],
            default="lucene",
        ),
    ]

    HELP: str = """\
## Elasticsearch

**Install:** `pip install elasticsearch aiohttp`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | no | `localhost` | Server hostname or IP |
| `port` | no | `9200` | HTTP port |
| `username` | no | — | Username |
| `password` | no | — | Password (masked) |
| `query_mode` | no | `lucene` | Query language: `lucene` or `dev_tools` |

**Queries:** Prefix with the target index name (pattern or alias) and ` | `.

*Lucene mode:*

```
orders | status:open AND total:>50
```

```
orders | *
```

*Dev Tools mode (Kibana Dev Tools syntax):*

```
GET /orders/_search
{"query": {"match": {"status": "open"}}}
```

```
GET /orders,products/_search
{"query": {"match_all": {}}, "sort": [{"total": "desc"}]}
```

Any Elasticsearch REST endpoint is accepted — the response is returned as a
flat table. Search responses unpack `hits.hits`; all other responses are
flattened as a single row.

System indices (names starting with `.`) are hidden in the explore tree.

**Explore tree:**

```
(root)
└── <index>
    ├── mappings  → field name and type
    └── aliases   → alias names
```

`explore.describe` is supported on `[index]` paths and returns field metadata
from the index mapping (name, type).
"""

    def __init__(
        self, params: dict[str, Any], client: elasticsearch.AsyncElasticsearch
    ) -> None:
        super().__init__(params)
        self._client = client
        self._ever_connected = False

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "ElasticsearchDriver":
        return cls(params, cls._open(params))

    @staticmethod
    def _open(params: dict[str, Any]) -> elasticsearch.AsyncElasticsearch:
        host = params.get("host", "localhost")
        port = int(params.get("port", 9200))
        kwargs: dict[str, Any] = {"hosts": [f"http://{host}:{port}"]}
        username = params.get("username")
        password = params.get("password")
        if username and password:
            kwargs["basic_auth"] = (username, password)
        return elasticsearch.AsyncElasticsearch(**kwargs)

    async def reconnect(self) -> None:
        await self._client.close()
        self._client = self._open(self.params)
        self._ever_connected = False

    async def disconnect(self) -> None:
        await self._client.close()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        try:
            result = await self._execute(query)
            self._ever_connected = True
            return result
        except Exception as exc:
            if isinstance(exc, elasticsearch.ConnectionError):
                if self._ever_connected:
                    raise ConnectionLostError(str(exc)) from exc
                raise DriverError(str(exc)) from exc
            raise DriverError(str(exc)) from exc

    async def _execute(self, query: str) -> ReadResult:
        mode = self.params.get("query_mode", "lucene")
        if mode == "lucene":
            return await self._execute_lucene(query)
        elif mode == "dev_tools":
            return await self._execute_dev_tools(query)
        else:
            raise DriverError(f"Unknown query_mode: {mode!r}")

    async def _execute_lucene(self, query: str) -> ReadResult:
        if " | " not in query:
            raise DriverError(
                "Query must be in the format: <index> | <query>\n"
                "Example: orders | status:open AND total:>50"
            )
        index, _, lucene = query.partition(" | ")
        resp = await self._client.search(
            index=index.strip(), q=lucene.strip(), size=_DEFAULT_SEARCH_SIZE
        )
        return self._hits_to_result(resp)

    async def _execute_dev_tools(self, query: str) -> ReadResult:
        _VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}
        lines = query.strip().splitlines()
        tokens = lines[0].strip().split(None, 1)
        if len(tokens) != 2 or tokens[0].upper() not in _VALID_METHODS:
            raise DriverError(
                "Dev Tools query must be in Kibana Dev Tools format:\n"
                "  METHOD /path\n"
                "  {optional body}\n"
                "Example:\n"
                "  GET /orders/_search\n"
                '  {"query": {"match_all": {}}}'
            )
        method, path = tokens[0].upper(), tokens[1].strip()
        body_str = "\n".join(lines[1:]).strip()
        body = json.loads(body_str) if body_str else None
        headers: dict[str, str] = {}
        if body is not None:
            if "_search" in path:
                body.setdefault("size", _DEFAULT_SEARCH_SIZE)
            headers["Content-Type"] = "application/json"
        raw = await self._client.transport.perform_request(
            method, path, body=body, headers=headers
        )
        resp = raw.body if hasattr(raw, "body") else raw
        if isinstance(resp, dict) and "hits" in resp:
            return self._hits_to_result(resp)
        if isinstance(resp, dict):
            return flatten_docs(list(resp.keys()), [[resp[k] for k in resp]])  # ty: ignore[invalid-argument-type]
        return ReadResult(columns=["response"], rows=[[str(resp)]], rows_total=1)

    def _hits_to_result(self, resp: Any) -> ReadResult:
        hits = resp["hits"]["hits"]
        total = resp["hits"]["total"]
        rows_total = total["value"] if isinstance(total, dict) else int(total)
        if not hits:
            return ReadResult(columns=[], rows=[], rows_total=rows_total)
        docs = [{"_id": hit["_id"], **hit.get("_source", {})} for hit in hits]
        columns = list(dict.fromkeys(k for doc in docs for k in doc))
        rows = [[doc.get(col) for col in columns] for doc in docs]
        return flatten_docs(columns, rows, rows_total=rows_total)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                resp = await self._client.cat.indices(
                    format="json", h="index", s="index"
                )
                return [
                    ExploreItem(name=entry["index"], type="index", expandable=True)  # ty: ignore[invalid-argument-type]
                    for entry in resp
                    if not entry["index"].startswith(".")  # ty: ignore[invalid-argument-type]
                ]
            case [_index]:
                return [
                    ExploreItem(name="mappings", type="group", expandable=True),
                    ExploreItem(name="aliases", type="group", expandable=True),
                ]
            case [index, "mappings"]:
                resp = await self._client.indices.get_mapping(index=index)
                props = resp[index]["mappings"].get("properties", {})
                return [
                    ExploreItem(
                        name=field, type=info.get("type", "object"), expandable=False
                    )
                    for field, info in props.items()
                ]
            case [index, "aliases"]:
                resp = await self._client.indices.get_alias(index=index)
                aliases = resp.get(index, {}).get("aliases", {})
                return [
                    ExploreItem(name=alias, type="alias", expandable=False)
                    for alias in aliases
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        match path:
            case [index]:
                resp = await self._client.indices.get_mapping(index=index)
                props = resp[index]["mappings"].get("properties", {})
                return TableDescription(
                    table=index,
                    schema=None,
                    columns=[
                        ColumnInfo(name=field, type=info.get("type", "object"))
                        for field, info in props.items()
                    ],
                )
            case _:
                return None
