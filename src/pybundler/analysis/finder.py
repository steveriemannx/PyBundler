"""Import resolution against a concrete ``sys.path``.

The finder mirrors CPython's ``FileFinder`` closely enough for bundling:

* regular modules and packages (``.py``),
* extension modules for the target interpreter (``EXT_SUFFIX`` and friends),
* namespace packages (a directory without ``__init__.py``),
* built-in and frozen modules reported by the interpreter probe.

It deliberately does not support zip imports / eggs; PyBundler warns about
those instead of silently producing a broken bundle.
"""

from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path

from .distinfo import DistInfo, DistributionIndex

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class SearchEntry:
    path: Path
    kind: str  # "project" | "site" | "stdlib" | "runtime"


@dataclass(frozen=True)
class FoundModule:
    name: str
    kind: str  # "module" | "package" | "namespace" | "extension" | "builtin" | "frozen"
    path: Path
    is_package: bool
    root: Path
    rel: Path
    dist: DistInfo | None = None
    category: str = "project"  # "stdlib" | "thirdparty" | "project"

    @property
    def top_level(self) -> str:
        return self.name.split(".", 1)[0]

    @property
    def is_stdlib(self) -> bool:
        return self.kind in ("builtin", "frozen")

    @property
    def directory(self) -> Path:
        return self.path if self.path.is_dir() else self.path.parent

    def describe(self) -> dict[str, object]:
        return {
            "name": self.name,
            "kind": self.kind,
            "path": str(self.path),
            "package": self.is_package,
            "dist": self.dist.name if self.dist else None,
        }


class ModuleFinder:
    """Resolve dotted module names using an ordered list of search entries."""

    def __init__(
        self,
        entries: Iterable[SearchEntry],
        *,
        ext_suffixes: Iterable[str] = (),
        builtin_modules: Iterable[str] = (),
        distributions: DistributionIndex | None = None,
        stdlib_dirs: Iterable[Path] = (),
        site_dirs: Iterable[Path] = (),
        warnings: list[str] | None = None,
    ) -> None:
        self.entries = [entry for entry in entries if entry.path.is_dir()]
        self.ext_suffixes = tuple(sorted(set(ext_suffixes) - {""}, key=len, reverse=True))
        self.builtin_modules = frozenset(builtin_modules)
        self.distributions = distributions or DistributionIndex()
        self.stdlib_dirs = tuple(
            dict.fromkeys(
                candidate
                for path in stdlib_dirs
                if path and path.exists()
                for candidate in (path, path.resolve())
            )
        )
        self.site_dirs = tuple(
            dict.fromkeys(
                candidate
                for path in site_dirs
                if path and path.exists()
                for candidate in (path, path.resolve())
            )
        )
        self.warnings = warnings if warnings is not None else []
        self._resolved_entries = [(entry, entry.path.resolve()) for entry in self.entries]

    # -- classification -------------------------------------------------
    def classify_path(self, path: Path) -> str:
        def inside(directories: tuple[Path, ...]) -> bool:
            for candidate in (path, path.resolve()):
                for directory in directories:
                    try:
                        candidate.relative_to(directory)
                        return True
                    except ValueError:
                        continue
            return False

        if inside(self.stdlib_dirs):
            return "stdlib"
        if inside(self.site_dirs):
            return "thirdparty"
        return "project"

    # -- resolution -----------------------------------------------------
    def find(self, name: str, *, importer: str | None = None) -> FoundModule | None:
        if not name:
            return None
        if name in self.builtin_modules:
            return FoundModule(
                name=name,
                kind="builtin",
                path=Path(f"<builtin:{name}>"),
                is_package=False,
                root=Path(),
                rel=Path(),
            )
        parts = name.split(".")
        if not all(_IDENTIFIER.match(part) for part in parts):
            return None
        relative = Path(*parts)
        for entry, resolved_entry in self._resolved_entries:
            found = self._find_in_entry(name, relative, entry, resolved_entry)
            if found is not None:
                return found
        return None

    def _find_in_entry(
        self, name: str, relative: Path, entry: SearchEntry, resolved_entry: Path
    ) -> FoundModule | None:
        base = entry.path / relative
        # Extension module: <name><EXT_SUFFIX>
        for suffix in self.ext_suffixes:
            candidate = Path(str(base) + suffix)
            if candidate.is_file():
                return self._make(name, "extension", candidate, False, entry)
        # Regular module
        module = base.with_suffix(".py")
        if module.is_file():
            return self._make(name, "module", module, False, entry)
        # Package
        if base.is_dir():
            for init_name in ("__init__.py",):
                init = base / init_name
                if init.is_file():
                    return self._make(name, "package", init, True, entry)
            for suffix in self.ext_suffixes:
                init = base / f"__init__{suffix}"
                if init.is_file():
                    return self._make(name, "extension", init, True, entry)
            if any(part for part in relative.parts):
                # Namespace package portion. Keep the directory so that data
                # files can be discovered; submodules are added when imported.
                return self._make(name, "namespace", base, True, entry)
        # A module may live in a package directory without a matching parent
        # path when a namespace package spans several roots; `base` already
        # accounts for the full dotted path so nothing else to try.
        return None

    def _make(
        self, name: str, kind: str, path: Path, is_package: bool, entry: SearchEntry
    ) -> FoundModule:
        try:
            rel = path.relative_to(entry.path)
        except ValueError:  # pragma: no cover - defensive
            rel = Path(path.name)
        classification = self.classify_path(path)
        if classification == "thirdparty":
            dist = self.distributions.resolve(name.split(".", 1)[0])
        elif classification == "stdlib":
            dist = None
        else:
            dist = None
        if entry.kind == "site" and classification == "project":
            classification = "thirdparty"
        return FoundModule(
            name=name,
            kind=kind,
            path=path,
            is_package=is_package,
            root=entry.path,
            rel=rel,
            dist=dist,
            category=classification,
        )

    # -- utilities ------------------------------------------------------
    def package_dir(self, name: str) -> Path | None:
        found = self.find(name)
        if found is None:
            return None
        if found.kind in ("package", "namespace"):
            return found.path if found.path.is_dir() else found.path.parent
        if found.kind == "extension" and found.is_package:
            return found.path.parent
        return found.path.parent if found.kind in ("module", "extension") else None

    def iter_submodules(self, package: str) -> Iterator[str]:
        """Yield dotted names of every importable module inside *package*."""
        directory = self.package_dir(package)
        if directory is None or not directory.is_dir():
            return
        for path in sorted(directory.rglob("*")):
            if not path.is_file():
                continue
            if "__pycache__" in path.parts:
                continue
            relative = path.relative_to(directory)
            if relative.parts and relative.parts[0].startswith("."):
                continue
            if path.suffix == ".py":
                parts = list(relative.with_suffix("").parts)
                if parts and parts[-1] in ("__init__",):
                    parts = parts[:-1]
                if parts and all(_IDENTIFIER.match(part) for part in parts):
                    yield ".".join([package, *parts])
                continue
            stripped = _strip_extension(path, self.ext_suffixes)
            if stripped is not None and _IDENTIFIER.match(stripped):
                parts = list(relative.parent.parts)
                if parts and parts != [Path(".")]:
                    yield ".".join([package, *parts, stripped])
                else:
                    yield f"{package}.{stripped}"


def _strip_extension(path: Path, suffixes: Iterable[str]) -> str | None:
    name = path.name
    for suffix in suffixes:
        if name.endswith(suffix):
            return name[: -len(suffix)]
    if name.endswith(".py"):
        return name[:-3]
    for suffix in (".so", ".pyd", ".dylib"):
        if name.endswith(suffix):
            return name[: -len(suffix)]
    return None


def matches_exclusion(name: str, patterns: Iterable[str]) -> bool:
    """Return True when *name* matches any exclusion pattern.

    Patterns are matched with :mod:`fnmatch`, and a bare top-level name also
    excludes its submodules (``tkinter`` excludes ``tkinter.filedialog``).
    """
    for pattern in patterns:
        if not pattern:
            continue
        if fnmatch.fnmatchcase(name, pattern):
            return True
        if not any(char in pattern for char in "*?[]") and (
            name == pattern or name.startswith(pattern + ".")
        ):
            return True
    return False
