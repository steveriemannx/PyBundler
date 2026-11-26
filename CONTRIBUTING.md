# Contributing

Thanks for helping. PyBundler is deliberately small and dependency-free; the
best contributions keep it that way.

## Ground rules

* **No runtime dependencies.** Everything in `src/pybundler` must import only the
  standard library. `tests/test_zero_dependencies.py` enforces this. New
  functionality that seems to need a library usually needs a smaller design.
* **No vendored code** unless it is clearly licensed, documented in
  `docs/licensing.md`, and approved in the pull request.
* **Cross-platform mindset.** macOS is exercised end-to-end locally; CI jobs
  cover Linux, Windows (MSVC, x64/x86) and FreeBSD. Windows/FreeBSD code paths must
  at least be reviewed and, where possible, guarded by tests with mocks. If you
  cannot test a platform, say so in the PR.
* **Deterministic output.** Avoid timestamps, random names or hash-order in
  bundle paths and manifests.
* **Explain user-visible behaviour** in `README.md`, `docs/` and `CHANGELOG.md`.

## Development setup

```bash
git clone https://github.com/pybundler/pybundler
cd pybundler
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Python 3.11+ is required. A C compiler enables native launcher tests.

## Running the tests

```bash
# fast suite: no network, no compiler required
PYTHONPATH=src python -m unittest discover -s tests -t tests

# end-to-end builds (downloads/uses a cached runtime, compiles the launcher)
PYBUNDLER_E2E=1 PYTHONPATH=src python -m unittest discover -s tests -t tests -p 'test_e2e.py'

# everything
make test e2e
```

`make lint` runs `compileall` and the zero-dependency test. `make example`
builds and runs `examples/hello`.

Keep tests hermetic: use `TempDirTestCase` from `tests/support.py`, never write
into the repository, and set `PYBUNDLER_CACHE_DIR` if a test touches the cache.

## Code style

* 99-column lines, four-space indentation, `from __future__ import annotations`.
* Dataclasses for data, small functions, explicit types on public APIs.
* Docstrings explain *why* for anything non-obvious.
* Errors use `pybundler.errors` with a `hint=` when the user can fix it.
* Never print from library code; use the `Logger`.

`ruff`/`black` are not required; match the surrounding style.

## Adding a hook

1. Read [docs/hooks.md](docs/hooks.md).
2. Create `src/pybundler/hooks/<package>.py` with `PACKAGES`, optional
   `HIDDEN_IMPORTS`/`DATA`/`BINARIES`, and a small `hook(ctx)` function.
3. Add a unit test in `tests/` that builds a fake package tree and asserts the
   graph/payload additions.
4. Explain the package's runtime behaviour in a comment — future maintainers
   need to know *why* the hook exists.

Prefer `ctx.collect_all()` and package-relative paths over absolute paths.

## Touching the launcher

`src/pybundler/resources/launcher.c` is compiled per application with `-D`
macros. When changing it:

* build the example (`make example`) and run it with `env -i` to verify the
  environment is self-contained,
* test onefile extraction with a cold cache (`PYBUNDLER_CACHE_DIR=$(mktemp -d)`),
* test `multiprocessing` (spawn) and `python app -c "print(1)"` pass-through,
* keep the C99/POSIX and Win32 branches in sync.

## Reporting bugs

Include `pybundler doctor --json`, the failing command with `-v`, the
`dist/<name>.build.json` report, and a minimal reproducer. Security issues go
through the process in [SECURITY.md](SECURITY.md), not the public tracker.

## Pull request checklist

- [ ] tests added/updated; `make test` and relevant e2e pass
- [ ] no new third-party imports (`make lint`)
- [ ] docs and CHANGELOG updated
- [ ] behaviour on macOS/Linux considered; Windows notes included
- [ ] commit messages explain the *why*
