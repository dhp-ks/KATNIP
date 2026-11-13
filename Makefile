.PHONY: install dev test lint doctor run

install:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e .

dev:
	python3 -m venv .venv
	.venv/bin/pip install --upgrade pip
	.venv/bin/pip install -e '.[dev]'

test:
	.venv/bin/pytest -q

lint:
	.venv/bin/ruff check .

doctor:
	.venv/bin/katnip doctor --config configs/example.yaml

run:
	.venv/bin/katnip run --config configs/example.yaml
