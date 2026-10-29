.PHONY: dev test lint fmt

# A plain editable install with the test extras, plus ruff for make lint/fmt. agent-notes has no sibling
# repo dependency since regista was dropped (Plan 026 D3).
dev:
	uv pip install -e ".[test]" "ruff>=0.5,<0.16"

test:
	uv run pytest

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests
