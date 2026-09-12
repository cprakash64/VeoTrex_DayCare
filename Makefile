.PHONY: bootstrap db-up db-down db-test-up db-test-down migrate migrate-test db-test api web edge test lint format typecheck check

COMPOSE := docker compose -f infra/local/compose.yaml

# Every uv invocation below uses --all-packages. "uv run --package <member>" re-synchronises the
# shared workspace virtualenv down to that one member's dependency closure, uninstalling the other
# member's dependencies (NumPy, SciPy, Pillow, edge-agent requirements). Running it before a test
# suite produces mass collection errors that look like database failures.

bootstrap:
	uv sync --all-packages --all-groups --locked
	pnpm install --frozen-lockfile

# --- databases -------------------------------------------------------------------------------
# Development and test are two separate PostgreSQL clusters. Roles are cluster-wide, so the
# destructive database suite must never share a server with development data.

# Development cluster only; the test cluster is behind the "test" profile and cannot start here.
db-up:
	$(COMPOSE) up -d --wait postgres

db-down:
	$(COMPOSE) down

# Disposable test cluster only. Storage is tmpfs, so stopping it discards all test data.
db-test-up:
	$(COMPOSE) --profile test up -d --wait postgres-test

db-test-down:
	$(COMPOSE) --profile test down

# --- migrations ------------------------------------------------------------------------------
# Each target validates its destination first, so "migrate" cannot reach a test database and
# "migrate-test" cannot silently fall back to development settings.

migrate:
	uv run --all-packages python -m veotrex_api.database_targets --require-development
	uv run --all-packages alembic -c apps/api/alembic.ini upgrade head

migrate-test:
	uv run --all-packages python -m veotrex_api.database_targets --require-test
	VEOTREX_DATABASE_URL="$${VEOTREX_TEST_DATABASE_URL:?VEOTREX_TEST_DATABASE_URL must be set}" \
		uv run --all-packages alembic -c apps/api/alembic.ini upgrade head

# Database-backed suite against the validated test cluster.
db-test:
	uv run --all-packages python -m veotrex_api.database_targets --require-test
	uv run --all-packages pytest apps/api/tests

# --- services --------------------------------------------------------------------------------
api:
	uv run --all-packages uvicorn veotrex_api.main:app --reload

web:
	pnpm web:dev

edge:
	uv run --all-packages veotrex-edge-agent

# --- quality ---------------------------------------------------------------------------------
test:
	uv run --all-packages pytest --cov=veotrex_api --cov=veotrex_edge_agent --cov-report=term-missing
	pnpm web:test

lint:
	uv run --all-packages ruff check .
	pnpm web:lint

format:
	uv run --all-packages ruff format .

typecheck:
	uv run --all-packages mypy
	pnpm web:typecheck

check: lint typecheck test
	uv run --all-packages ruff format --check .
