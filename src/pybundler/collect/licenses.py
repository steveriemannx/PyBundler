"""Licence collection and policy audit for bundles.

Shipping Python code means shipping other people's licences.  PyBundler always
copies the licence files of every distribution it bundles, plus the licence of
the Python runtime itself, into ``licenses/`` and writes a machine readable
``THIRD-PARTY.json`` manifest.

The audit is advisory: it flags licences that are not in the permissive
allow-list so that ``pybundler licenses --check`` can be used in CI.  It is not
legal advice.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from ..analysis.graph import Analysis
from ..build.plan import Payload
from ..fsutil import write_json
from ..log import Logger
from ..runtimes import Runtime

PYBUNDLER_LICENSE = """BSD 2-Clause License

Copyright (c) 2026, steveriemannx and PyBundler contributors
All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.

2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE ARE
DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE
FOR ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY,
OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
"""

PERMISSIVE_IDS = {
    "MIT",
    "MIT-0",
    "BSD-2-Clause",
    "BSD-3-Clause",
    "BSD-4-Clause",
    "0BSD",
    "ISC",
    "Apache-2.0",
    "PSF-2.0",
    "Python-2.0",
    "Zlib",
    "Unlicense",
    "CC0-1.0",
    "BSD-3-Clause-Clear",
    "BlueOak-1.0.0",
}

WEAK_COPYLEFT_IDS = {
    "MPL-2.0",
    "EPL-2.0",
    "CDDL-1.0",
    "LGPL-2.1-only",
    "LGPL-3.0-only",
    "LGPL-2.1-or-later",
    "LGPL-3.0-or-later",
}

_STRONG_COPYLEFT = re.compile(r"\b(A?GPL|SSPL|CPL|EUPL)\b", re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9.\-+]+")


def classify_license(expression: str | None) -> str:
    """Return ``permissive``, ``weak-copyleft``, ``copyleft`` or ``unknown``."""
    if not expression:
        return "unknown"
    text = expression.strip()
    if not text or text.lower() in ("unknown", "none"):
        return "unknown"
    lowered = text.lower()
    # SPDX identifiers are compared case-insensitively.
    tokens = {token for token in _TOKEN.findall(text) if token.upper() == token or "-" in token}
    upper = {token.upper() for token in tokens}
    if _STRONG_COPYLEFT.search(text) or "gnu general public license" in lowered:
        return "copyleft"
    if (
        upper & {identifier.upper() for identifier in WEAK_COPYLEFT_IDS}
        or "mozilla public license" in lowered
        or "eclipse public license" in lowered
        or "gnu lesser general public" in lowered
        or "common development and distribution" in lowered
    ):
        return "weak-copyleft"
    for identifier in PERMISSIVE_IDS:
        if identifier.upper() in upper:
            return "permissive"
    if any(
        word in lowered
        for word in (
            "mit",
            "bsd",
            "apache",
            "isc",
            "zlib",
            "python software foundation",
            "public domain",
            "unlicense",
        )
    ):
        return "permissive"
    return "unknown"


@dataclass
class LicenseEntry:
    name: str
    version: str
    license: str
    classification: str
    files: list[str] = field(default_factory=list)


@dataclass
class LicenseReport:
    entries: list[LicenseEntry] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)

    def non_permissive(self) -> list[LicenseEntry]:
        return [entry for entry in self.entries if entry.classification != "permissive"]


def plan_licenses(
    payload: Payload,
    analysis: Analysis,
    runtime: Runtime,
    *,
    project_root: Path | None = None,
    logger: Logger | None = None,
) -> LicenseReport:
    report = LicenseReport()
    payload.add_text(PYBUNDLER_LICENSE, "licenses/pybundler/LICENSE", origin="licenses")

    project_root = project_root or analysis.project_root
    if project_root is not None:
        for candidate in sorted(project_root.glob("LICENSE*")) + sorted(project_root.glob("COPYING*")):
            if candidate.is_file() and candidate.stat().st_size < 1_000_000:
                payload.add_file(
                    candidate, f"licenses/application/{candidate.name}", origin="licenses"
                )
                break

    runtime_license = _find_runtime_license(runtime.root)
    if runtime_license is not None:
        payload.add_file(runtime_license, "licenses/python/LICENSE", origin="licenses")
    report.entries.append(
        LicenseEntry(
            name="CPython",
            version=runtime.version,
            license="PSF-2.0",
            classification="permissive",
            files=["licenses/python/LICENSE"] if runtime_license else [],
        )
    )

    manifest: list[dict[str, object]] = [
        {
            "name": "CPython",
            "version": runtime.version,
            "license": "PSF-2.0",
            "classification": "permissive",
            "source": "python-build-standalone",
        },
        {
            "name": "PyBundler",
            "version": _pybundler_version(),
            "license": "BSD-2-Clause",
            "copyright": "Copyright (c) 2026, steveriemannx and PyBundler contributors",
            "classification": "permissive",
            "source": "https://github.com/pybundler/pybundler",
        },
    ]

    for canonical, dist in sorted(analysis.distributions.items()):
        expression = dist.license_summary
        classification = classify_license(expression)
        files: list[str] = []
        license_files = dist.license_files()
        base = f"licenses/python/{canonical}-{dist.version}"
        for source in license_files:
            relative = source.relative_to(dist.dist_info)
            dest = f"{base}/{relative.as_posix()}"
            payload.add_file(source, dest, origin="licenses")
            files.append(dest)
        if not license_files:
            note = (
                f"{dist.name} {dist.version}\n"
                f"License metadata: {expression}\n\n"
                "The installed distribution did not ship a licence file. "
                "Inspect the project's own repository for the full text.\n"
            )
            dest = f"{base}/LICENSE-NOT-FOUND.txt"
            payload.add_text(note, dest, origin="licenses")
            files.append(dest)
        entry = LicenseEntry(
            name=dist.name,
            version=dist.version,
            license=expression,
            classification=classification,
            files=files,
        )
        report.entries.append(entry)
        if classification != "permissive":
            report.warnings.append(
                f"{dist.name} {dist.version} is licensed under {expression!r} "
                f"({classification}); review whether you may redistribute it"
            )
        manifest.append(
            {
                "name": dist.name,
                "version": dist.version,
                "license": expression,
                "classification": classification,
                "files": files,
            }
        )

    payload.add_text(
        "# Third party licences\n\n"
        "This bundle was produced by PyBundler, which is licensed under the\n"
        "BSD 2-Clause licence (see `licenses/pybundler/LICENSE`).\n\n"
        "Python itself is distributed under the Python Software Foundation\n"
        "Licence 2.0. Third-party packages keep their own licences; the\n"
        "machine readable manifest is `THIRD-PARTY.json`.\n",
        "licenses/README.md",
        origin="licenses",
    )
    payload.add_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n",
        "licenses/THIRD-PARTY.json",
        origin="licenses",
    )

    if logger is not None:
        for warning in report.warnings:
            logger.warn(warning)
    return report


def _find_runtime_license(root: Path) -> Path | None:
    candidates = [
        root / "LICENSE",
        root / "LICENSE.txt",
        root / "python" / "LICENSE",
        root / "share" / "doc" / "python" / "LICENSE",
        root / "python.LICENSE",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    patterns = (
        "LICENSE*",
        "COPYING*",
        "lib/python*/LICENSE*",
        "lib/python*/COPYING*",
        "Lib/LICENSE*",
        "Lib/COPYING*",
        "python/LICENSE*",
    )
    for pattern in patterns:
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file() and candidate.stat().st_size < 4_000_000:
                return candidate
    return None


def write_license_report(root: Path, report: LicenseReport) -> None:
    """Write a standalone audit report next to an existing bundle."""
    write_json(
        root / "licenses" / "AUDIT.json",
        {
            "entries": [
                {
                    "name": entry.name,
                    "version": entry.version,
                    "license": entry.license,
                    "classification": entry.classification,
                    "files": entry.files,
                }
                for entry in report.entries
            ],
            "warnings": report.warnings,
        },
    )


def _pybundler_version() -> str:
    from .. import __version__

    return __version__
