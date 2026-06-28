.PHONY: ci external-tests

ci: external-tests
	uv run ruff check --fix
	uv run ruff format
	uv run ty check
	uv run pytest

external-tests:
	scripts/run-elasticsearch-tests.sh
	scripts/run-mongodb-tests.sh
	scripts/run-neo4j-tests.sh
	scripts/run-oracle-tests.sh
	scripts/run-sqlserver-tests.sh
