# PyBundler

Bundle a Python application into a relocatable directory or a single executable.
PyBundler is an independent alternative to PyInstaller with two deliberate
differences:

* **BSD-2-Clause** licensed, with **zero runtime dependencies** (pure CPython
  standard library), and
* a design that prefers *working on the first try* over minimal size: the whole
  standard library and whole package trees are shipped, and every bundled
  dependency's licence is collected and audited.

```
$ pybundler build --verify
==> Preparing Python runtime
  ok runtime Python 3.14.10 ready (65.7 MiB)
==> Analysing imports
  ok 150 modules, 3 distributions
==> Planning payload
  ok 956 files, 57.9 MiB planned
==> Materialising bundle
==> Precompiling bytecode
==> Fixing native libraries
  ok 5 copied, 12 load command(s) rewritten
==> Building launcher
  ok launcher compiled with cc
==> Installing bundle

bundle:  dist/myapp
launch:  dist/myapp/myapp
mode:    onedir (native launcher)
size:    127.7 MiB
```

## Status

**Beta.** The onedir/onefile pipeline, the analysis engine, native-library
rewriting, `.app` assembly and licence collection are tested end-to-end on
macOS arm64 (see [tests](tests)).

| Platform | Runtime | Status |
| --- | --- | --- |
| macOS 11+ (arm64, x86_64) | python-build-standalone | tested end-to-end |
| Linux glibc (x86_64, aarch64) | python-build-standalone | shared code paths, unit tests, CI job |
| Windows 10+ (x64, x86) | python-build-standalone | beta; MSVC/clang-cl launcher, CI job |
| FreeBSD 13+ (x86_64, i686) | system Python (ports/pkg) | beta; no PBS builds exist, CI job |

Native-library handling is Mach-O on macOS, ELF on Linux/FreeBSD, and
"adjacent DLLs" on Windows. PE import-table rewriting is not implemented yet;
ship external DLLs inside package directories or via `collect.binaries`.

**32-bit x86:** Windows i686 is fully supported (PBS publishes `i686-pc-windows-msvc`).
Linux and FreeBSD use a local 32-bit interpreter (`--runtime local`) because PBS
has no 32-bit Linux builds. macOS has no 32-bit runtime since 10.15; build
`x86_64` (Intel) instead, which is fully supported.

## Install

Requires **Python 3.11+** to run the bundler. There is nothing to `pip install`
at runtime: PyBundler itself has no third-party dependencies.

```bash
git clone https://github.com/pybundler/pybundler
cd pybundler
python -m pip install .
pybundler --version
```

A C compiler is needed for native launchers:

| Platform | Toolchain |
| --- | --- |
| macOS | Xcode Command Line Tools (`xcode-select --install`) |
| Linux | `build-essential` / `gcc` |
| Windows | Visual Studio Build Tools (MSVC `cl`) or LLVM `clang-cl`. MinGW is not used. For 32-bit builds use the x86 Developer Command Prompt and 32-bit Python. |
| FreeBSD | base `clang` (`cc`) and `pkg install python311 py311-pip` |

Without a compiler, `onedir` builds can still use `--launcher script`
(a shell/batch wrapper). `onefile` always needs a compiler.

## Quick start

```bash
pybundler init                 # writes pybundler.toml + main.py if missing
pybundler build                # -> dist/<name>/<name>
./dist/myapp/myapp
```

Build a single file instead:

```bash
pybundler build --onefile
./dist/myapp                  # first run extracts into the user cache (~1-2 s), later runs are instant
```

macOS windowed application (`.app`):

```bash
pybundler build --windowed
open dist/MyApp.app
```

More commands:

```
pybundler build [spec]        build a bundle
pybundler inspect             explain imports, missing modules and the payload
pybundler run <bundle>        launch a built bundle
pybundler licenses --check    fail if a bundled dependency is not permissive
pybundler info <bundle>       show the embedded manifest / onefile layout
pybundler doctor              check compiler, zlib, cache, runtime versions
pybundler cache list|prune    manage downloaded interpreters
pybundler clean               remove this project's build output
```

`-v`, `-q` and `--color` work on every command (before or after the
subcommand); `--json` is available on `build`, `inspect`, `licenses`, `info`,
`doctor` and `cache`.

## Spec file

PyBundler reads `pybundler.toml` — or `[tool.pybundler]` in `pyproject.toml`:

```toml
[app]
name = "myapp"
version = "1.2.0"
entry = "src/myapp/__main__.py"   # or "myapp.__main__" or "myapp.cli:main"
mode = "onedir"                   # "onedir" | "onefile"
console = true                    # false -> .app on macOS / no console on Windows
icon = "assets/AppIcon.icns"      # macOS .app icon (.icns) / Windows .ico
macos_bundle_id = "org.example.myapp"

[python]
version = "3.14"                  # defaults to the build environment's version
runtime = "auto"                  # auto | download | local
# runtime_path = "/path/to/python-build-standalone"
# executable = ".venv/bin/python"

[build]
output_dir = "dist"
work_dir = "build"
strip_sources = true              # .pyc only; --keep-sources for readable tracebacks
compress = true                   # zlib for onefile payloads
verify = false                    # run the bundle once after building

[collect]
hidden_imports = ["myapp.plugins.extra"]
exclude = ["tkinter", "myapp.tests"]
packages = ["myapp"]              # force whole packages into the bundle
data = ["assets:assets", "config/*.json:config"]
binaries = ["vendor/libfoo.dylib:lib/libfoo.dylib"]
hooks = ["build/hooks/myapp.py"]
include_tests = false

[launch]
args = ["--self-test"]            # used by --verify
env = { MYAPP_ENV = "production" }
```

Full reference: [docs/spec.md](docs/spec.md).

## How it works

1. **Interpreter probe** — PyBundler asks the build environment (`.venv`,
   `$VIRTUAL_ENV`, or `--python`) for its `sys.path`, extension suffix and
   built-in modules. Import analysis runs against *that* environment.
2. **Runtime provisioning** — a relocatable CPython is taken from
   [python-build-standalone](https://github.com/astral-sh/python-build-standalone)
   (PSF-2.0, relocatable, verified by SHA-256) and cached per version. If your
   interpreter is already relocatable, `--runtime local` reuses it.
3. **Import analysis** — source is compiled to code objects and every
   `IMPORT_NAME` is followed, including imports inside functions, class bodies
   and comprehensions. Relative imports are resolved from the module context,
   literal `importlib.import_module("x")` calls are picked up, and package data
   is carried along because imported packages are copied **whole**.
4. **Payload planning** — the runtime is trimmed of tests, `site-packages`,
   headers, static libs and (unless `tkinter` is used) Tcl/Tk. Distribution
   metadata (`*.dist-info`) is shipped so `importlib.metadata` keeps working.
5. **Native fixup** — Mach-O dependencies outside the bundle are copied into
   `lib/` and their load commands rewritten to `@loader_path/...`; ELF
   dependencies are copied and found through `LD_LIBRARY_PATH`.
6. **Launcher** — a small C program (compiled at build time with the paths baked
   in) sets `PYTHONHOME`/`PYTHONPATH`, extracts onefile payloads on first run,
   and execs the bundled interpreter.

PyBundler exposes the same compatibility surface applications expect from a
frozen build: `sys.frozen`, `sys._MEIPASS`, `sys.executable` pointing at the
application, and working `multiprocessing` (spawn and fork) including
`freeze_support()`.

Details: [docs/architecture.md](docs/architecture.md).

## Licensing

This is the reason the project exists, so it is taken seriously:

| Component | Licence |
| --- | --- |
| PyBundler source | **BSD-2-Clause** ([LICENSE](LICENSE)) |
| PyBundler runtime dependencies | **none** |
| Bundled CPython (PBS build) | PSF-2.0 |
| Onefile compression (zlib) | Zlib licence |
| Your dependencies | unchanged; collected into `licenses/` |
| Copied native libraries | listed in `licenses/NATIVE-LIBRARIES.json` |

Every build writes:

```
licenses/pybundler/LICENSE          BSD-2 text for the bundler
licenses/python/LICENSE             CPython licence
licenses/application/LICENSE        your project's LICENSE/COPYING, when present
licenses/python/<dist>-<ver>/...    licence files of every bundled package
licenses/THIRD-PARTY.json           machine readable manifest
licenses/NATIVE-LIBRARIES.json      external .dylib/.so files + their origin
```

`pybundler licenses <bundle>` adds `licenses/AUDIT.json` (classification
results and warnings) when the bundle is a onedir directory.

`pybundler licenses --check` exits with code **3** if any bundled distribution's
licence is not on the permissive allow-list (MIT, BSD, Apache-2.0, ISC, PSF,
Zlib, …). It is an advisory audit, not legal advice. See
[docs/licensing.md](docs/licensing.md).

## Design trade-offs

* **Whole stdlib, whole packages.** Bundles are larger than PyInstaller's but
  lazy imports, plugin directories and package data work without per-package
  recipes. `strip_sources` (default on) removes `.py` sources, keeping `.pyc`.
* **No cross-compilation.** The runtime must match the build machine's OS,
  architecture, bitness and Python *minor* version. Build each target on its
  own platform (or in a matching container/CI runner). FreeBSD, Windows, Linux
  and macOS launchers are compiled with the host toolchain (MSVC/clang-cl on
  Windows, C99 compilers elsewhere).
* **Downloads are pinned.** The PBS release tag is pinned
  (`PYBUNDLER_PBS_TAG`, default `20261124`) and cached; `--pbs-tag` overrides it.
* **No zipimport/egg support.** Wheels and normal installs only; PyInstaller
  has the same practical limitation.

`pybundler licenses <bundle>` also writes `licenses/AUDIT.json` next to the
bundle's manifests when given a onedir directory.

## Known limitations

* Windows: DLL dependency discovery is limited to DLLs shipped inside packages
  or copied into `lib/` (which is added to `PATH`); no PE import-table
  rewriting yet.
* FreeBSD: PyBundler ships the system Python (`/usr/local`) and copies the ELF
  dependencies it can resolve. Absolute `RPATH` entries baked into ports
  binaries may still refer to `/usr/local/lib`, so the target machine should
  have the same port packages installed (or use a static Python build).
  python-build-standalone has no FreeBSD binaries, so `runtime = "download"`
  is rejected with a clear error.
* macOS: ad-hoc signatures only. Notarization, hardened runtime and universal
  binaries are on the roadmap; onefile executables cannot be re-signed with
  `codesign` after appending (the kernel still accepts the linker signature).
* No UPX, no splash screens, no Linux `.desktop` generation.
* Windows launchers embed `icon` when it is a `.ico` file and fall back to a built-in
  default icon otherwise.
* `--strip-sources` keeps stdlib sources (for tracebacks) and the entry script.
* Experiments with bytecode-level dead code analysis are intentionally avoided;
  dynamic imports need `hidden_imports` or a hook.

Roadmap: smaller stdlib pruning, cross-build via CI containers, Windows PE
imports, notarization helper, zipapp-style *onefile without extraction*.

## Development

```bash
make test          # unit tests (no network, no compiler needed)
make e2e           # full builds of tests/fixtures/hello-console (needs network or a cached runtime)
make lint          # compileall + zero-dependency check
make example       # build the GUI example (examples/hello)

# FreeBSD:
pkg install python311 py311-pip
python3.11 -m pip install .
python3.11 -m pybundler build --verify
```

Tests use `unittest` only, so `python -m unittest` works anywhere:

```bash
PYTHONPATH=src python -m unittest discover -s tests -t tests
PYBUNDLER_E2E=1 PYTHONPATH=src python -m unittest discover -s tests -t tests -p 'test_e2e.py'
```

Project layout:

```
src/pybundler/          bundler package (stdlib only)
  analysis/             interpreter probe, import graph, hooks, dist metadata
  collect/              runtime trimming, package trees, native libs, licences
  build/                payload plan, onefile container, launcher, .app, build
  resources/            launcher.c and bootstrap boot.py
  hooks/                built-in hooks (tkinter, matplotlib)
examples/hello/         GUI (Tkinter) example
  tests/fixtures/hello-console/  console fixture used by the e2e tests
docs/                   architecture, spec, hooks, licensing, comparison
tests/                  unittest suite
```

Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md) and
[SECURITY.md](SECURITY.md).
