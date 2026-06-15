"""Elasticsearch driver — requires: pip install elasticsearch"""

import asyncio
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from ..protocol import (
    ColumnInfo,
    DMLResult,
    ExploreItem,
    SelectResult,
    TableDescription,
    DriverParam,
)
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError

if TYPE_CHECKING:
    import elasticsearch

T = TypeVar("T")

_DEFAULT_SEARCH_SIZE = 1000


class ElasticsearchDriver(BaseDriver):
    """Elasticsearch driver backed by the official elasticsearch-py client.

    Args:
        params: Connect request fields (``host``, ``port``, ``username``, ``password``,
            ``query_mode``).
        client: Open Elasticsearch client. Use :meth:`create` instead of constructing directly.
    """

    PARAMS: list[DriverParam] = [
        DriverParam(key="host", type="string", label="Host", default="localhost"),
        DriverParam(key="port", type="integer", label="Port", default=9200),
        DriverParam(key="username", type="string", label="Username"),
        DriverParam(key="password", type="string", label="Password", secret=True),
        DriverParam(
            key="query_mode",
            type="enum",
            label="Query Mode",
            choices=["lucene", "dsl"],
            default="lucene",
        ),
    ]

    HELP: str = """\
## Elasticsearch

**Install:** `pip install elasticsearch`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `host` | no | `localhost` | Server hostname or IP |
| `port` | no | `9200` | HTTP port |
| `username` | no | — | Username |
| `password` | no | — | Password (masked) |
| `query_mode` | no | `lucene` | Query language: `lucene` or `dsl` |

**Queries:** Prefix with the target index name (pattern or alias) and ` | `.

*Lucene mode:*

```
orders | status:open AND total:>50
```

```
orders | *
```

*DSL mode — Kibana Dev Tools syntax:*

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

    def __init__(self, params: dict[str, Any], client: "elasticsearch.Elasticsearch") -> None:
        super().__init__(params)
        self._client = client

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "ElasticsearchDriver":
        try:
            import elasticsearch  # noqa: F401
        except ImportError:
            raise RuntimeError("elasticsearch not installed — run: pip install elasticsearch")
        client = await asyncio.get_running_loop().run_in_executor(None, lambda: cls._open(params))
        return cls(params, client)

    @staticmethod
    def _open(params: dict[str, Any]) -> "elasticsearch.Elasticsearch":
        import elasticsearch

        host = params.get("host", "localhost")
        port = int(params.get("port", 9200))
        kwargs: dict[str, Any] = {"hosts": [f"http://{host}:{port}"]}
        username = params.get("username")
        password = params.get("password")
        if username and password:
            kwargs["basic_auth"] = (username, password)
        return elasticsearch.Elasticsearch(**kwargs)

    async def reconnect(self) -> None:
        self._client = await asyncio.get_running_loop().run_in_executor(
            None, lambda: self._open(self.params)
        )

    async def disconnect(self) -> None:
        await asyncio.get_running_loop().run_in_executor(None, self._client.close)

    async def execute(self, query: str, binds: list[Any]) -> SelectResult | DMLResult:
        try:
            return await self._run(self._execute_sync, query)
        except Exception as exc:
            import elasticsearch
            if isinstance(exc, elasticsearch.ConnectionError):
                raise ConnectionLostError(str(exc)) from exc
            raise

    def _execute_sync(self, query: str) -> SelectResult:
        mode = self.params.get("query_mode", "lucene")
        if mode == "lucene":
            return self._execute_lucene_sync(query)
        elif mode == "dsl":
            return self._execute_dsl_sync(query)
        else:
            raise ValueError(f"Unknown query_mode: {mode!r}")

    def _execute_lucene_sync(self, query: str) -> SelectResult:
        if " | " not in query:
            raise ValueError(
                "Query must be in the format: <index> | <query>\n"
                "Example: orders | status:open AND total:>50"
            )
        index, _, lucene = query.partition(" | ")
        resp = self._client.search(index=index.strip(), q=lucene.strip(), size=_DEFAULT_SEARCH_SIZE)
        return self._hits_to_result(resp)

    def _execute_dsl_sync(self, query: str) -> SelectResult:
        _VALID_METHODS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"}
        lines = query.strip().splitlines()
        tokens = lines[0].strip().split(None, 1)
        if len(tokens) != 2 or tokens[0].upper() not in _VALID_METHODS:
            raise ValueError(
                "DSL query must be in Kibana Dev Tools format:\n"
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
        raw = self._client.transport.perform_request(method, path, body=body, headers=headers)
        resp = raw.body if hasattr(raw, "body") else raw
        if isinstance(resp, dict) and "hits" in resp:
            return self._hits_to_result(resp)
        if isinstance(resp, dict):
            return flatten_docs(list(resp.keys()), [[resp[k] for k in resp]])
        return SelectResult(columns=["response"], rows=[[str(resp)]])

    def _hits_to_result(self, resp: Any) -> SelectResult:
        hits = resp["hits"]["hits"]
        if not hits:
            return SelectResult(columns=[], rows=[])
        docs = [{"_id": hit["_id"], **hit.get("_source", {})} for hit in hits]
        columns = list(dict.fromkeys(k for doc in docs for k in doc))
        rows = [[doc.get(col) for col in columns] for doc in docs]
        return flatten_docs(columns, rows)

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        return await self._run(self._explore_list_sync, path)

    def _explore_list_sync(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                resp = self._client.cat.indices(format="json", h="index", s="index")
                return [
                    ExploreItem(name=entry["index"], type="index", expandable=True)
                    for entry in resp
                    if not entry["index"].startswith(".")
                ]
            case [_index]:
                return [
                    ExploreItem(name="mappings", type="group", expandable=True),
                    ExploreItem(name="aliases", type="group", expandable=True),
                ]
            case [index, "mappings"]:
                resp = self._client.indices.get_mapping(index=index)
                props = resp[index]["mappings"].get("properties", {})
                return [
                    ExploreItem(name=field, type=info.get("type", "object"), expandable=False)
                    for field, info in props.items()
                ]
            case [index, "aliases"]:
                resp = self._client.indices.get_alias(index=index)
                aliases = resp.get(index, {}).get("aliases", {})
                return [
                    ExploreItem(name=alias, type="alias", expandable=False)
                    for alias in aliases
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return await self._run(self._explore_describe_sync, path)

    def _explore_describe_sync(self, path: list[str]) -> TableDescription | None:
        match path:
            case [index]:
                resp = self._client.indices.get_mapping(index=index)
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

    async def _run(self, fn: Callable[..., T], *args: Any) -> T:
        return await asyncio.get_running_loop().run_in_executor(None, lambda: fn(*args))
