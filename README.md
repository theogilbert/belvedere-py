# dbelveder-py

JSON-over-stdio server backend for [dbelveder.nvim](../dbelveder.nvim). The Neovim plugin spawns this process and communicates through its stdin/stdout pipes using newline-delimited JSON.

## Requirements

- Python 3.14+
- [uv](https://github.com/astral-sh/uv) (recommended)

## Installation

```bash
pip install dbelveder-py
# or with SQL Server support:
pip install "dbelveder-py[mssql]"
```

## Usage

The server is started by the Neovim plugin automatically. To run it manually:

```bash
dbelveder [--log] [--max-concurrency N]
```

| Flag | Default | Description |
|------|---------|-------------|
| `--log` | off | Log all requests and responses to `~/.local/state/dbelveder/server.log` |
| `--max-concurrency N` | 5 | Max concurrent requests per connection |

## Supported drivers

| Driver name | Dependency | Notes |
|-------------|------------|-------|
| `sqlite` | stdlib | In-memory and file databases |
| `sqlserver` | `mssql-python` | Requires `pip install "dbelveder-py[mssql]"` |

## Protocol

Communication uses newline-delimited JSON (one message per line). See [docs/protocol.md](../dbelveder.nvim/docs/protocol.md) for the full specification.

### Methods

- **`connect`** — open a database connection, returns a `connection_id`
- **`disconnect`** — close a connection
- **`execute`** — run a SQL statement
  - SELECT → `{"columns": [...], "rows": [...]}`
  - INSERT/UPDATE/DELETE → `{"rows_affected": N}`
- **`explore.list`** — list child nodes in the database object tree (schemas, tables, columns, …)
- **`explore.describe`** — return column metadata for a table

Long-running operations emit progress notifications before the final response.

## Caching

Explore results (`explore.list` and `explore.describe`) are cached per connection to avoid redundant queries. The cache is persisted to disk at `~/.cache/dbelveder/` and reloaded on reconnect. Pass `reset_cache: true` in an explore request to invalidate it.

Passwords are never written to the cache file.

## Development

```bash
uv sync --group dev
uv run pytest
```
