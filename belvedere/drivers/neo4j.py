"""Neo4j driver — requires: pip install neo4j"""

from typing import Any, LiteralString

import neo4j
import neo4j.exceptions

from ..protocol import (
    DriverParam,
    ExploreItem,
    IndexDescription,
    IndexKeyField,
    ParamType,
    ReadResult,
    WriteResult,
)
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError, DriverError


class Neo4jDriver(BaseDriver):
    """Neo4j driver backed by the official neo4j async Python client.

    Args:
        params: Connect request fields (``uri``, ``user``, ``password``, ``database``).
        driver: Open AsyncDriver instance. Use :meth:`create` instead of constructing directly.
    """

    LABEL = "Neo4j"

    PARAMS: list[DriverParam] = [
        DriverParam(
            key="uri",
            type=ParamType.STRING,
            label="Bolt URI",
            default="bolt://localhost:7687",
        ),
        DriverParam(key="user", type=ParamType.STRING, label="User", default="neo4j"),
        DriverParam(
            key="password", type=ParamType.STRING, label="Password", secret=True
        ),
        DriverParam(
            key="database", type=ParamType.STRING, label="Database", default="neo4j"
        ),
    ]

    HELP: str = """\
## Neo4j

**Install:** `pip install neo4j`

| Parameter | Required | Default | Description |
|-----------|----------|---------|-------------|
| `uri` | no | `bolt://localhost:7687` | Bolt URI |
| `user` | no | `neo4j` | Username |
| `password` | no | — | Password (masked) |
| `database` | no | `neo4j` | Database name |

**Queries:** Cypher. Positional bind parameters are referenced as `$0`, `$1`, …

```cypher
MATCH (u:User {name: $0})-[:BOUGHT]->(p:Product) RETURN u, p
```

Results are serialized and flattened: nodes expand to `col._labels`, `col.prop`,
…; relationships expand to `col._type`, `col.prop`, …

**Explore tree:**

```
(root)
├── entities       → <label>  → property names (sampled from existing nodes)
├── relationships  → <type>   → property names (sampled from existing relationships)
└── indexes        → index name
```

`explore.describe` is supported on `["indexes", index_name]` paths and returns an
`IndexDescription` with the indexed properties (direction = index type, e.g. `RANGE`,
`TEXT`, `POINT`) and `unique`.
"""

    def __init__(self, params: dict[str, Any], driver: neo4j.AsyncDriver) -> None:
        super().__init__(params)
        self._driver = driver

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "Neo4jDriver":
        return cls(params, await _make_neo4j_driver(params))

    async def reconnect(self) -> None:
        await self._driver.close()
        self._driver = await _make_neo4j_driver(self.params)

    async def disconnect(self) -> None:
        await self._driver.close()

    async def execute(self, query: str, binds: list[Any]) -> ReadResult | WriteResult:
        """Run a Cypher statement. Positional bind values map to ``$0``, ``$1``, …

        Args:
            query: Cypher statement to execute.
            binds: Positional bind parameters (referenced as ``$0``, ``$1``, … in the query).

        Returns:
            ReadResult for queries that RETURN rows, DMLResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        params = {str(i): v for i, v in enumerate(binds)}
        db = self.params.get("database", "neo4j")
        try:
            async with self._driver.session(database=db) as session:
                result = await session.run(query, params)  # ty: ignore[invalid-argument-type]
                keys = result.keys()
                if keys:
                    rows = []
                    async for record in result:
                        rows.append([_serialize(record[k]) for k in keys])
                    return flatten_docs(list(keys), rows)
                summary = await result.consume()
                c = summary.counters
                affected = (
                    c.nodes_created
                    + c.nodes_deleted
                    + c.relationships_created
                    + c.relationships_deleted
                    + c.properties_set
                )
                return WriteResult(rows_affected=affected)
        except Exception as exc:
            if isinstance(
                exc,
                (neo4j.exceptions.ServiceUnavailable, neo4j.exceptions.SessionExpired),
            ):
                raise ConnectionLostError(str(exc)) from exc
            raise DriverError(str(exc)) from exc

    async def explore_list(self, path: list[str]) -> list[ExploreItem]:
        match path:
            case []:
                return [
                    ExploreItem(name="entities", type="group", expandable=True),
                    ExploreItem(name="relationships", type="group", expandable=True),
                    ExploreItem(name="indexes", type="group", expandable=True),
                ]
            case ["indexes"]:
                names = await self._query_column(
                    "SHOW INDEXES YIELD name RETURN name ORDER BY name", "name"
                )
                return [
                    ExploreItem(name=n, type="index", expandable=False) for n in names
                ]
            case ["entities"]:
                labels = await self._query_column(
                    "CALL db.labels() YIELD label RETURN label ORDER BY label", "label"
                )
                return [
                    ExploreItem(name=label, type="label", expandable=True)
                    for label in labels
                ]
            case ["relationships"]:
                types = await self._query_column(
                    "CALL db.relationshipTypes() YIELD relationshipType"
                    " RETURN relationshipType ORDER BY relationshipType",
                    "relationshipType",
                )
                return [
                    ExploreItem(name=t, type="relationship_type", expandable=True)
                    for t in types
                ]
            case ["entities", label]:
                props = await self._node_properties(label)
                return [
                    ExploreItem(name=p, type="property", expandable=False)
                    for p in props
                ]
            case ["relationships", rel_type]:
                props = await self._relationship_properties(rel_type)
                return [
                    ExploreItem(name=p, type="property", expandable=False)
                    for p in props
                ]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> IndexDescription | None:
        match path:
            case ["indexes", index_name]:
                spec = await self._index_info(index_name)
                if spec is None:
                    return None
                labels_or_types: list[str] = spec["labelsOrTypes"] or []
                return IndexDescription(
                    index=index_name,
                    fields=[
                        IndexKeyField(name=prop, direction=spec["type"])
                        for prop in (spec["properties"] or [])
                    ],
                    unique=spec["owningConstraint"] is not None,
                    entity=", ".join(labels_or_types) if labels_or_types else None,
                )
            case _:
                return None

    async def _index_info(self, index_name: str) -> dict | None:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(
                "SHOW INDEXES YIELD name, type, properties, labelsOrTypes, owningConstraint "
                "RETURN name, type, properties, labelsOrTypes, owningConstraint"
            )
            rows = await result.data()
        return next((r for r in rows if r["name"] == index_name), None)

    async def _query_column(self, query: LiteralString, key: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(query)
            return [r[key] for r in await result.data()]

    async def _node_properties(self, label: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(
                f"MATCH (n:`{label}`) UNWIND keys(n) AS prop"  # ty: ignore[invalid-argument-type]
                " RETURN DISTINCT prop ORDER BY prop"
            )
            return [r["prop"] for r in await result.data()]

    async def _relationship_properties(self, rel_type: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(
                f"MATCH ()-[r:`{rel_type}`]->() UNWIND keys(r) AS prop"  # ty: ignore[invalid-argument-type]
                " RETURN DISTINCT prop ORDER BY prop"
            )
            return [r["prop"] for r in await result.data()]


def _serialize(value: Any) -> Any:
    """Recursively convert neo4j graph objects to plain Python values."""
    if isinstance(value, neo4j.graph.Node):
        return {"_labels": sorted(value.labels), **dict(value)}
    if isinstance(value, neo4j.graph.Relationship):
        return {"_type": value.type, **dict(value)}
    if isinstance(value, neo4j.graph.Path):
        return [_serialize(n) for n in value.nodes]
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


async def _make_neo4j_driver(params: dict[str, Any]) -> neo4j.AsyncDriver:
    auth = (params.get("user", "neo4j"), params.get("password", ""))
    driver = neo4j.AsyncGraphDatabase.driver(params["uri"], auth=auth)
    try:
        await driver.verify_connectivity()
    except Exception as exc:
        await driver.close()
        raise DriverError(str(exc)) from exc
    return driver
