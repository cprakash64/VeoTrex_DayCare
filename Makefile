.PHONY: bootstrap db-up db-down migrate api web edge test lint format typecheck check

bootstrap:
	uv sync --all-packages --all-groups --locked
	pnpm install --frozen-lockfile

db-up:
	docker compose -f infra/local/compose.yaml up -d --wait

db-down:
	docker compose -f infra/local/compose.yaml down

migrate:
	uv run --package veotrex-api alembic -c apps/api/alembic.ini upgrade head

api:
	uv run --package veotrex-api uvicorn veotrex_api.main:app --reload

web:
	pnpm web:dev

edge:
	uv run --package veotrex-edge-agent veotrex-edge-agent

test:
	uv run pytest --cov=veotrex_api --cov=veotrex_edge_agent --cov-report=term-missing
	pnpm web:test

lint:
	uv run ruff check .
	pnpm web:lint

format:
	uv run ruff format .

typecheck:
	uv run mypy
	pnpm web:typecheck

check: lint typecheck test
	uv run ruff format --check .
