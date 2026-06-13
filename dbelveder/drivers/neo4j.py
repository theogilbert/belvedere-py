"""Neo4j driver — requires: pip install neo4j"""

from typing import TYPE_CHECKING, Any

from ..protocol import DMLResult, ExploreItem, SelectResult, TableDescription, DriverParam
from ..tabular import flatten_docs
from .base import BaseDriver, ConnectionLostError

if TYPE_CHECKING:
    import neo4j


def _serialize(value: Any) -> Any:
    """Recursively convert neo4j graph objects to plain Python values."""
    try:
        from neo4j.graph import Node, Relationship, Path
        if isinstance(value, Node):
            return {"_labels": sorted(value.labels), **dict(value)}
        if isinstance(value, Relationship):
            return {"_type": value.type, **dict(value)}
        if isinstance(value, Path):
            return [_serialize(n) for n in value.nodes]
    except ImportError:
        pass
    if isinstance(value, list):
        return [_serialize(v) for v in value]
    if isinstance(value, dict):
        return {k: _serialize(v) for k, v in value.items()}
    return value


class Neo4jDriver(BaseDriver):
    """Neo4j driver backed by the official neo4j async Python client.

    Args:
        params: Connect request fields (``uri``, ``user``, ``password``, ``database``).
        driver: Open AsyncDriver instance. Use :meth:`create` instead of constructing directly.
    """

    PARAMS: list[DriverParam] = [
        DriverParam(key="uri", type="string", label="Bolt URI", default="bolt://localhost:7687"),
        DriverParam(key="user", type="string", label="User", default="neo4j"),
        DriverParam(key="password", type="string", label="Password", secret=True),
        DriverParam(key="database", type="string", label="Database", default="neo4j"),
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

`explore.describe` always returns `None` (no fixed schema).
"""

    def __init__(self, params: dict[str, Any], driver: "neo4j.AsyncDriver") -> None:
        super().__init__(params)
        self._driver = driver

    @classmethod
    async def create(cls, params: dict[str, Any]) -> "Neo4jDriver":
        try:
            import neo4j as _neo4j
        except ImportError:
            raise RuntimeError("neo4j not installed — run: pip install neo4j")
        driver = _neo4j.AsyncGraphDatabase.driver(
            params.get("uri", "bolt://localhost:7687"),
            auth=(params.get("user", "neo4j"), params.get("password", "")),
        )
        await driver.verify_connectivity()
        return cls(params, driver)

    async def reconnect(self) -> None:
        await self._driver.close()
        import neo4j as _neo4j
        self._driver = _neo4j.AsyncGraphDatabase.driver(
            self.params.get("uri", "bolt://localhost:7687"),
            auth=(self.params.get("user", "neo4j"), self.params.get("password", "")),
        )
        await self._driver.verify_connectivity()

    async def disconnect(self) -> None:
        await self._driver.close()

    async def execute(self, sql: str, binds: list[Any]) -> SelectResult | DMLResult:
        """Run a Cypher statement. Positional bind values map to ``$0``, ``$1``, …

        Args:
            sql: Cypher statement to execute.
            binds: Positional bind parameters (referenced as ``$0``, ``$1``, … in the query).

        Returns:
            SelectResult for queries that RETURN rows, DMLResult otherwise.

        Raises:
            ConnectionLostError: If the connection was lost during execution.
        """
        params = {str(i): v for i, v in enumerate(binds)}
        db = self.params.get("database", "neo4j")
        try:
            async with self._driver.session(database=db) as session:
                result = await session.run(sql, params)
                keys = result.keys()
                if keys:
                    rows = []
                    async for record in result:
                        rows.append([_serialize(record[k]) for k in keys])
                    return flatten_docs(list(keys), rows)
                summary = await result.consume()
                c = summary.counters
                affected = (
                    c.nodes_created + c.nodes_deleted
                    + c.relationships_created + c.relationships_deleted
                    + c.properties_set
                )
                return DMLResult(rows_affected=affected)
        except Exception as exc:
            try:
                import neo4j.exceptions as _exc
                if isinstance(exc, (_exc.ServiceUnavailable, _exc.SessionExpired)):
                    raise ConnectionLostError(str(exc)) from exc
            except ImportError:
                pass
            raise

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
                return [ExploreItem(name=n, type="index", expandable=False) for n in names]
            case ["entities"]:
                labels = await self._query_column(
                    "CALL db.labels() YIELD label RETURN label ORDER BY label", "label"
                )
                return [ExploreItem(name=l, type="label", expandable=True) for l in labels]
            case ["relationships"]:
                types = await self._query_column(
                    "CALL db.relationshipTypes() YIELD relationshipType"
                    " RETURN relationshipType ORDER BY relationshipType",
                    "relationshipType",
                )
                return [ExploreItem(name=t, type="relationship_type", expandable=True) for t in types]
            case ["entities", label]:
                props = await self._node_properties(label)
                return [ExploreItem(name=p, type="property", expandable=False) for p in props]
            case ["relationships", rel_type]:
                props = await self._relationship_properties(rel_type)
                return [ExploreItem(name=p, type="property", expandable=False) for p in props]
            case _:
                return []

    async def explore_describe(self, path: list[str]) -> TableDescription | None:
        return None

    async def _query_column(self, query: str, key: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(query)
            return [r[key] for r in await result.data()]

    async def _node_properties(self, label: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(
                f"MATCH (n:`{label}`) UNWIND keys(n) AS prop"
                " RETURN DISTINCT prop ORDER BY prop"
            )
            return [r["prop"] for r in await result.data()]

    async def _relationship_properties(self, rel_type: str) -> list[str]:
        db = self.params.get("database", "neo4j")
        async with self._driver.session(database=db) as session:
            result = await session.run(
                f"MATCH ()-[r:`{rel_type}`]->() UNWIND keys(r) AS prop"
                " RETURN DISTINCT prop ORDER BY prop"
            )
            return [r["prop"] for r in await result.data()]
