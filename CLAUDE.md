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

Belvedere is a **JSON-over-stdio server** that lets IDE clients (e.g. belvedere.nvim) query databases. The process model is:

```
stdin → Server → Dispatcher → CachingDriver → BaseDriver implementation
                             ↓
                       ConnectionStore (per-connection state + idle timer)
```

**Wire protocol** (`protocol.py`): newline-delimited JSON. Each request `{id, method, params}` gets a response `{id, result, error}`. Progress notifications `{id, progress}` can be emitted before the final response. All types are dataclasses; `encode`/`decode` handle serialisation.

**Server** (`server.py`): reads stdin asynchronously, spawns one `asyncio.Task` per request. A lock serialises stdout writes. The `cancel` method cancels an in-flight task by request id.

**Dispatcher** (`dispatcher.py`): routes methods to handlers. `connect`/`disconnect`/`capabilities`/`driver.help` are connection-free; all others require a `connection_id`. Each connection is protected by a semaphore (`max_concurrency`) and an `IdleTimer` that auto-closes after inactivity. On `ConnectionLostError`, the dispatcher transparently reconnects and retries once.

**Drivers** (`drivers/`): each database backend subclasses `BaseDriver` and declares `LABEL`, `PARAMS`, `LANGUAGES`, `HELP`, and `DEFAULT_IDLE_TIMEOUT` as class attributes. The `drivers/__init__.py` registry lazy-imports drivers so missing optional packages don't break startup — only installed drivers appear in `capabilities`. To add a driver: implement `BaseDriver`, add it to `_REGISTRY`. Complex drivers may be packages (e.g. `drivers/oracle/` with `driver.py` and `queries.py`) — the registry still imports only the outer package.

**Describe result types** (`protocol.py`): `explore_describe` returns a `DescribeResult` union — `TableDescription`, `IndexDescription`, `IndicesDescription`, `ColumnDescription`, or `ColumnsDescription` (discriminate on the `type` field). `ColumnDescription` uses `data_type` (not `type`) for the SQL data type to avoid collision with the discriminator. `ColumnDescription.exclusive_indices` / `composite_indices` carry full `IndexDescription` objects, not booleans like the lighter `ColumnInfo` inside `TableDescription`.

**Explore cache** (`explore_cache.py`): `CachingDriver` wraps any `BaseDriver` and caches `explore_list` / `explore_describe` results in a per-connection JSON file under `~/.cache/belvedere/`. The cache file is keyed by a SHA-256 of the non-sensitive connection params. Passwords and other `secret` params are never written to disk.

## Test layout

| Path | Purpose |
|------|---------|
| `tests/unit/` | Fast, no I/O, mock all drivers |
| `tests/integration/` | Run against embedded/in-process databases (DuckDB, SQLite) |
| `tests/external/` | Require a live external service; not in default `pytest` paths |

The project uses `pytest-asyncio` in `auto` mode — all async test functions are picked up automatically.
