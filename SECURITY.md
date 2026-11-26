# Security policy

## Supported versions

PyBundler is pre-1.0. Security fixes land on the latest `0.2.x` release and
`main`.

## Reporting a vulnerability

Please **do not** open a public issue. Use GitHub's private vulnerability
reporting ("Security" → "Report a vulnerability") on
`https://github.com/pybundler/pybundler`, or contact the maintainers listed in
`pyproject.toml`.

Include the PyBundler version, platform, a description, and a reproducer if
possible. We aim to acknowledge within 72 hours.

## Threat model

PyBundler runs at build time on a developer machine and produces an executable
that runs with the invoking user's privileges. Relevant boundaries:

* **Untrusted bundle contents.** Onefile extraction happens in
  `launcher.c`. Every entry path is validated (no absolute paths, no `..`, no
  backslashes, no drive letters) and extraction uses a temporary directory plus
  an atomic rename. The Python reader used by tooling performs the same checks.
* **Runtime downloads.** Interpreter archives come from the pinned
  python-build-standalone release over HTTPS and are verified against the
  SHA-256 digest published by the GitHub API before extraction. Extraction
  rejects absolute paths, `..` components, device nodes and unsafe symlinks.
  On **FreeBSD** there are no PBS builds: the runtime is the system interpreter
  installed with pkg/ports, so its provenance, patches and updates are managed
  by the OS package manager rather than by PyBundler.
* **Build inputs.** Spec files are code for the build (hook files are imported).
  Do not build projects you do not trust, just as you would not run their tests.
* **Environment overrides.** `PYBUNDLER_CACHE_DIR`, `PYBUNDLER_TMPDIR` and
  `CC` affect where files are written and which compiler is invoked. Bundles
  honour `PYBUNDLER_CACHE_DIR` for onefile extraction; treat it like any other
  writable cache location.
* **Code signing.** Bundles are not signed with an identity by PyBundler
  (ad-hoc signatures only). Notarization and Developer ID signing are the
  distributor's responsibility.
* **Privileges.** The launcher does not elevate privileges, does not set
  setuid/setgid bits, and does not execute anything from the bundle other than
  the bundled interpreter and application code.

Out of scope: vulnerabilities in CPython, python-build-standalone binaries,
third-party packages you choose to bundle, and the security of the application
itself. Report those upstream; the bundled licence manifests tell you what is
inside.

## Hardening tips

* Pin `pbs_tag` in the spec and review python-build-standalone release notes
  before bumping.
* Build from a clean virtualenv and commit a lockfile for dependencies.
* Run `pybundler licenses --check` in CI.
* Distribute onedir bundles when you need code signing/notarization; onefile
  payloads cannot be re-signed after appending.
* Set restrictive permissions on the onefile extraction cache directory if the
  application handles sensitive data.
