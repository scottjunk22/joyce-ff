# Convenience targets. On Windows without `make`, use .\run.ps1 instead.
PY := .venv/Scripts/python.exe

.PHONY: help test initdb validate sync run venv

help:
	@echo "Targets: venv, test, initdb, validate, sync, run"

venv:
	python -m venv .venv
	$(PY) -m pip install --upgrade pip
	$(PY) -m pip install -r requirements.txt

test:
	$(PY) -m pytest

initdb:
	$(PY) manage.py initdb

validate:
	$(PY) manage.py validate

sync:
	$(PY) manage.py sync

run:
	$(PY) manage.py run
