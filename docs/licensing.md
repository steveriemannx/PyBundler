# Licensing

PyBundler exists partly because licence provenance is often an afterthought in
packaging. This page documents what PyBundler itself is licensed under, which
components it ships, and how to audit a build.

> This is engineering documentation, not legal advice. Review the output of
> `pybundler licenses` with your own counsel before distributing.

## PyBundler itself

* **License:** BSD 2-Clause (see [LICENSE](../LICENSE)).
* **Copyright:** 2026, steveriemannx and PyBundler contributors.
* **Runtime dependencies:** none. Every import in `src/pybundler` resolves to
  CPython's standard library; `tests/test_zero_dependencies.py` enforces this.
* **Build-time dependencies:** `setuptools>=77` (MIT) via PEP 517, plus a C
  compiler for the launcher.
* **Development/test dependencies:** none (the suite uses `unittest`).

This differs deliberately from the main alternatives: PyInstaller is
GPL-2.0-or-later with a special exception for the bootloader, Nuitka is
AGPL-3.0, and PyOxidizer is MPL-2.0. Those tool licences do not usually change
the licence of your program, but some organisations prefer not to add copyleft
or weak-copyleft tooling to their build pipeline at all. PyBundler aims to make
that decision trivial.

## What a bundle contains

| Component | Typical licence | Where it lands |
| --- | --- | --- |
| Your application code | yours | `site/` |
| Bundled CPython runtime | PSF-2.0 | `runtime/python/` |
| Python packages | their own (MIT/BSD/Apache/…) | `site/` |
| Package metadata | same as package | `site/*.dist-info/` |
| Native libraries copied from the build machine | their own | `lib/` |
| zlib (only if compression is enabled) | Zlib | linked into the launcher |
| Tcl/Tk (only with `tkinter`) | Tcl/Tk licence (BSD-like) | `runtime/python/lib/tcl*`, `tk*` |

Nothing is relicensed. PyBundler does not modify third-party Python code, and
`strip_sources` only removes `.py` files after compiling them to `.pyc`.

## Automatic collection

Every build writes:

```
licenses/
  pybundler/LICENSE              BSD-2 text
  application/...                your project's LICENSE/COPYING (if present)
  python/LICENSE                 CPython/PSF licence (when present in the runtime)
  python/<dist>-<version>/...    every LICENSE/COPYING/NOTICE/AUTHORS file found
                                 in a bundled distribution's dist-info directory
  THIRD-PARTY.json               machine readable list with licence + class
  NATIVE-LIBRARIES.json          shared libraries copied by the native fixup
  README.md                      short explanation for recipients
```

`THIRD-PARTY.json` entries look like:

```json
{
  "name": "numpy",
  "version": "2.4.4",
  "license": "BSD-3-Clause AND 0BSD AND MIT AND Zlib AND CC0-1.0",
  "classification": "permissive",
  "files": ["licenses/python/numpy-2.4.4/LICENSE.txt"]
}
```

If a distribution ships no licence file (some distro packages strip it),
PyBundler still writes `LICENSE-NOT-FOUND.txt` with the metadata it has, so the
gap is visible rather than silent.

## The audit

```bash
pybundler licenses dist/myapp          # table of every distribution
pybundler licenses dist/myapp --check  # exit 3 if anything is non-permissive
pybundler licenses dist/myapp --json   # machine readable
pybundler licenses ./pybundler.toml    # audit the project's current environment
```

Classification is a small SPDX-aware heuristic:

| Class | Examples | Meaning |
| --- | --- | --- |
| `permissive` | MIT, BSD-2/3-Clause, Apache-2.0, ISC, PSF-2.0, Zlib, 0BSD, Unlicense | allow-list |
| `weak-copyleft` | MPL-2.0, EPL-2.0, LGPL-2.1/3.0, CDDL | file/library-level obligations |
| `copyleft` | GPL, AGPL, SSPL, EUPL | distribution obligations |
| `unknown` | proprietary, missing metadata | needs manual review |

Expressions with `AND`/`OR` are evaluated conservatively: a strong copyleft
anywhere in the expression is flagged. SPDX identifiers are compared
case-insensitively and human readable names such as
`"Mozilla Public License 2.0 (MPL 2.0)"` are recognised.

### Using it in CI

```yaml
- run: pybundler build --output-dir dist
- run: pybundler licenses dist/myapp --check
```

A failure means "a human should look at this", not necessarily "you are doing
something illegal" — for example MPL-2.0 files may be redistributed unmodified
with their licence text, which PyBundler does.

## Native libraries

Wheels usually contain their own `.dylib`/`.so` files, which are copied together
with the package and whose licences are covered by the distribution's metadata.
Libraries linked from *outside* the environment (Homebrew's OpenBLAS, GCC's
`libgfortran`, Intel's `libomp`, …) are copied by the native fixup and recorded
in `licenses/NATIVE-LIBRARIES.json` with their original path. Their licences are
not part of Python package metadata, so PyBundler emits a build warning asking
you to verify them.

Typical examples:

| Library | Licence |
| --- | --- |
| OpenBLAS | BSD-3-Clause |
| libgfortran / libgcc_s / libquadmath | GPL-3.0-with-GCC-exception |
| libomp (LLVM) | Apache-2.0 WITH LLVM-exception |
| Tcl/Tk | BSD-style |

Build systems that link GPL runtimes may impose obligations; the report makes
that visible.

## What recipients get

Ship the whole `licenses/` directory next to the executable (it is inside
`dist/<name>/` for onedir and inside the extracted payload for onefile). For a
onefile executable, run `pybundler info dist/myapp` to print the embedded
manifest; the licence files themselves are in the extraction cache and in the
build report.

## FAQ

**Does PyBundler relicense my code?** No.

**Am I allowed to sell bundles made with PyBundler?** Yes; BSD-2-Clause only
requires keeping its copyright notice and licence text (which PyBundler writes
into `licenses/pybundler/LICENSE`).

**Why is the runtime PSF-2.0 and not BSD?** CPython is distributed under the
PSF licence, which is permissive and compatible with BSD/MIT/Apache projects.

**What about the python-build-standalone build itself?** The project is
MIT-licensed; the binaries it publishes contain CPython and bundled third-party
libraries, whose licences are included in its distributions. PyBundler copies
the runtime's own licence file into `licenses/python/LICENSE` when present.

**Are hooks licenses relevant?** Hooks are build-time code; they are not shipped
into the bundle.
