#!/usr/bin/env python3
"""Audit the licences of PyBundler's own dependency tree.

PyBundler has no runtime dependencies, so this script is mostly a guard against
regressions: it parses ``pyproject.toml``, fails if any runtime requirement is
declared, and reports the licences of the optional build/test tooling it knows
about.

Run from the repository root:

    python3 tools/check_licenses.py
"""

from __future__ import annotations

import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

KNOWN = {
    "setuptools": ("MIT", "permissive"),
    "wheel": ("MIT", "permissive"),
    "pip": ("MIT", "permissive"),
}

# Development-only tools; PyBundler itself ships none of these.
DEV_ALLOWED = {
    "setuptools": "MIT",
}


def main() -> int:
    document = tomllib.loads((ROOT / "pyproject.toml").read_text("utf-8"))
    project = document.get("project", {})
    build = document.get("build-system", {})

    problems: list[str] = []
    runtime = project.get("dependencies") or []
    if runtime:
        problems.append(f"runtime dependencies declared: {runtime}")

    optional = project.get("optional-dependencies") or {}
    for group, requirements in optional.items():
        for requirement in requirements:
            name = requirement.split(";")[0].strip().split("[")[0]
            name = name.split(" ")[0].split("=")[0].split(">")[0].split("<")[0].lower()
            if name not in DEV_ALLOWED:
                problems.append(
                    f"optional dependency {name!r} in group {group!r} has no licence review"
                )

    for requirement in build.get("requires", []):
        name = requirement.split(";")[0].strip().split("[")[0]
        name = name.split(" ")[0].split("=")[0].split(">")[0].split("<")[0].lower()
        if name not in KNOWN:
            problems.append(f"build requirement {name!r} has no licence review")

    licence = project.get("license")
    if licence not in ("BSD-2-Clause", {"text": "BSD-2-Clause"}):
        problems.append(f"unexpected project licence: {licence!r}")

    print("PyBundler dependency licence audit")
    print(f"  project licence:      {licence}")
    print(f"  runtime dependencies: {len(runtime)}")
    for name, (expression, classification) in sorted(KNOWN.items()):
        print(f"  build tool {name:<12} {expression} [{classification}]")

    if problems:
        print("\nFAILED:")
        for problem in problems:
            print(f"  - {problem}")
        return 1
    print("\nOK: no unreviewed dependencies")
    return 0


if __name__ == "__main__":
    sys.exit(main())
