.PHONY: test lint up logs

test:
	uv run pytest

lint:
	uv run ruff check .
	uv run ruff format --check .

up:
	docker compose up -d --build

logs:
	docker compose logs -f --tail=200
