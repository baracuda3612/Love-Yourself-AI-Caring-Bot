BREW_PYTHON := $(shell if command -v brew >/dev/null 2>&1; then candidate="$$(brew --prefix python@3.12 2>/dev/null)/bin/python3.12"; if [ -x "$$candidate" ]; then printf '%s' "$$candidate"; fi; fi)
PYTHON ?= $(if $(BREW_PYTHON),$(BREW_PYTHON),python3.12)
VENV := .venv
PYTHON_VERSION := 3.12.14
PIP_VERSION := 26.2.1
PIP_TOOLS_VERSION := 7.6.1
TARGET_TESTS ?= tests/test_plan_parser.py tests/test_plan_builder_v5.py

.PHONY: bootstrap lock services-up services-down test-collect test-targeted test audit

bootstrap:
	@actual="$$($(PYTHON) -c 'import platform; print(platform.python_version())')"; \
	if [ "$$actual" != "$(PYTHON_VERSION)" ]; then \
		echo "Python $(PYTHON_VERSION) is required; found $$actual"; exit 1; \
	fi
	$(PYTHON) -m venv --clear $(VENV)
	$(VENV)/bin/python -m pip install --upgrade pip==$(PIP_VERSION) pip-tools==$(PIP_TOOLS_VERSION)
	$(VENV)/bin/pip-sync requirements-dev.txt

lock:
	$(VENV)/bin/pip-compile --strip-extras --resolver=backtracking --output-file=requirements.txt requirements.in
	$(VENV)/bin/pip-compile --strip-extras --resolver=backtracking --output-file=requirements-dev.txt requirements-dev.in

services-up:
	docker compose -f compose.test.yml up -d --wait

services-down:
	docker compose -f compose.test.yml down -v

test-collect:
	$(VENV)/bin/python -m pytest --collect-only -q

test-targeted:
	$(VENV)/bin/python -m pytest -q $(TARGET_TESTS)

test:
	$(VENV)/bin/python -m pytest -q

audit:
	$(VENV)/bin/pip-audit --local --progress-spinner off
