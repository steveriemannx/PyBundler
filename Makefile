PY ?= python3
PYTHONPATH := src
export PYTHONPATH

.PHONY: help test e2e lint example build wheel clean doctor

help:
	@echo "targets: test e2e lint example build wheel doctor clean"

test:
	$(PY) -m unittest discover -s tests -t tests

e2e:
	PYBUNDLER_E2E=1 $(PY) -m unittest discover -s tests -t tests -p 'test_e2e.py'

lint:
	$(PY) -m compileall -q -j 0 src tests examples tools
	$(PY) -m unittest discover -s tests -t tests -p 'test_zero_dependencies.py'
	$(PY) tools/check_licenses.py

example:
	$(PY) -m pybundler build examples/hello/pybundler.toml --verify
	./examples/hello/dist/hello/hello --name make

build:
	$(PY) -m pybundler build

wheel:
	$(PY) -m pip wheel --no-deps --wheel-dir dist-wheels .

doctor:
	$(PY) -m pybundler doctor

clean:
	rm -rf build dist dist-wheels .pytest_cache
	rm -rf src/*.egg-info
	rm -rf examples/hello/build examples/hello/dist
	find . -name '__pycache__' -type d -prune -exec rm -rf {} +
