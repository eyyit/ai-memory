.PHONY: check test venv check-push

PYTEST := $(if $(wildcard .venv/bin/pytest),.venv/bin/pytest,PYTHONPATH=src pytest)

venv:
	python3 -m venv .venv
	.venv/bin/python -m pip install --upgrade pip
	.venv/bin/python -m pip install -e '.[test]'

check: test

test:
	$(PYTEST) -q --ignore=tests/test_oauth_snapshot.py

check-push:
	python3 scripts/check-push.py --ci
