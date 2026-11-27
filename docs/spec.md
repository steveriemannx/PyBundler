# Spec reference

PyBundler looks for configuration in this order:

1. the path passed to the command (`pybundler build path/to/pybundler.toml`),
2. `./pybundler.toml`, then `./pybundler.build.toml`,
3. `./pyproject.toml` containing a `[tool.pybundler]` table.

All relative paths in the spec are resolved against the **spec file's
directory**, not the current working directory. A spec can be a standalone file
or the `[tool.pybundler]` table of a `pyproject.toml`; `[project]` fields
(`name`, `version`, `scripts`) are used as defaults.

```toml
[app]
name = "myapp"
entry = "src/main.py"
```

is the minimum useful spec. Everything else has a default.

## `[app]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `name` | string | `[project].name` or directory name | Application/executable name. Letters, digits, `.`, `-`, `_`. |
| `version` | string | `[project].version` or `"0.0.0"` | Shown in `pybundler.json`, macOS bundles and reports. |
| `entry` | string | auto-detected | Entry point, see below. |
| `mode` | `"onedir"` \| `"onefile"` | `"onedir"` | Bundle shape. `--onefile`/`--onedir` override. |
| `console` | bool | `true` | `false` builds a `.app` on macOS and suppresses the console window on Windows. |
| `icon` | path | none | macOS `.icns` copied into the `.app` as `AppIcon.icns`; on Windows a `.ico` is embedded into the launcher (a built-in default icon is used otherwise). |
| `bundle_id` / `macos_bundle_id` | string | `org.pybundler.<name>` | `CFBundleIdentifier`. |
| `company` | string | `""` | Reserved for metadata. |

### Entry points

| Form | Example | Behaviour |
| --- | --- | --- |
| script | `entry = "main.py"` | Executed as `__main__`; its directory is the first import root. |
| package `__main__` | `entry = "myapp/__main__.py"` | Detected as a package and executed as `myapp.__main__` (relative imports work). |
| module | `entry = "myapp.__main__"` | `python -m` semantics. |
| callable | `entry = "myapp.cli:main"` | Imports the module and calls the function with no arguments (console-scripts style). |

Without `entry`, PyBundler tries `src/__main__.py`, `__main__.py`,
`src/main.py`, `main.py`, `src/app.py`, `app.py`, then
`src/<name>/__main__.py` and `<name>/__main__.py`. In a `pyproject.toml` with a
single `[project.scripts]` entry and no explicit `entry`, that script is used
(callable form).

## `[python]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `executable` | path | auto | Interpreter used for analysis. Auto order: `.venv`, `venv`, `env`, `$VIRTUAL_ENV`, current interpreter. |
| `version` | string | build interpreter's `X.Y` | Requested runtime version (`"3.13"` or `"3.13.7"`). Must match the build interpreter's minor version. |
| `runtime` | `"auto"` \| `"download"` \| `"local"` | `"auto"` | Where the shipped interpreter comes from. |
| `runtime_path` | path | none | A python-build-standalone style runtime root (or any prefix that passes the relocatability probe). Implies local use. |
| `pbs_tag` | string | `20261124` (or `PYBUNDLER_PBS_TAG`) | python-build-standalone release tag. |

Rules:

* The build environment and the runtime must share the Python **minor** version
  and CPU architecture; otherwise the build fails with an explanation.
* **FreeBSD** has no python-build-standalone binaries. `runtime = "download"`
  fails with a hint; `auto` falls back to the local interpreter (from
  `pkg`/ports), copies only the interpreter's own files from `/usr/local`, and
  collects the ELF dependencies it can resolve.
* **Bitness matters.** A 32-bit Python is a 32-bit target even on a 64-bit OS.
  Windows i686 is available through PBS; Linux i686 needs `--runtime local`
  (PBS has no 32-bit Linux builds); macOS no longer supports 32-bit runtimes.
  Use a build environment whose pointer size matches the runtime.
* `runtime = "auto"` reuses the local interpreter only when `otool`/`readelf`
  show it is relocatable. Homebrew and python.org framework builds are not, so
  they trigger a download.
* Downloads are cached in `PYBUNDLER_CACHE_DIR` (see `pybundler cache`).

## `[build]`

| Key | Type | Default | Meaning |
| --- | --- | --- | --- |
| `output_dir` | path | `dist` | Where bundles are written. |
| `work_dir` | path | `build` | Intermediate files (`build/pybundler/<name>/`). |
| `strip_sources` | bool | `true` | Ship `.pyc` only (except the entry script and stdlib sources). `--keep-sources` overrides. |
| `compress` | bool | `true` | zlib-compress onefile payload entries when beneficial. Ignored for onedir. |
| `verify` | bool | `false` | Run the bundle once at the end of the build (with `launch.args`). |
| `keep_work` | bool | `false` | Keep `work_dir` after a successful build. |

## `[collect]`

| Key | Type | Meaning |
| --- | --- | --- |
| `hidden_imports` | list[str] | Modules to include even though static analysis cannot see them. |
| `exclude` | list[str] | Module names/globs to drop (`tkinter`, `pkg.tests`, `pkg.tests.*`). A bare name also excludes submodules. |
| `packages` | list[str] | Packages whose complete tree must be included (namespace packages, plugin hosts). |
| `data` | list[str] | Extra files/directories, see below. |
| `binaries` | list[str] | Extra native files, see below. |
| `hooks` | list[path] | Hook files (see [hooks.md](hooks.md)). |
| `include_tests` | bool | Include `test`/`tests` directories inside copied packages (default `false`). |

### `data` and `binaries` syntax

Entries use the host's path separator (`:` on POSIX, `;` on Windows):

```
"assets:assets"                 # copy project assets/ -> <bundle>/assets
"config/*.json:config"          # glob, copy matches into <bundle>/config/
"docs"                          # implicit destination: same relative path
"vendor/libfoo.dylib:lib/libfoo.dylib"
```

* Sources may be globs (`*`, `?`, `**`).
* Without an explicit destination the path relative to the project root is kept.
* A destination that looks like a directory (trailing `/`, or no suffix for a
  file source, e.g. `assets`) receives the file name automatically.
* Destinations are always bundle-relative; absolute destinations are rejected.
* On Windows use `;`, e.g. `"assets;assets"`.

## `[launch]`

| Key | Type | Meaning |
| --- | --- | --- |
| `env` | table[str, scalar] | Extra environment variables baked into `pybundler.json`; the bootstrap applies them with `os.environ.setdefault`, so an operator's environment wins. |
| `args` | list[str] | Arguments used by `build.verify` (and by nothing else). |

## CLI overrides

| Flag | Overrides |
| --- | --- |
| `--onefile`, `--onedir`, `--mode` | `app.mode` |
| `--name` | `app.name` |
| `--console`, `--windowed` | `app.console` |
| `--python` | `python.executable` |
| `--python-version` | `python.version` |
| `--runtime`, `--runtime-path`, `--pbs-tag` | `[python]` |
| `--output-dir`, `--work-dir` | `[build]` |
| `--strip-sources`, `--keep-sources` | `build.strip_sources` |
| `--no-compress` | `build.compress` |
| `--verify` | `build.verify` |
| `--keep-work` | `build.keep_work` |
| `--hidden-import` (repeatable) | appended to `collect.hidden_imports` |
| `--exclude-module` (repeatable) | appended to `collect.exclude` |
| `--add-data`, `--add-binary` (repeatable) | appended to `collect.data` / `collect.binaries` |
| `--hook-dir` | adds every `*.py` in the directory to `collect.hooks` |
| `--include-tests` | `collect.include_tests` |

## Environment variables

| Variable | Effect |
| --- | --- |
| `PYBUNDLER_CACHE_DIR` | Root for runtimes/downloads. Also honoured by onefile bundles at runtime (extraction location). |
| `PYBUNDLER_PBS_TAG` | Default python-build-standalone release tag. |
| `PYBUNDLER_TMPDIR` | Parent directory for temporary build dirs. |
| `PYBUNDLER_DEBUG` | Launcher/bootstrap prints paths and adds context to startup errors. |
| `CC` | Compiler used for the launcher. |
| `NO_COLOR` | Disable ANSI colours. |

## Exit codes

| Code | Meaning |
| --- | --- |
| 0 | Success |
| 1 | Build/runtime failure |
| 2 | Usage or configuration error |
| 3 | `pybundler licenses --check` found non-permissive licences |
| 130 | Interrupted |
