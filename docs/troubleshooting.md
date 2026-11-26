# Troubleshooting

Start with:

```bash
pybundler doctor
pybundler inspect --missing --distributions
PYBUNDLER_DEBUG=1 ./dist/myapp/myapp
```

Every build writes `dist/<name>.build.json` with warnings, missing imports,
native-library rewrites and the exact runtime/build ids.

## Build-time problems

### `no pybundler.toml found in ...`

Run `pybundler init`, or pass the spec path: `pybundler build path/to/spec.toml`.
PyBundler also reads `[tool.pybundler]` from `pyproject.toml`; note that
`[tool.pybundler.app]` (or a flat `[tool.pybundler]` table) is the correct shape
— a bare `[app]` table after `[tool.pybundler]` belongs to the top level in TOML.

### `build environment and bundled runtime use different Python versions`

Compiled extensions are only ABI-compatible within one Python minor version.
Create a virtualenv with the version you want to ship and build inside it:

```bash
python3.13 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
pybundler build --python-version 3.13
```

### `cannot parse <file>` with a syntax error

Analysis compiles sources with the build interpreter. If your code uses syntax
newer than the build Python (for example `except*` on 3.10), build with the same
or a newer Python than the runtime.

### `no standalone Python X.Y runtime published`

The pinned python-build-standalone release does not contain that version for
your platform. `pybundler doctor` shows cached versions; pick one from the error
message's hint, or pass `--pbs-tag` with a release that has it.

### `cannot reach GitHub to download a Python runtime`

Air-gapped build? Copy a python-build-standalone runtime to the machine and use
`--runtime-path /opt/python-3.13` (or `python.runtime_path` in the spec).
`--runtime local` reuses the build interpreter when it is relocatable.

### `the local interpreter does not look relocatable`

Homebrew, MacPorts and python.org framework builds record absolute library paths.
The bundle may work on the build machine but not elsewhere. Use the default
download mode for portable artifacts.

### FreeBSD: runtime download fails

python-build-standalone publishes no FreeBSD binaries. Install the interpreter
from ports/pkg and use it as the build environment:

```bash
pkg install python311 py311-pip
python3.11 -m pip install .
python3.11 -m pybundler build        # runtime = "auto" -> local
```

`runtime = "download"` is rejected with a hint. PyBundler copies the
interpreter's own files from `/usr/local` and collects ELF dependencies it can
resolve (via `readelf`/`objdump`, both in the base system on modern FreeBSD).
Ports binaries may carry an absolute `RPATH=/usr/local/lib`; if the target
machine does not have the same ports installed, use a static Python build or
ship the missing libraries with `collect.binaries` (the launcher adds `lib/` to
`LD_LIBRARY_PATH`).

### FreeBSD: `pkg: No packages available` / wrong Python version

The build environment must match the runtime's minor version. Use the same
interpreter for `pip install .` and `pybundler build`, e.g.
`python3.11 -m pip install .` followed by `python3.11 -m pybundler build`.

### `no C compiler found`

Install Xcode Command Line Tools (`xcode-select --install`), `build-essential`,
base clang (FreeBSD), or Visual Studio Build Tools with the C++ workload
(Windows). On Windows PyBundler uses `cl.exe` or `clang-cl` — MinGW is not used.
Open a Developer Command Prompt (`vcvarsall`) or run the Visual Studio
environment script so `cl` is on `PATH`. For `onedir` you can fall back to
`--launcher script`; `onefile` always needs a compiler because it must extract
its payload.

### `zlib headers/lib are missing`

Install zlib development files (e.g. `apt install zlib1g-dev`) or build with
`--no-compress`; the onefile container then stores files uncompressed.

### `failed to compile the launcher`

The error includes the compiler output. Common causes: a compiler wrapper that
does not accept `-std=c99`, missing `-lz`, or an `$CC` environment variable
pointing at the wrong tool. Try `CC=clang pybundler build`.

### Import analysis misses a module

```
warning: N import(s) could not be resolved
```

`pybundler inspect --missing` lists module and importer. Fixes, in order of
preference:

1. add the module to `collect.hidden_imports`,
2. add the package that owns the dynamic import to `collect.packages`,
3. write a hook with `ctx.hidden_import()` (see [hooks.md](hooks.md)).

Optional accelerators (`numpy_distutils`, `psutil`, `pytest`) show up as missing
because packages probe for them at runtime; that is expected and harmless.

### `entry point is missing from the bundle`

Do not delete files from `dist/`. For `--strip-sources`, remember that stdlib
sources are kept, but if you manually clean the bundle you break this invariant.

### `onefile` first run is slow

The launcher extracts ~40–100 MB into the cache on first run. Subsequent runs
reuse `<cache>/pybundler/<name>-<build-id>/`. Set `PYBUNDLER_CACHE_DIR` to put
the cache on a fast/local disk.

### `another instance is unpacking this application; giving up`

Two processes started on a cold cache and extraction failed or is very slow.
The launcher waits up to two minutes and clears dead locks. If it persists,
delete the stale directory (or the whole cache):

```bash
rm -rf ~/Library/Caches/pybundler/myapp
pybundler cache clear
```

### Onefile and onedir share the name

`dist/myapp` is either a directory (onedir) or a file (onefile). Build onefile
into a different `--output-dir` if you need both, or use different names.

## Runtime problems

### macOS: "cannot be opened because the developer cannot be verified"

PyBundler ad-hoc signs the launcher, but downloaded bundles carry the quarantine
attribute and are not notarized. For internal distribution:

```bash
xattr -dr com.apple.quarantine dist/MyApp.app
```

For public distribution, sign and notarize the `.app` with your Developer ID.
Note that appending a onefile payload invalidates a signature; prefer `.app`
(`--windowed`) bundles for notarized distribution.

### macOS: windowed app still shows a console

Check the build mode: `console = false` is required. A `.app` is produced only
when `console = false` on macOS; on Windows the console subsystem is used and
`FreeConsole()` is called when windowed.

### `tkinter` fails or Tcl data is missing

`tkinter` is only included when imported (or listed in `hidden_imports`). That
enables the `tcl-tk` runtime feature. Verify with:

```bash
python -c "import tkinter; print(tkinter.Tcl().eval('info patchlevel'))"
pybundler inspect --files | grep -E 'tcl|tkinter'
```

### matplotlib backend errors

Matplotlib chooses its backend at runtime. If you use TkAgg, make sure
`tkinter` is included (`hidden_imports = ["tkinter"]`); the built-in hook warns
when it detects the mismatch. For headless servers force a non-GUI backend in
your code or via `MPLBACKEND=Agg`.

### Windows: a DLL is missing

PyBundler copies DLLs that live inside package directories (and wheel sibling
`*.libs` directories) and adds the bundle's `lib/` to `PATH`. It does **not**
parse PE import tables, so a DLL loaded from a custom location must be declared
explicitly:

```toml
[collect]
binaries = ["C:/vendor/native/foo.dll:lib/foo.dll"]
```

Build from a Visual Studio Developer Command Prompt (or let CI set it up) so
`cl`/`clang-cl` is on `PATH`; set `CC=cl` if another compiler shadowing it. For
32-bit output use the **x86** Developer Command Prompt together with a 32-bit
Python (PBS publishes `i686-pc-windows-msvc` runtimes). Onefile builds without
zlib store the payload uncompressed, which is fine but larger. Windowed builds
keep the console subsystem and call `FreeConsole()`, so a console may flash
briefly at startup.

### 32-bit builds

* **Windows:** install 32-bit Python, run from the x86 Developer Command Prompt,
  and PBS will fetch the `i686-pc-windows-msvc` runtime automatically.
* **Linux:** PBS has no i686 build. Install a 32-bit Python from the distro and
  use `--runtime local`; PyBundler copies it and its ELF dependencies.
* **FreeBSD:** a 32-bit system Python works with `--runtime local`.
* **macOS:** 32-bit is impossible (Apple removed it); build `x86_64` or `arm64`.

A bitness mismatch between build environment and runtime is a hard error, since
compiled extensions cannot be mixed.

### Script launcher limitations

`--launcher script` writes a shell/batch wrapper instead of compiling C. It is a
fallback for `onedir` builds only, does not produce `.app` bundles, and on
Windows the batch wrapper cannot forward the interpreter flags that
`multiprocessing`'s `resource_tracker` uses. Prefer a real compiler for anything
that spawns processes.

### `multiprocessing` spawn errors / repeated output

PyBundler sets `sys.executable` to the application and the launcher forwards
interpreter invocations (`-c`/`-m`) to the bundled Python, so `spawn`,
`forkserver`, `resource_tracker` and `freeze_support()` work. Requirements:

* call `multiprocessing.freeze_support()` as the first statement inside
  `if __name__ == "__main__":`,
* do not guard module-level code that children must not execute behind
  something else (same rule as PyInstaller).

### `importlib.metadata.version("pkg")` raises `PackageNotFoundError`

Only distributions whose code is actually included get their `*.dist-info`
copied. If you query metadata for a package you never import, add it to
`hidden_imports` or `packages`.

### A package's data file is missing

Files inside an imported package directory are copied automatically. Files
outside it need `collect.data` or a hook (`ctx.add_data`). Files written at
runtime must go to a user-writable location, not inside the bundle.

### Tracebacks have no source lines

`strip_sources` (default) ships `.pyc` files. Rebuild with `--keep-sources` to
ship `.py` files and get readable tracebacks (at the cost of size).

### Numpy/OpenBLAS licence warning

Libraries copied from `/opt/homebrew`, `/usr/local` or `/usr/lib` are recorded
in `licenses/NATIVE-LIBRARIES.json`. Verify their licences; GCC runtime
libraries, for example, carry the GPL-with-GCC-exception.

## Getting help

When filing an issue, include:

* `pybundler doctor --json`
* the failing command and full output (`-v`)
* `dist/<name>.build.json`
* the platform and Python versions
* a minimal reproducer project, if possible
