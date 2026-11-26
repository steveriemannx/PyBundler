# Comparison

PyBundler is not trying to win a feature checklist. It targets teams that want a
predictable, permissively licensed bundler with no supply-chain surface of its
own. This page is a fair summary of where it fits and where it does not.

## At a glance

| | PyBundler | PyInstaller | Nuitka | cx_Freeze | shiv / pex | PyOxidizer |
| --- | --- | --- | --- | --- | --- | --- |
| **Licence** | BSD-2-Clause | GPL-2.0-or-later + bootloader exception | AGPL-3.0 | PSF-2.0 | BSD / Apache-2.0 | MPL-2.0 |
| **Runtime dependencies** | none | several (altgraph, packaging, pefile, …) | zstandard etc. | several | several | Rust toolchain |
| **Host platforms** | macOS, Linux, Windows, FreeBSD¹ | macOS, Linux, Windows, FreeBSD | macOS, Linux, Windows, FreeBSD | macOS, Linux, Windows | any (needs Python) | macOS, Linux, Windows |
| **Approach** | interpreter + payload bundle | interpreter + bootloader | Python → C compilation | interpreter + freezer | zipapp on system Python | Rust embedding |
| **Standalone output** | yes (ships CPython) | yes | yes | yes | no (needs Python) | yes |
| **onedir** | yes | yes | yes | yes | n/a | n/a |
| **onefile** | yes (zlib, cached extraction) | yes | yes (onefile mode) | yes | zipapp | yes |
| **Import analysis** | bytecode `IMPORT_NAME` scan | bytecode scan + hooks | full AST/compiler | module finder + hooks | none (runtime import) | static + runtime |
| **Hooks ecosystem** | small, user-extensible | large | recipes | hooks | n/a | limited |
| **Stdlib size strategy** | keep all (safe) | prune to used modules | compile used modules | prune | none | configurable |
| **Cross-compilation** | no (build on target OS) | limited | yes (with C toolchain) | limited | n/a | cross |
| **Startup performance** | normal CPython | normal CPython | compiled, often faster | normal | normal | normal |
| **Licence audit of bundled deps** | built in | manual | manual | manual | manual | manual |
| **Maturity** | beta (2026) | very mature | mature | mature | mature | low activity |

¹ FreeBSD uses the system Python from ports/pkg because python-build-standalone
publishes no FreeBSD binaries; macOS/Linux/Windows use a bundled relocatable
CPython.

Licence and platform data was current at the time of writing; verify upstream
projects before making decisions.

## Why PyBundler instead of PyInstaller?

* **Licence hygiene.** BSD-2-Clause source, zero runtime dependencies, and no
  GPL tooling in the pipeline. PyInstaller's bootloader exception makes the GPL
  a non-issue for most users, but some legal teams prefer avoiding the question.
* **Small dependency surface.** The bundler is ~4k lines of stdlib Python plus a
  single C file. You can audit it in an afternoon.
* **No hook archaeology.** Because whole package trees are copied, packages
  that dynamically import their own submodules usually work without a hook.
* **Licence reporting is a first-class output**, including native libraries
  copied from the build machine.
* **Transparent formats.** The onefile container, `pybundler.json` and the
  launcher are documented and small enough to understand.

## Why PyInstaller instead of PyBundler?

* **Maturity and coverage.** PyInstaller has years of fixes for obscure
  packages, platforms and edge cases; PyBundler is beta.
* **Size.** PyInstaller prunes the stdlib and supports UPX; PyBundler bundles
  more and does not compress `onedir` output.
* **Ecosystem.** Hundreds of community hooks, extensive documentation and
  Stack Overflow coverage.
* **Cross-platform confidence.** Windows and Linux support in PyInstaller is
  battle tested; PyBundler's Windows path is the least exercised.
* **Notarization/codesigning workflows** are more complete upstream.

## Why Nuitka?

Nuitka is a compiler: it turns your Python into C, which can improve runtime
performance and produce harder-to-extract binaries. It is also **AGPL-3.0**
licensed (the licence covers the compiler, not your program), which is a
blocker for some build pipelines. PyBundler does not compile your code; it
bundles a CPython runtime. If raw speed or source protection is the goal,
Nuitka is the better tool; if a permissive tool licence is a requirement,
Nuitka is not an option.

## Why shiv/pex?

Those tools create zipapps that still require a compatible interpreter on the
target machine. PyBundler bundles the interpreter, so it works on machines
without Python — at the cost of size.

## Why PyOxidizer?

PyOxidizer embeds Python in a Rust binary with aggressive static linking. It is
powerful but requires a Rust toolchain, is MPL-2.0, and has seen little
maintenance in recent years. PyBundler deliberately stays a thin, auditable
Python program.

## Migration notes from PyInstaller

| PyInstaller | PyBundler |
| --- | --- |
| `pyinstaller app.spec` | `pybundler build pybundler.toml` |
| `--onedir` / `--onefile` | `--onedir` / `--onefile` (same names) |
| `--add-data "src:dst"` | `--add-data "src:dst"` (same syntax, host path separator) |
| `--hidden-import` | `--hidden-import` |
| `--exclude-module` | `--exclude-module` |
| `--windowed` | `--windowed` (`.app` on macOS) |
| `.spec` Python hooks | `pybundler.toml` + Python hook files |
| `sys._MEIPASS` | supported |
| `sys.frozen` | supported |
| `--collect-all pkg` | `packages = ["pkg"]` or a hook with `ctx.collect_all()` |
| `--paths` | put the path on `sys.path` of the build environment |
| `--runtime-tmpdir` | set `PYBUNDLER_CACHE_DIR` at runtime |
| UPX | not supported yet |

PyInstaller `.spec` files are Python programs and are not compatible; the
translation is mechanical for common options.
