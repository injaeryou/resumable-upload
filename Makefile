.PHONY: help install install-pip lint format format-check type-check test test-all test-all-versions ci clean

# Use Python from activated virtual environment if available, otherwise detect
# Priority: .venv/bin/python > venv/bin/python > VIRTUAL_ENV/bin/python > python3 from PATH
VENV_PYTHON := $(shell if [ -d .venv ]; then echo .venv/bin/python; elif [ -d venv ]; then echo venv/bin/python; fi)
VIRTUAL_ENV_PYTHON := $(if $(VIRTUAL_ENV),$(VIRTUAL_ENV)/bin/python,)
PYTHON := $(or $(VENV_PYTHON),$(VIRTUAL_ENV_PYTHON),$(shell command -v python3 2>/dev/null || command -v python 2>/dev/null || echo python3))
RUN := $(PYTHON) -m

help:
	@echo "Available commands:"
	@echo "  make install          - Install package with all dependencies (using uv, recommended)"
	@echo "  make install-pip      - Install package with all dependencies (using pip)"
	@echo "  make lint             - Run ruff linting"
	@echo "  make format           - Format code with ruff (auto-fix)"
	@echo "  make format-check     - Verify formatting without modifying files"
	@echo "  make type-check       - Run ty type checker on resumable_upload/"
	@echo "  make test-minimal     - Run minimal tests (excluding web frameworks)"
	@echo "  make test             - Run all tests (including web frameworks)"
	@echo "  make test-all-versions - Run tests on all Python versions (requires tox)"
	@echo "  make ci               - Run full CI checks (lint, format-check, type-check, test)"
	@echo "  make clean            - Clean build artifacts"
	@echo ""
	@echo "Note: Make sure to activate your virtual environment first:"
	@echo "  source .venv/bin/activate  # or: source venv/bin/activate"

install:
	@command -v uv >/dev/null 2>&1 || { echo "Error: uv is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	uv pip install -e ".[dev,test]"

install-pip:
	$(PYTHON) -m pip install -e ".[dev,test]"

lint:
	$(RUN) ruff check .

format:
	$(RUN) ruff format .

format-check:
	$(RUN) ruff format --check .

type-check:
	$(RUN) ty check resumable_upload

test-minimal:
	@echo "Running minimal tests... (excluding web frameworks)"
	$(RUN) pytest --cov=resumable_upload --cov-report=term --cov-report=html -k "not test_flask and not test_fastapi and not test_django"

test:
	@echo "Running all tests... (including web frameworks)"
	$(RUN) pytest --cov=resumable_upload --cov-report=term --cov-report=html

test-all-versions:
	@echo "Testing on all Python versions (3.9, 3.10, 3.11, 3.12, 3.13, 3.14)..."
	@echo "This requires tox to be installed: pip install tox"
	$(RUN) tox

ci: lint format-check type-check test
	@echo "✅ All CI checks passed!"

clean:
	rm -rf build/ dist/ *.egg-info .coverage htmlcov/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -r {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
