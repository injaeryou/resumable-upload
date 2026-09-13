.PHONY: help install install-pip lint format format-check type-check test test-all test-all-versions ci clean interop check-surfaces verify

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
	@echo "  make interop          - Cross-impl tests (tusd, tus-js-client, tus-py-client)"
	@echo "  make check-surfaces   - Fail if code changed vs BASE (default main) without tests/docs"
	@echo "  make verify           - check-surfaces + ci + interop: the definition of done"
	@echo "  make clean            - Clean build artifacts"
	@echo ""
	@echo "Note: Make sure to activate your virtual environment first:"
	@echo "  source .venv/bin/activate  # or: source venv/bin/activate"

install:
	@command -v uv >/dev/null 2>&1 || { echo "Error: uv is not installed. Install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"; exit 1; }
	uv pip install -e ".[dev,test,all]"

install-pip:
	$(PYTHON) -m pip install -e ".[dev,test,all]"

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

# A change to resumable_upload/ must come with tests and docs (see CLAUDE.md
# "Definition of Done"). Compares the working tree (committed + uncommitted)
# against the merge-base with BASE, so it works on a branch before the PR.
BASE ?= main
check-surfaces:
	@mb=$$(git merge-base $(BASE) HEAD); \
	code=$$(git diff --name-only $$mb -- resumable_upload | grep -c . || true); \
	tests=$$(git diff --name-only $$mb -- tests | grep -c . || true); \
	docs=$$(git diff --name-only $$mb -- docs README.md README.ko.md TUS_COMPLIANCE.md | grep -c . || true); \
	readme=$$(git diff --name-only $$mb -- README.md | grep -c . || true); \
	readme_ko=$$(git diff --name-only $$mb -- README.ko.md | grep -c . || true); \
	fail=0; \
	if [ $$code -gt 0 ] && [ $$tests -eq 0 ]; then echo "✗ resumable_upload/ changed but tests/ did not"; fail=1; fi; \
	if [ $$code -gt 0 ] && [ $$docs -eq 0 ]; then echo "✗ resumable_upload/ changed but docs/ and README did not"; fail=1; fi; \
	if [ $$readme -ne $$readme_ko ]; then echo "✗ README.md and README.ko.md must change together"; fail=1; fi; \
	cli=$$(git diff --name-only $$mb -- resumable_upload/cli.py | grep -c . || true); \
	cli_tests=$$(git diff --name-only $$mb -- tests/test_cli.py | grep -c . || true); \
	cli_docs=$$(git diff --name-only $$mb -- docs/operations/cli.md | grep -c . || true); \
	core=$$(git diff --name-only $$mb -- resumable_upload/server resumable_upload/client | grep -c . || true); \
	if [ $$cli -gt 0 ] && [ $$cli_tests -eq 0 ]; then echo "✗ cli.py changed but tests/test_cli.py did not"; fail=1; fi; \
	if [ $$cli -gt 0 ] && [ $$cli_docs -eq 0 ]; then echo "✗ cli.py changed but docs/operations/cli.md did not"; fail=1; fi; \
	if [ $$core -gt 0 ] && [ $$cli -eq 0 ]; then echo "⚠ server/client changed but cli.py did not: confirm no new option needs a serve/upload flag (TestCLIParity covers constructor kwargs)"; fi; \
	if [ $$fail -eq 1 ]; then echo "   (compared against merge-base with $(BASE); override with BASE=<ref>)"; exit 1; fi; \
	echo "✓ change surfaces vs $(BASE): code=$$code tests=$$tests docs=$$docs"

verify: check-surfaces ci interop
	@echo "✅ verify passed: surfaces, ci, interop"

# Four interop pairings: ours<->ours always; our-client<->tusd, our-server<->
# tus-js-client, our-server<->tus-py-client each need their reference impl.
# Provisions all three reference clients (tusd for the host OS/arch is fetched
# from GitHub releases); anything that fails to provision just skips.
interop:
	@command -v npm >/dev/null 2>&1 && (cd tests/interop && npm install --silent) || echo "npm not found — tus-js-client pairing will skip"
	@command -v uv >/dev/null 2>&1 && uv pip install --quiet tuspy || $(RUN) pip install --quiet tuspy || echo "tuspy install failed — tus-py-client pairing will skip"
	@TUSD_BIN=$$(command -v tusd || sh tests/interop/fetch_tusd.sh 2>/dev/null || true); \
	 [ -n "$$TUSD_BIN" ] && echo "using tusd: $$TUSD_BIN" || echo "tusd unavailable — tusd pairing will skip"; \
	 TUSD_BIN=$$TUSD_BIN $(RUN) pytest tests/test_interop.py -v

clean:
	rm -rf build/ dist/ *.egg-info .coverage htmlcov/ .pytest_cache/ .ruff_cache/
	find . -type d -name __pycache__ -exec rm -r {} + 2>/dev/null || true
	find . -type f -name "*.pyc" -delete
