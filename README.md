# grannos-py

[![PyPI](https://img.shields.io/pypi/v/grannos-py.svg)](https://pypi.org/project/grannos-py/)
[![Python versions](https://img.shields.io/pypi/pyversions/grannos-py.svg)](https://pypi.org/project/grannos-py/)

JSON-over-stdio server backend to query databases.

This server can be used by compatible clients (e.g. [grannos.nvim](https://github.com/theogilbert/grannos.nvim)) to explore and query databases in an IDE.

## Requirements

- Python 3.12+

## Installation

`grannos` is a command-line server that the editor client spawns, so install it as a
tool — that puts the `grannos` executable on your `PATH` in its own isolated environment:

```bash
uv tool install grannos-py[all]
```

Database drivers are opt-in [extras](#supported-drivers) — install the ones you need:

```bash
uv tool install "grannos-py[postgres]"          # a single driver
uv tool install "grannos-py[postgres,oracle]"   # several
uv tool install "grannos-py[all]"               # every driver
```

To add a driver to an existing install, re-run the command with the extra included.

Plain `pip install grannos-py` works too if you would rather manage the environment
yourself — just make sure the resulting `grannos` executable is on the `PATH` your
editor sees.

## Usage

The server is started by the Neovim plugin automatically. To run it manually:

```bash
grannos [--log] [-v] [--max-concurrency N] [--max-request-bytes N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log` | off | Log all requests and responses to `~/.local/state/grannos/server.log` |
| `-v` | off | Log at DEBUG level (requires `--log`). Adds one line per query any driver sends to the database — the statements you run *and* the catalog queries the tree, describe and find views generate. Bind values are logged for the driver's own catalog queries (schema and object names), never for your statements. |
| `--max-concurrency N` | 5 | Max concurrent requests per connection |
| `--max-request-bytes N` | 16777216 (16 MiB) | Largest single request accepted. A longer one — a very long query, say — is answered with an error instead of being buffered. |

## Supported drivers

| Driver | Extra | Python package |
|--------|-------|----------------|
| `sqlite` | — (always available) | stdlib |
| `duckdb` | `duckdb` | `duckdb` |
| `postgres` | `postgres` | `psycopg[binary]` |
| `sqlserver` | `mssql` | `mssql-python` |
| `oracle` | `oracle` | `oracledb` |
| `neo4j` | `neo4j` | `neo4j` |
| `mongodb` | `mongodb` | `pymongo` |
| `elasticsearch` | `elasticsearch` | `elasticsearch`, `aiohttp` |
| `prometheus` | `prometheus` | `aiohttp` |
| `s3` | `s3` | `boto3`, `pyyaml` |

Only drivers whose package is installed are advertised via `capabilities`. See [docs/drivers.md](https://github.com/theogilbert/grannos-py/blob/main/docs/drivers.md) for connection parameters, query syntax, and explore tree structure for each driver.

## Protocol

Communication uses newline-delimited JSON (one message per line). See [docs/protocol.md](https://github.com/theogilbert/grannos.nvim/blob/main/docs/protocol.md) for the full specification.

### Methods

- **`capabilities`** — list available drivers and their connection parameters
- **`driver.help`** — return Markdown help text for a named driver
- **`connect`** — open a database connection, returns a `connection_id`
- **`disconnect`** — close a connection
- **`execute`** — run a query against the connected database
  - Query with results → `{"columns": [...], "rows": [...], "rows_total": N}` (`rows_total` is the total number of matching rows; may exceed `len(rows)` when the driver applies a default fetch limit)
  - Write operation → `{"rows_affected": N}`

  Query syntax depends on the driver. Examples:

  ```sql
  -- SQLite / SQL Server / Oracle
  SELECT name, age FROM users WHERE active = 1
  ```

  ```cypher
  // Neo4j (Cypher)
  MATCH (u:User)-[:BOUGHT]->(p:Product) RETURN u.name, p.title
  ```

  ```
  // Elasticsearch (Lucene)
  orders | status:open AND total:>50
  ```

  ```json
  // MongoDB
  {"find": "orders", "filter": {"status": "open"}, "limit": 100}
  ```

  ```
  // Prometheus (PromQL)
  rate(http_requests_total[5m])
  ```

- **`cancel`** — cancel an in-flight request by its id
- **`explore.list`** — list child nodes in the database object tree (schemas, tables, columns, …)
- **`explore.describe`** — describe a node: column metadata for a table, index details, and so on
- **`explore.find`** — resolve a symbol name and node type to `explore.describe` paths, without the client searching its own cache
- **`explore.preview`** — return a small sample of rows for a node in the tree
- **`explore.diagram`** — render a table and everything reachable from it by foreign key as an ASCII diagram, with regions mapping cursor positions back to `explore.describe` paths
- **`explore.download`** — download the object at a path (or at an opaque `ref`) to a local file
- **`session.set`** / **`session.get`** — set and read driver-specific session values

Long-running operations emit progress notifications before the final response.

## Server behaviour

**Idle timeout:** a connection is auto-closed after `idle_timeout` seconds of inactivity. Pass `"idle_timeout": <seconds>` in `connect.params` (default: `600`).

**Explore cache:** `explore.list` and `explore.describe` results are cached per connection and persisted to `~/.cache/grannos/`. Pass `"reset_cache": true` in any explore request to invalidate the cache for that connection. Passwords are never written to the cache.

## Development

Requires [uv](https://github.com/astral-sh/uv).

```bash
uv sync --group dev --all-extras
uv run pytest
```
