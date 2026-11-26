# Architecture

This document describes how PyBundler turns a Python project into a
self-contained application. It is written for people who want to debug a build,
write a hook, or contribute.

## Pipeline

```
              ┌──────────────────┐
 pybundler.toml│  config.py       │
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐   run target interpreter once,
              │  interp.py       │   cache sys.path / EXT_SUFFIX / builtins
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐   PBS download or local prefix,
              │  runtimes.py     │   SHA-256 verified, cached per version
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐   bytecode import scanning + hooks
              │  analysis/       │   models: Module, Distribution, DataFile
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐   Payload = ordered (dest, source/mode) list
              │  collect/        │   runtime trim, package trees, licences
              └────────┬─────────┘
                       ▼
              ┌──────────────────┐   materialise → pyc → native fixup → launcher
              │  build/          │   → onedir directory | onefile container | .app
              └──────────────────┘
```

Every phase is deterministic for the same inputs: payload entries are sorted,
build ids are derived from content sizes and configuration (not timestamps), and
licence manifests are key-sorted. Rebuilding an unchanged project produces the
same build id.

## 1. Build environment

`interp.py` runs the selected interpreter with a tiny JSON probe. The probe is
executed with a sanitised environment (no `PYTHONPATH`, no user site) so the
result describes the environment, not the caller's shell.

Selection order for the build interpreter:

1. `--python` / `python.executable`,
2. `<project>/.venv`, `venv`, `env`,
3. `$VIRTUAL_ENV`,
4. the interpreter running PyBundler.

The build environment and the bundled runtime **must share the same Python
minor version**, because compiled extension modules are only ABI-compatible
within a minor version. PyBundler refuses to mix them and explains how to fix
it. The architecture must match too.

## 2. Runtime provisioning

PyBundler ships a complete interpreter rather than relying on the host's:

* **download** (default): `cpython-<version>+<tag>-<triple>-install_only_stripped.tar.gz`
  from python-build-standalone. The pinned release tag is
  `PYBUNDLER_PBS_TAG` (default `20261124`). Assets are verified against the
  SHA-256 digest in the GitHub API response, extracted safely (no `..`, no
  absolute paths, no device nodes) and cached:
  `<cache>/runtimes/cpython-<version>-<triple>-<tag>/`.
* **local**: copy `sys.base_prefix`. Relocatability is checked with `otool`/
  `readelf`; Homebrew and python.org framework builds are detected as
  non-relocatable and only used when explicitly requested, with a warning.
* **auto**: use the local interpreter when it is relocatable and matches the
  requested version; otherwise download. On FreeBSD (no PBS builds) `auto`
  always uses the local interpreter and warns when it is not relocatable.

FreeBSD has no python-build-standalone binaries. `auto` therefore uses the
interpreter installed with pkg/ports, which is **not** downloaded or
checksum-verified by PyBundler; keep it patched through the OS package manager.

The cache is per-user (`~/Library/Caches/pybundler` on macOS, `$XDG_CACHE_HOME`
or `~/.cache/pybundler` elsewhere, `%LOCALAPPDATA%` on Windows, `~/.cache` on
FreeBSD) and can be overridden with `PYBUNDLER_CACHE_DIR`. A lock file prevents
concurrent builds from corrupting a cache entry.

When the runtime is **local** (system `/usr`, `/usr/local`, a Homebrew
framework or a venv base), the collector copies only the interpreter's own
files: `bin/python*`, `lib/libpython*`, `lib/pythonX.Y/**` (minus
`site-packages`/tests), Tcl/Tk when needed, and on Windows `python*.exe`,
`python*.dll`, `vcruntime*.dll`, `Lib/` and `DLLs/`. Debian/Ubuntu multiarch
`libpython` locations are recognised. Everything else in the prefix is left
behind.

### Runtime trimming

`collect/runtime.py` keeps the standard library whole except for well-understood
development trees:

* `test`, `tests`, `idlelib`, `turtledemo`, `ensurepip`, `lib2to3`,
* `site-packages` / `dist-packages` (third-party code comes from analysis),
* headers, `share/`, pkg-config files, static libs, `bin/*` wrappers except
  `python*`, per-version `config-*` build directories,
* Tcl/Tk and `tkinter` unless the graph used `tkinter` (then the `tcl-tk`
  feature is enabled).

Keeping the whole stdlib is intentional: it removes an entire class of failures
(lazily imported codecs, platform modules, `pkgutil`-driven imports) at the cost
of a few MiB.

## 3. Import analysis

`analysis/bytecode.py` compiles each source file with the build interpreter and
walks `IMPORT_NAME` instructions recursively, including code objects nested in
functions, classes and comprehensions. For every import statement it records the
module name, the relative-import level and the `fromlist`.

Resolution (`analysis/finder.py`) mirrors `FileFinder`:

1. extension modules (`EXT_SUFFIX`, `.so`, `.pyd`, `.abi3.so`),
2. regular modules (`name.py`),
3. packages (`name/__init__.py` or `name/__init__<EXT_SUFFIX>`),
4. namespace packages (directories without `__init__.py`).

Paths are classified as *stdlib*, *third-party* or *project*. Symlinks are
followed and classified by their lexical **and** resolved location, which is
required for Homebrew/Nix style installs where a package is a symlink into a
versioned store.

`analysis/graph.py` performs a breadth-first walk:

* parents of every module are added (`a.b.c` implies `a`, `a.b`),
* `from a.b import c` enqueues both `a.b` and the *soft* candidate `a.b.c`
  (it may be an attribute, not a module),
* literal `importlib.import_module("...")` / `__import__("...")` calls are added,
* unused imports report as missing only when they are hard imports; stdlib
  names are never reported.

Missing hard imports are warnings, not errors: optional accelerators are
common. `pybundler inspect --missing` lists them with the importing module.

### Package trees

For every imported *package*, the complete directory is copied (minus
`__pycache__`, tests, build caches, and excluded submodules). This is the core
robustness decision of PyBundler: package data, plugin folders, lazily imported
submodules and dynamically loaded extensions all arrive automatically. A hook is
only needed for things that live *outside* the package tree or for optional
runtime features.

### Distribution metadata

`analysis/distinfo.py` parses `*.dist-info` directories directly (no
`importlib.metadata`) and maps top-level import names to distributions using
`top_level.txt`, `RECORD`, or — when a distro stripped both, as Homebrew does —
the distribution name. The metadata is shipped into `site/` so that
`importlib.metadata.version("numpy")` works inside the bundle.

## 4. Hooks

Hooks are Python files with an optional `PACKAGES` list and one of:

* declarative `HIDDEN_IMPORTS`, `DATA`, `BINARIES`,
* a `hook(ctx)` function using the `HookContext` API.

Built-in hooks live in `src/pybundler/hooks/` (Tcl/Tk and a matplotlib backend
check). User hooks come from `collect.hooks` or `--hook-dir`. Hooks run after
the initial walk and may enqueue imports that themselves trigger more hooks;
the loop is bounded to six rounds. See [hooks.md](hooks.md).

## 5. Payload and materialisation

`build/plan.py` defines a `Payload` as an ordered mapping from bundle-relative
path to either a source file, a text blob, or a symlink. Safety rules:

* destinations must be relative, without `..` segments,
* duplicate destinations keep the first writer and emit a warning,
* symlinks inside package trees are dereferenced when copying (content is
  shipped), while runtime symlinks such as `bin/python3` stay symlinks in
  onedir builds.

Materialisation writes into a staging directory, precompiles bytecode, then
fixes native libraries. For `onedir`, the staging directory is moved into
`dist/` atomically (old build renamed to `.<name>.old` first). For `onefile`,
the payload is packed into a container, appended to the launcher, and moved into
place.

### Bytecode

`precompile` runs the **bundled** interpreter with `py_compile`, using
`UNCHECKED_HASH` invalidation. With `strip_sources` (default), sources become
legacy-location `.pyc` files (`site/pkg/mod.pyc`) and are deleted. Two
exceptions remain as source: the entry script (executed by the bootstrap) and
`_pybundler_boot.py` itself is loaded as a module so its `.pyc` suffices.

### Native library fixup

`collect/binaries.py` walks extension modules in `site/`, `lib/` and `bin/`:

* **macOS**: `otool -L` dependencies that are absolute or `@rpath`-based and not
  provided by the OS are copied to `lib/`. The referring binary is rewritten
  with `install_name_tool -change <old> @loader_path/<relative>`. Copied
  libraries are inspected recursively, their rpaths and source directories are
  used to resolve transitive dependencies, and modified binaries are re-signed
  ad hoc. Absolute references to OS libraries (`/usr/lib`, `/System`) are left
  alone.
* **Linux / FreeBSD**: `readelf -d` (`objdump -p` fallback) `NEEDED` entries are
  resolved through `$ORIGIN`, rpaths, the bundle and the build machine.
  Non-glibc/non-base libraries are copied to `lib/`; the launcher prepends
  `lib/` to `LD_LIBRARY_PATH`. Stdlib extension modules
  (`lib/pythonX.Y/lib-dynload`) and multiarch directories are scanned too.
* **Windows**: adjacent DLLs are resolved by the Windows loader, and the
  launcher prepends the bundle's `lib/` to `PATH`, so DLLs copied there are
  found. PyBundler does not parse PE import tables yet, so external DLLs must be
  shipped with their package or via `collect.binaries`.

Wheel-created sibling directories (`numpy.libs`, `PIL.libs`, `*.dylibs`, …) are
copied together with the package that owns them.

Copied native libraries are listed in `licenses/NATIVE-LIBRARIES.json` and in
the build report, because their licences are not covered by Python package
metadata.

## 6. Bundle layout (onedir)

```
dist/myapp/
  myapp                  native launcher (or run.sh / .cmd with --launcher script)
  pybundler.json         build manifest (entry point, runtime paths, build id)
  site/                  PYTHONPATH: application + packages + dist-info + boot module
  runtime/python/        PYTHONHOME: trimmed CPython install
  lib/                   extra shared libraries discovered by the fixup
  assets/                data from the spec
  licenses/              licence files and manifests
```

The launcher resolves everything relative to its own executable, so the
directory can be moved or renamed. Windows adds `.exe`; a windowed macOS build
places the same tree in `MyApp.app/Contents/MacOS/`.

## 7. Launcher

`resources/launcher.c` is compiled per application. The build writes a small
generated header (`#define PYB_NAME "..."`, build id, relative paths, mode) and
forces it into the translation unit (`-include` for GCC/Clang, `/FI` for
MSVC). Windows uses the Microsoft toolchain (`cl` or `clang-cl`, static CRT,
`shell32.lib`/`user32.lib`); macOS/Linux/FreeBSD use a C99 compiler. 32-bit
POSIX targets compile with `-m32`; 32-bit Windows uses the x86 MSVC environment.

At runtime the launcher:

1. locates itself (`_NSGetExecutablePath` on macOS, `/proc/self/exe` on Linux,
   `sysctl(KERN_PROC_PATHNAME)` on FreeBSD, `GetModuleFileNameW` on Windows),
2. for onefile builds reads the 24-byte footer (64-bit file offsets, so 32-bit
   launchers handle large payloads), extracts the container into
   `<cache>/pybundler/<name>-<build-id>/` under a lock, with a temporary
   directory + atomic rename for crash safety, and path-traversal checks,
3. sets `PYTHONHOME`, `PYTHONPATH`, `PYTHONNOUSERSITE`, `PYTHONSAFEPATH`,
   `PYTHONDONTWRITEBYTECODE`, `PYTHONUTF8`, `PYBUNDLER_ROOT`,
   `PYBUNDLER_APP_PATH`, and prepends `lib/` to `DYLD_LIBRARY_PATH` /
   `LD_LIBRARY_PATH`,
4. execs `python -B -s -m _pybundler_boot <user args>`.

If the first argument group contains `-c` or `-m`, the launcher skips the
bootstrap and execs the interpreter directly. That is what makes
`multiprocessing`'s `resource_tracker` (which runs
`sys.executable -B -s -P -c ...`) work while `sys.executable` points at the
application, exactly like a PyInstaller bootloader.

The bootstrap (`resources/boot.py`) reads `pybundler.json`, sets
`sys.frozen`, `sys._MEIPASS`, `sys.executable` and `sys.pybundler`, then runs
the entry point:

* `script`: compiled and executed as a fresh `__main__` module (so
  `sys.argv[0]` stays the application path),
* `module`: `runpy.run_module(..., run_name="__main__")`,
* `callable`: imports `module:function` and calls it (console-scripts style).

## 8. Onefile container

```
[ launcher ][ container ][ footer ]
```

Container (`build/onefile.py`):

```
magic     8   "PBFC1\r\n\0"
version   u32 1
count     u32
per entry:
  name_len u16, method u8 (0 stored / 8 zlib), kind u8 (0 file / 1 symlink),
  mode u32, usize u64, csize u64, name, data
```

Footer:

```
magic   8   "PBFEND01"
offset  u64  start of container
length  u64  container length
```

The Python implementation can read/extract containers too (`pybundler info` on a
bare `.pbf`, tests), and rejects absolute paths, `..` segments and backslashes.
Compression is used only when it actually shrinks a file and when zlib is
available at build time; otherwise the same container format stores files
uncompressed.

## 9. Build id and cache

The build id is a 12-hex-digit SHA-256 over sorted payload destinations, sizes
and the configuration. Onefile builds use it as the extraction directory name,
so changing the application invalidates the cache automatically. The cache is
never cleaned automatically (documented in `pybundler cache`); the bootstrap
reuses an existing directory atomically.

## 10. Report

Every build writes `dist/<name>.build.json` with tool and runtime versions,
analysis statistics, payload totals by origin, native-library rewrites, licence
entries, warnings, bytecode failures, verification results and timings. This is
the first file to attach to a bug report.

## Implementation constraints

* Python 3.11+ for the bundler; target applications can use any version that
  python-build-standalone publishes (3.11–3.15 at the time of writing), as long
  as the build environment uses the same minor version.
* Only CPython. PyPy/GraalPy are rejected with an explicit error.
* 32-bit targets are first-class: the launcher uses 64-bit file offsets
  (`_fseeki64`) so onefile payloads work on i686 Windows, and the target is
  derived from the *interpreter's* pointer size, not just the OS architecture.
* No `pip` at build time. Analysis is filesystem-based: whatever is importable
  in the build environment is what gets bundled.
* Full end-to-end coverage runs on macOS arm64 locally. CI jobs are included
  for Linux, Windows (MSVC, x64/x86) and FreeBSD; Windows/FreeBSD are beta.
