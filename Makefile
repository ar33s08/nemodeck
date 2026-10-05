SHELL := /bin/bash
PY := .venv/bin/python

.PHONY: venv demo serve test selftest

venv:
	uv venv --python 3.12 .venv
	uv pip install --python $(PY) -e ".[dev]"

demo:  ## run against the built-in simulator (no NemoClaw needed)
	$(PY) -m nemodeck serve --adapter demo

serve:  ## run against a real NemoClaw install on this host
	$(PY) -m nemodeck serve --adapter live

test:
	$(PY) -m pytest -q

selftest:  ## adapter probe against the local host
	$(PY) -m nemodeck selftest