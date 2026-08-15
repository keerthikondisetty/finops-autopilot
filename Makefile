SHELL := /usr/bin/env bash
.SHELLFLAGS := -eu -o pipefail -c
.DEFAULT_GOAL := help
VENV := .venv

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z_.-]+:.*?## ' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

.PHONY: verify
verify: lint test ## Everything CI runs
	@echo ""
	@echo "  All checks passed."

.PHONY: lint
lint: $(VENV)
	@echo ">> ruff"; $(VENV)/bin/python -m ruff check src tests
	@echo ">> mypy"; $(VENV)/bin/python -m mypy src/finops

.PHONY: test
test: $(VENV)
	@echo ">> pytest"; $(VENV)/bin/python -m pytest

$(VENV):
	@uv venv --python 3.12 .venv >/dev/null
	@uv pip install --python .venv/bin/python -e ".[dev]" >/dev/null

.PHONY: clean
clean:
	@rm -rf .pytest_cache .mypy_cache .ruff_cache
	@find . -type d -name '__pycache__' -prune -exec rm -rf {} + 2>/dev/null || true
	@echo clean
