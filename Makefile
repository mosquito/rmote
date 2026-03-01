.PHONY: docs docs-clean docs-open reformat test tests mypy pytest

docs:
	uv run --group docs sphinx-build -b html docs docs/_build/html -W

docs-clean:
	rm -rf docs/_build

docs-open: docs
	python -m webbrowser file://$(shell pwd)/docs/_build/html/index.html

reformat:
	uv run --group dev ruff format
	uv run --group dev ruff check --fix

mypy:
	uv run --group dev mypy .

pytest:
	uv run --group dev pytest -v --cov=rmote --cov-report=term-missing

test: reformat mypy pytest
tests: test