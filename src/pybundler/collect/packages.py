"""Copying importable trees and distribution metadata into the bundle.

Third-party packages are copied *whole* (minus caches, tests and headers)
rather than file-by-file.  This is the single most important robustness
decision in PyBundler: package data, lazily imported submodules, plugin
directories and dynamically loaded extension modules all keep working without
per-package recipes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Iterable

from ..analysis.finder import matches_exclusion
from ..analysis.graph import Analysis
from ..build.plan import Payload
from ..fsutil import human_size
from ..log import Logger

_CACHE_PARTS = {"__pycache__", ".git", ".hg", ".svn", ".mypy_cache", ".pytest_cache", ".ruff_cache"}
_HEADER_SUFFIXES = {".h", ".hpp", ".pxd", ".pyx", ".c", ".cc", ".cpp", ".pyi"}


def _dotted_name(package: str, relative: Path) -> str:
    parts = list(relative.parts)
    if parts and parts[-1].endswith(".py"):
        parts[-1] = parts[-1][:-3]
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join([package, *parts]) if parts else package


def plan_site(
    analysis: Analysis,
    payload: Payload,
    *,
    include_tests: bool = False,
    exclusions: Iterable[str] = (),
    logger: Logger | None = None,
) -> list[str]:
    """Add all analysed modules, package trees and dist-info to *payload*."""
    warnings: list[str] = []
    excluded = tuple(exclusions)
    copied_dirs: list[Path] = []
    size_before = payload.total_size()

    def package_filter(package: str, root: Path):
        def keep(relative: Path) -> bool:
            parts = relative.parts
            if not parts:
                return True
            if any(part in _CACHE_PARTS for part in parts):
                return False
            if relative.suffix in (".pyc", ".pyo"):
                return False
            if not include_tests and any(part in ("tests", "test") for part in parts[:-1]):
                return False
            if relative.suffix in _HEADER_SUFFIXES and "include" in parts[:-1]:
                return False
            dotted = _dotted_name(package, relative)
            if excluded and matches_exclusion(dotted, excluded):
                return False
            return True

        return keep

    def already_copied(directory: Path) -> bool:
        return any(directory == root or directory.is_relative_to(root) for root in copied_dirs)

    for name, module in analysis.modules.items():
        if module.kind == "namespace":
            continue
        if module.kind in ("builtin", "frozen"):
            continue
        if module.kind == "package" or (module.kind == "extension" and module.is_package):
            directory = module.directory
            if already_copied(directory):
                continue
            prefix = "site"
            try:
                relative_dir = directory.relative_to(module.root)
            except ValueError:  # pragma: no cover - defensive
                relative_dir = Path(directory.name)
            dest = f"{prefix}/{relative_dir.as_posix()}"
            copied_dirs.append(directory)
            payload.add_tree(
                directory,
                dest,
                filter=package_filter(name, directory) if module.kind == "package" else None,
                origin=f"package:{name.split('.', 1)[0]}",
            )
            continue
        # Plain module or extension module.
        dest = f"site/{module.rel.as_posix()}"
        payload.add_file(module.path, dest, origin=f"module:{name.split('.', 1)[0]}")

    # Wheels place shared libraries in sibling ``*.libs`` / ``*.dylibs``
    # directories (auditwheel/delvewheel).  They are not importable packages,
    # so analysis never sees them; copy the ones belonging to bundled packages.
    top_levels = {name.split(".", 1)[0] for name in analysis.modules}
    normalized = lambda value: value.lower().replace("-", "_")

    def belongs(stem: str) -> bool:
        candidate = normalized(stem)
        for top in top_levels:
            package = normalized(top)
            if candidate == package or candidate.startswith(package + "_") or package.startswith(candidate + "_"):
                return True
        return False

    for site_dir in analysis.site_dirs:
        if not site_dir.is_dir():
            continue
        for pattern in ("*.libs", "*.dylibs"):
            for library_dir in sorted(site_dir.glob(pattern)):
                if not library_dir.is_dir():
                    continue
                stem = library_dir.name.rsplit(".", 1)[0]
                if not belongs(stem):
                    continue
                payload.add_tree(
                    library_dir,
                    f"site/{library_dir.name}",
                    filter=lambda rel: not any(part in _CACHE_PARTS for part in rel.parts),
                    origin=f"native-libs:{stem}",
                )

    # Distribution metadata is required by importlib.metadata / pkg_resources.
    for canonical, dist in analysis.distributions.items():
        dist_info = dist.dist_info
        if not dist_info.is_dir():
            continue
        dest = f"site/{dist_info.name}"
        payload.add_tree(
            dist_info,
            dest,
            filter=lambda rel: not any(part in _CACHE_PARTS for part in rel.parts),
            origin=f"metadata:{canonical}",
        )

    # Explicit data files from the spec / hooks. Destinations are already fully
    # resolved by the caller (config parsing / hooks), so they are used as-is.
    for data in analysis.data:
        if data.source.is_dir():
            payload.add_tree(data.source, data.dest, origin=f"data:{data.origin}")
        else:
            payload.add_file(data.source, data.dest, origin=f"data:{data.origin}")

    for binary in analysis.binaries:
        payload.add_file(binary.source, binary.dest, origin=f"binary:{binary.origin}")

    if logger is not None:
        logger.detail(
            f"site payload: {len(analysis.modules)} modules, "
            f"{len(analysis.distributions)} distributions, "
            f"{human_size(payload.total_size() - size_before)}"
        )
    return warnings
