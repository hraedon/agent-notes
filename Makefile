.PHONY: dev test lint fmt check-suite-lock check-suite-lock-strict

# Develop-against-lock (Plan 019 B2): install regista at the released version
# pinned in SUITE.lock (the single source of truth for what to develop against),
# so dev and CI compose against the artifact the suite ships. Override the
# substrate deliberately with DEV_AGAINST=main|<ref>|sibling (see
# docs/develop-against-lock.md). Same install shape CI uses.
dev:
	python scripts/dev-install.py

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
