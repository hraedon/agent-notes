.PHONY: test lint fmt check-suite-lock

test:
	uv run pytest

lint:
	uv run ruff check src tests

fmt:
	uv run ruff format src tests

# Compare the local sibling regista checkout against the SUITE.lock pin
# (Plan 017 WI-4.1). Informational by default; use `make check-suite-lock-strict`
# to fail the build on drift (e.g. as a pre-release gate).
check-suite-lock:
	uv run python scripts/check_suite_lock.py

check-suite-lock-strict:
	uv run python scripts/check_suite_lock.py --strict
