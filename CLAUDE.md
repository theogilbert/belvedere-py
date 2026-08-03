# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
# Install dev dependencies (run once)
uv sync --group dev --all-extras

# Run unit + integration tests (default pytest paths: tests/unit, tests/integration)
uv run pytest

# Run a single test file or test
uv run pytest tests/unit/test_dispatcher.py
uv run pytest tests/unit/test_dispatcher.py::test_name

# Lint, format, and type-check
uv run ruff check --fix
uv run ruff format
uv run ty check

# Run all CI checks in one go (includes external tests via Docker)
make ci

# Run only external tests (each script spins up a Docker container)
make external-tests
```

External tests (require a live service) live in `tests/external/`. The scripts in `scripts/` spin up the required service via Docker; `make ci` runs them all automatically.

## Architecture

Grannos is a **JSON-over-stdio server** that lets IDE clients (e.g. grannos.nvim) query databases. The process model is:

```
stdin → Server → Dispatcher → CachingDriver → BaseDriver implementation
                             ↓
                       ConnectionStore (per-connection state + idle timer)
```

**Wire protocol** (`protocol.py`): newline-delimited JSON. Each request `{id, method, params}` gets a response `{id, result, error}`. Progress notifications `{id, progress}` can be emitted before the final response. All types are dataclasses; `encode`/`decode` handle serialisation.

**Server** (`server.py`): reads stdin asynchronously, spawns one `asyncio.Task` per request. A lock serialises stdout writes. The `cancel` method cancels an in-flight task by request id.

**Dispatcher** (`dispatcher.py`): routes methods to handlers. `connect`/`disconnect`/`capabilities`/`driver.help` are connection-free; all others require a `connection_id`. Each connection is protected by a semaphore (`max_concurrency`) and an `IdleTimer` that auto-closes after inactivity. On `ConnectionLostError`, the dispatcher transparently reconnects and retries once.

**Drivers** (`drivers/`): each database backend subclasses `BaseDriver` and declares `LABEL`, `PARAMS`, `LANGUAGES`, `HELP`, and `DEFAULT_IDLE_TIMEOUT` as class attributes. The `drivers/__init__.py` registry lazy-imports drivers so missing optional packages don't break startup — only installed drivers appear in `capabilities`. To add a driver: implement `BaseDriver`, add it to `_REGISTRY`. Complex drivers may be packages (e.g. `drivers/oracle/`, `drivers/postgres/`, each with `driver.py` and `queries.py`) — the registry still imports only the outer package.

**Describe result types** (`protocol.py`): `explore_describe` returns a `DescribeResult` union — `TableDescription`, `IndexDescription`, `IndicesDescription`, `ColumnDescription`, or `ColumnsDescription` (discriminate on the `type` field). `ColumnDescription` uses `data_type` (not `type`) for the SQL data type to avoid collision with the discriminator. `ColumnDescription.exclusive_indices` / `composite_indices` carry full `IndexDescription` objects, not booleans like the lighter `ColumnInfo` inside `TableDescription`.

**Symbol lookup** (`explore_find.py`): the `explore.find` method turns a symbol name, a node type, and a list of `SearchScope` ancestors into `explore.describe` paths — letting a client resolve the symbol under the cursor without searching its own cache. Generic: each driver declares *where* each kind of node lives in its tree as `BaseDriver.FIND_PATHS` path templates (`"*"` = one level of children, any other segment = a literal group name), and `walk_find` expands them through `explore_list`, pruning each wildcard level by the scopes. Scopes sharing a `type` are alternatives, scopes of different types compound. Because the walk runs on `CachingDriver` (not on the wrapped driver), it is served by the explore cache and needs no cache of its own; `MAX_LIST_CALLS` caps the fan-out of an under-scoped search. A driver that can resolve names in one catalog query overrides `explore_find` instead, raising `FindNotSupported` for the node types its query does not cover.

Node types are the `NodeType` enum in `protocol.py` — one flat vocabulary across all drivers, shared by `explore.list`'s `type` field, `explore.find`'s `type` param, and `FIND_PATHS`. Note `ExploreItem.type` is still typed `str`: several drivers overload it on leaf field nodes to report the field's *data* type (`"int4"`, `"varchar2"`) rather than `NodeType.COLUMN`.

**Method results** (`protocol.py`): every dispatcher handler returns a dataclass (`ExploreListResult`, `ExecuteReadResult`, …) unioned as `MethodResult`, not an ad-hoc dict — these *are* the wire shape, since `encode` serialises them field-for-field. They are distinct from the driver-level types (`ReadResult`, `WriteResult`) because a method result may add fields the driver knows nothing about, such as the server-measured `duration_ms`. `session.get` is the sole exception, returning a bare dict whose shape its driver's `SESSION_PARAMS` define.

**Execute messages** (`protocol.py`): `ReadResult`/`WriteResult` — and the `Execute*Result` method results they feed — carry `messages: list[ExecuteMessage]`, out-of-band text a *successful* statement produced (Oracle `DBMS_OUTPUT` lines, PL/SQL compilation errors; Postgres `RAISE NOTICE` and SQL Server `PRINT` would fit the same field). `MessageLevel` is deliberately just `info`/`warning` — a failed request is reported through the response's `error`, so there is no `error` level. Position is structured as `line`/`col` (1-indexed, into the query string as submitted) rather than baked into `text`, so clients never parse it back out. `explore.preview` deliberately does not carry messages: its query is the server's, not the user's.

**Explore cache** (`explore_cache.py`): `CachingDriver` wraps any `BaseDriver` and caches `explore_list` / `explore_describe` results in a per-connection JSON file under `~/.cache/grannos/`. The cache file is keyed by a SHA-256 of the non-sensitive connection params. Passwords and other `secret` params are never written to disk.

**Diagram rendering** (`diagram/`): the `explore.diagram` method (`dispatcher.py:_handle_explore_diagram`) renders a table and every table reachable from it via foreign keys as an ASCII box-and-connector diagram. The pipeline is a straight-line pass through the package: `graph.discover` walks `explore_describe` calls to find all connected tables/edges → `layout.compute_layout` assigns each table a hub-centered column/side via BFS → `place.place` turns that abstract layout into concrete box rectangles with overprovisioned routing channels → `route.route` A*-searches an orthogonal path per edge across a character grid, then `route.compact` strips unused channel space back out → `canvas.Canvas` blits the boxes and edges onto a character grid and renders it to text. Every table/column name drawn is tracked as a `DiagramRegion` (byte-offset span, see `protocol.py`) so a client can map a cursor position in the rendered diagram back to an `explore.describe` path.

## Test layout

| Path | Purpose |
|------|---------|
| `tests/unit/` | Fast, no I/O, mock all drivers |
| `tests/integration/` | Run against embedded/in-process databases (DuckDB, SQLite) |
| `tests/external/` | Require a live external service; not in default `pytest` paths |

The project uses `pytest-asyncio` in `auto` mode — all async test functions are picked up automatically.
