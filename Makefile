UV ?= uv

.PHONY: install run format format-check lint typecheck test check migrate container-smoke compose-up compose-down

install:
	$(UV) sync --python 3.12

run:
	$(UV) run uvicorn ninjatech_deployment_lab.main:app --reload

format:
	$(UV) run ruff format .

format-check:
	$(UV) run ruff format --check .

lint:
	$(UV) run ruff check .

typecheck:
	$(UV) run mypy src tests

test:
	$(UV) run pytest

check: format-check lint typecheck test

migrate:
	$(UV) run alembic upgrade head

container-smoke:
	bash scripts/container-smoke.sh

compose-up:
	docker compose up --build

compose-down:
	docker compose down
