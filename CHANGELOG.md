# Changelog

All notable changes to PyBundler are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/).

## [0.2.1] - 2026-11-26

### Changed

* Copyright holder `steveriemannx` added to `LICENSE`, package metadata and
  the embedded licence text.

* **Windows launchers are built with the Microsoft toolchain** (`cl.exe` or
  `clang-cl`) instead of MinGW. MinGW is no longer selected or required; the
  launcher links the static CRT (`/MT`) and the required Win32 libraries.
* **32-bit x86 targets**: bitness is taken from the interpreter's pointer size,
  not just the OS architecture. Windows i686 uses PBS
  `i686-pc-windows-msvc`; Linux and FreeBSD use a local 32-bit interpreter
  (`--runtime local`) because PBS has no 32-bit Linux builds; macOS remains
  64-bit only (x86_64/arm64), as Apple removed 32-bit support.
* The launcher uses 64-bit file offsets (`_fseeki64`) so onefile payloads work
  on 32-bit Windows, and the Windows cache/lock paths are wide-character safe.

### Fixed

* `#if PYB_USE_ZLIB` incorrectly pulled in `zlib.h` when compression was
  disabled, breaking builds on hosts without zlib development files.
* Microsoft C compatibility: `FOF_NO_UI` is replaced with the underlying flags,
  CRT deprecation warnings are disabled, and Win32 libraries are linked via
  `#pragma comment`.

## [0.2.0] - 2026-11-26

Cross-platform release: FreeBSD support, stronger Windows support and safer
local-runtime collection.

### Added

* **FreeBSD support** (13+): the local interpreter from ports/pkg is used as the
  runtime (`auto` falls back to local because python-build-standalone publishes
  no FreeBSD binaries), ELF dependencies are collected, and the launcher locates
  itself through `sysctl(KERN_PROC_PATHNAME)`.
* **Windows improvements**: `lib/` is prepended to `PATH` so copied DLLs are
  found, cache and lock paths use wide-character APIs for Unicode user names,
  and wheels' sibling `*.libs` / `*.dylibs` directories are bundled with their
  packages.
* Local runtime prefixes (`/usr`, `/usr/local`, Homebrew frameworks, venvs) are
  trimmed to the interpreter's own files instead of copying the whole prefix.
  Debian/Ubuntu multiarch `libpython` locations are handled.
* Native-library fixup scans `lib/python*/lib-dynload`, `DLLs/` and multiarch
  directories, so dependencies of stdlib extension modules are collected too.
* python-build-standalone `install_only` is used as a fallback when the
  stripped asset is unavailable for a version.
* `pybundler cache --json`, safer `pybundler clean` (only the current project's
  output; `--all` for the old behaviour), application `LICENSE` copied to
  `licenses/application/`, and `licenses/AUDIT.json` written by
  `pybundler licenses`.

### Fixed

* Missing symlink entries in onefile payloads (the interpreter is no longer
  duplicated three times; hello onefile is back to ~23 MiB).
* Directory symlinks inside runtimes are preserved instead of silently dropped.
* Duplicate build warnings and missing-import duplicates.
* `--runtime-path` now implies local mode even when `runtime = "auto"`.

## [0.1.0] - 2026-11-26

First public beta.

### Added

* `pybundler build` with `onedir` and `onefile` output.
* Relocatable CPython runtimes from python-build-standalone:
  * pinned release tag (`PYBUNDLER_PBS_TAG`, default `20261124`),
  * SHA-256 verification of downloads,
  * per-user cache with locking, plus `pybundler cache list|prune|clear`.
* Import analysis based on bytecode `IMPORT_NAME` scanning:
  * imports inside functions, classes and comprehensions,
  * relative imports resolved from module context,
  * literal `importlib.import_module("…")` / `__import__("…")`,
  * namespace packages, extension modules and symlinked installs.
* Whole-package copying, so package data, plugins and lazily imported
  submodules work without recipes.
* Distribution metadata (`*.dist-info`) is shipped so `importlib.metadata`
  works inside bundles; licence files are collected automatically.
* Native library handling:
  * Mach-O dependency copying and `@loader_path` rewriting,
  * ELF `NEEDED` collection with `LD_LIBRARY_PATH` support,
  * `licenses/NATIVE-LIBRARIES.json` for libraries copied from the build host.
* C launcher with application constants baked in:
  * relative path resolution (bundles are movable),
  * onefile extraction with zlib, atomic rename, lock file and traversal checks,
  * interpreter pass-through for `-c`/`-m` (makes `multiprocessing` work),
  * `--launcher script` fallback for compilers-free onedir builds.
* macOS `.app` assembly (`--windowed`), `Info.plist`, `.icns` icons.
* `sys.frozen`, `sys._MEIPASS`, `sys.executable`, `sys.pybundler` compatibility,
  plus `multiprocessing` spawn/fork support.
* Spec files (`pybundler.toml`, `[tool.pybundler]` in `pyproject.toml`),
  `pybundler init`, CLI overrides, `[launch.env]`.
* Hook system with declarative attributes and a Python API; built-in hooks for
  `tkinter` (Tcl/Tk) and matplotlib backend mismatches.
* Commands: `build`, `inspect`, `run`, `info`, `licenses`, `doctor`, `init`,
  `cache`, `clean`; JSON output for automation.
* Licence audit with permissive/weak-copyleft/copyleft classification and
  `licenses --check` (exit code 3).
* Build reports (`dist/<name>.build.json`) with stats, warnings, native library
  rewrites and licence entries.
* A `unittest` suite (unit + end-to-end builds) and zero third-party runtime
  dependencies.
### Known limitations

* Windows dependency analysis is limited to DLLs shipped inside packages; no PE
  import-table parsing yet.
* macOS bundles are ad-hoc signed; notarization is manual, and onefile payloads
  cannot be re-signed after appending.
* No cross-compilation: build on the target OS/architecture with a matching
  Python minor version.
* No UPX, splash screens, Windows icon embedding or Linux `.desktop` files.
* Zipimport/egg-based distributions are not supported.
