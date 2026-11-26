"""Import graph construction: the heart of reproducible bundling.

The graph walker resolves every import it can see, records which files carry
them, and then lets hooks add the pieces that static analysis cannot know.
Standard-library modules are recorded but not copied: the bundle ships a
complete stdlib and relies on CPython's own resolver for those.
"""

from __future__ import annotations

import re
import sys
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from ..config import EntryPoint
from ..errors import AnalysisError
from ..log import Logger
from .bytecode import ImportStmt, dynamic_imports, imports_from_code
from .distinfo import DistInfo, scan_distributions
from .finder import FoundModule, ModuleFinder, SearchEntry, matches_exclusion
from .hooks import HookRegistry

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class DataFile:
    source: Path
    dest: str
    origin: str = "config"


@dataclass(frozen=True)
class BinaryFile:
    source: Path
    dest: str
    origin: str = "config"


@dataclass(frozen=True)
class MissingImport:
    name: str
    importer: str
    soft: bool = False


@dataclass(frozen=True)
class EntryResolution:
    mode: str  # "script" | "module" | "callable"
    module: str | None
    function: str | None
    script_source: Path | None
    script_dest: str | None
    search_root: Path


@dataclass
class Analysis:
    entry: EntryResolution
    modules: dict[str, FoundModule]
    distributions: dict[str, DistInfo]
    data: list[DataFile]
    binaries: list[BinaryFile]
    missing: list[MissingImport]
    warnings: list[str]
    features: frozenset[str]
    project_root: Path
    site_dirs: tuple[Path, ...]
    stdlib_dirs: tuple[Path, ...]
    stdlib_seen: frozenset[str] = frozenset()

    @property
    def third_party(self) -> dict[str, FoundModule]:
        return {name: mod for name, mod in self.modules.items() if mod.category == "thirdparty"}

    @property
    def local_modules(self) -> dict[str, FoundModule]:
        return {name: mod for name, mod in self.modules.items() if mod.category == "project"}

    def stats(self) -> dict[str, object]:
        packages = {
            mod.dist.canonical
            for mod in self.modules.values()
            if mod.dist is not None
        }
        return {
            "modules": len(self.modules),
            "third_party_modules": len(self.third_party),
            "local_modules": len(self.local_modules),
            "distributions": len(packages),
            "stdlib_modules_seen": len(self.stdlib_seen),
            "data_files": len(self.data),
            "binaries": len(self.binaries),
            "missing_imports": len([m for m in self.missing if not m.soft]),
        }


# ---------------------------------------------------------------------------
# Entry point handling
# ---------------------------------------------------------------------------


def resolve_entry(entry: EntryPoint, project_root: Path) -> EntryResolution:
    """Turn the spec entry point into a runtime invocation plus a search root."""
    if entry.kind == "callable":
        if not entry.module or not entry.function:
            raise AnalysisError("callable entry point is missing module or function")
        return EntryResolution(
            mode="callable",
            module=entry.module,
            function=entry.function,
            script_source=None,
            script_dest=None,
            search_root=project_root,
        )
    if entry.kind == "module":
        if not entry.module:
            raise AnalysisError("module entry point is missing a module name")
        return EntryResolution(
            mode="module",
            module=entry.module,
            function=None,
            script_source=None,
            script_dest=None,
            search_root=project_root,
        )
    source = entry.source
    if source is None:
        raise AnalysisError(f"script entry point {entry.value!r} has no source file")
    source = source.resolve()
    if not source.is_file():
        raise AnalysisError(f"entry script not found: {source}")

    package_parts: list[str] = []
    directory = source.parent
    while (directory / "__init__.py").is_file():
        package_parts.insert(0, directory.name)
        if directory.parent == directory:
            break
        directory = directory.parent
    if package_parts:
        stem = source.stem
        module = ".".join([*package_parts, stem])
        return EntryResolution(
            mode="module",
            module=module,
            function=None,
            script_source=source,
            script_dest=None,
            search_root=directory,
        )
    return EntryResolution(
        mode="script",
        module=source.stem if _IDENTIFIER.match(source.stem) else None,
        function=None,
        script_source=source,
        script_dest=source.name,
        search_root=source.parent,
    )


# ---------------------------------------------------------------------------
# Finder construction
# ---------------------------------------------------------------------------


def _is_site_dir(path: Path) -> bool:
    return path.name in ("site-packages", "dist-packages") or path.name.endswith(".egg")


def build_finder(
    *,
    env: "EnvInfoLike",
    runtime_env: "EnvInfoLike",
    project_root: Path,
    entry_root: Path,
    extra_paths: Iterable[Path] = (),
) -> tuple[ModuleFinder, tuple[Path, ...], tuple[Path, ...]]:
    """Create a finder that mirrors the build environment's ``sys.path``."""
    site_dirs: list[Path] = []
    stdlib_dirs: list[Path] = []
    for candidate in (env.purelib, env.platlib, runtime_env.purelib, runtime_env.platlib):
        if candidate and candidate.is_dir() and candidate not in site_dirs:
            site_dirs.append(candidate)
    for entry in env.sys_path:
        if entry.is_dir() and _is_site_dir(entry) and entry not in site_dirs:
            site_dirs.append(entry)
    for candidate in (env.stdlib, env.platstdlib, runtime_env.stdlib, runtime_env.platstdlib):
        if candidate and candidate.is_dir() and candidate not in stdlib_dirs:
            stdlib_dirs.append(candidate)
    for candidate in (env.dynload, runtime_env.dynload):
        if candidate and candidate.is_dir() and candidate not in stdlib_dirs:
            stdlib_dirs.append(candidate)

    distributions = scan_distributions(site_dirs)

    entries: list[SearchEntry] = [SearchEntry(entry_root, "project")]
    if project_root != entry_root and project_root.is_dir():
        entries.append(SearchEntry(project_root, "project"))
    seen = {str(entry.path) for entry in entries}
    for path in [*env.sys_path, *extra_paths]:
        if not path.is_dir():
            continue
        key = str(path)
        if key in seen or path in (env.stdlib, env.platstdlib):
            continue
        seen.add(key)
        if _is_site_dir(path):
            kind = "site"
        elif path in (env.purelib, env.platlib):
            kind = "site"
        else:
            kind = "project" if path == project_root else "runtime"
        entries.append(SearchEntry(path, kind))

    suffixes = {env.ext_suffix, runtime_env.ext_suffix, ".so", ".pyd", ".abi3.so", ""}
    finder = ModuleFinder(
        entries,
        ext_suffixes=suffixes,
        builtin_modules=runtime_env.builtin_modules | env.builtin_modules,
        distributions=distributions,
        stdlib_dirs=stdlib_dirs,
        site_dirs=site_dirs,
    )
    return finder, tuple(site_dirs), tuple(stdlib_dirs)


class EnvInfoLike:  # pragma: no cover - typing helper, never instantiated
    purelib: Path
    platlib: Path
    stdlib: Path
    platstdlib: Path
    dynload: Path
    sys_path: tuple[Path, ...]
    ext_suffix: str
    builtin_modules: frozenset[str]


# ---------------------------------------------------------------------------
# The walker
# ---------------------------------------------------------------------------


class Analyzer:
    def __init__(
        self,
        finder: ModuleFinder,
        *,
        project_root: Path,
        exclusions: Iterable[str] = (),
        include_tests: bool = False,
        logger: Logger | None = None,
        stdlib_names: Iterable[str] = (),
    ) -> None:
        self.finder = finder
        self.project_root = project_root
        self.exclusions = list(exclusions)
        self.include_tests = include_tests
        self.logger = logger
        self.stdlib_names = frozenset(stdlib_names) | frozenset(sys.stdlib_module_names)
        self.modules: dict[str, FoundModule] = {}
        self.distributions: dict[str, DistInfo] = {}
        self.data: list[DataFile] = []
        self.binaries: list[BinaryFile] = []
        self.missing: list[MissingImport] = []
        self.warnings: list[str] = []
        self.features: set[str] = set()
        self.stdlib_seen: set[str] = set()
        self._queue: deque[tuple[str, str, bool]] = deque()
        self._seen: dict[str, bool] = {}
        self._warned: set[str] = set()
        self._data_dests: dict[str, Path] = {}
        self._exclusions_normalized = set(self.exclusions)

    # -- diagnostics ----------------------------------------------------
    def warn(self, message: str) -> None:
        if message not in self._warned:
            self._warned.add(message)
            self.warnings.append(message)

    # -- queue ----------------------------------------------------------
    def enqueue(self, name: str, *, importer: str = "<root>", soft: bool = False) -> None:
        name = name.strip()
        if not name or name in ("__main__", "__future__"):
            return
        if name.startswith("."):
            return
        if "_pybundler_boot" in name:
            return
        if matches_exclusion(name, self._exclusions_normalized):
            return
        # Record the strongest reason (hard beats soft) and avoid duplicate work.
        previous = self._seen.get(name)
        if previous is False:
            return
        self._seen[name] = soft
        self._queue.append((name, importer, soft))
        parent = name.rpartition(".")[0]
        if parent:
            self.enqueue(parent, importer=importer, soft=True)

    def exclude(self, *names: str) -> None:
        for name in names:
            self._exclusions_normalized.add(name)
            for module in list(self.modules):
                if matches_exclusion(module, (name,)):
                    self.modules.pop(module, None)

    def has(self, module: str) -> bool:
        if module in self.modules or module in self.stdlib_seen:
            return True
        found = self.finder.find(module)
        return found is not None

    def included(self, module: str) -> bool:
        """True when *module* or its top-level package is part of the graph."""
        if module in self.modules or module in self.stdlib_seen:
            return True
        top = module.split(".", 1)[0]
        return top in self.modules or top in self.stdlib_seen

    def included_names(self) -> set[str]:
        return set(self.modules) | set(self.stdlib_seen)

    def add_data(self, source: Path, dest: str, origin: str = "config") -> None:
        key = dest.rstrip("/")
        if key in self._data_dests:
            if self._data_dests[key] != source:
                self.warn(
                    f"two different sources map to bundle path {dest!r}: "
                    f"{self._data_dests[key]} and {source}"
                )
            return
        self._data_dests[key] = source
        self.data.append(DataFile(source=source, dest=dest.replace("\\", "/"), origin=origin))

    def add_binary(self, source: Path, dest: str) -> None:
        existing = {binary.dest for binary in self.binaries}
        if dest in existing:
            return
        self.binaries.append(BinaryFile(source=source, dest=dest.replace("\\", "/")))

    def include_package(self, package: str) -> None:
        found = self.finder.find(package)
        if found is None:
            self.warn(f"package {package!r} requested but not found in the build environment")
            return
        self.enqueue(package, importer="<collect>", soft=False)
        for submodule in self.finder.iter_submodules(package):
            if not matches_exclusion(submodule, self._exclusions_normalized):
                self.enqueue(submodule, importer="<collect>", soft=True)

    # -- main loop ------------------------------------------------------
    def run(self) -> None:
        while self._queue:
            name, importer, soft = self._queue.popleft()
            if name in self.modules:
                continue
            found = self.finder.find(name, importer=importer)
            if found is None:
                self._report_missing(name, importer, soft)
                continue
            if found.kind in ("builtin", "frozen"):
                continue
            if found.category == "stdlib":
                self.stdlib_seen.add(name)
                if name.split(".", 1)[0] in ("tkinter", "_tkinter"):
                    self.features.add("tcl-tk")
                continue
            if found.category == "thirdparty" and found.dist is not None:
                self.distributions.setdefault(found.dist.canonical, found.dist)
            self.modules[name] = found
            if name.split(".", 1)[0] in ("tkinter", "_tkinter"):
                self.features.add("tcl-tk")
            self._scan(found)

    def _report_missing(self, name: str, importer: str, soft: bool) -> None:
        top = name.split(".", 1)[0]
        if top in self.stdlib_names:
            return
        if name in self.modules:
            return
        self.missing.append(MissingImport(name=name, importer=importer, soft=soft))
        if soft or self.logger is None:
            return
        self.logger.detail(f"missing import {name!r} (from {importer})")

    def _scan(self, found: FoundModule) -> None:
        if found.kind in ("extension", "builtin", "frozen"):
            return
        path = found.path
        if path.is_dir():  # namespace package: nothing to scan
            return
        try:
            data = path.read_bytes()
        except OSError as exc:
            self.warn(f"cannot read {path}: {exc}")
            return
        try:
            code = compile(data, str(path), "exec", dont_inherit=True)
        except SyntaxError as exc:
            raise AnalysisError(
                f"cannot parse {path}",
                hint=(
                    f"{exc.msg} (line {exc.lineno}). PyBundler parses sources with the "
                    "build environment's Python; make sure it is not older than the "
                    "runtime you are bundling."
                ),
            ) from exc
        except ValueError as exc:  # null bytes and friends
            self.warn(f"cannot parse {path}: {exc}")
            return
        for statement in imports_from_code(code):
            for candidate, soft in self._import_candidates(found, statement):
                self.enqueue(candidate, importer=found.name, soft=soft)
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("latin-1", errors="replace")
        for dynamic in dynamic_imports(text, filename=str(path)):
            if dynamic.startswith("."):
                resolved = _resolve_relative(found.name, found.is_package, 1, dynamic.lstrip("."))
                if resolved:
                    self.enqueue(resolved, importer=found.name, soft=True)
            else:
                self.enqueue(dynamic, importer=found.name, soft=True)

    def _import_candidates(self, found: FoundModule, statement: ImportStmt) -> list[tuple[str, bool]]:
        name = statement.name
        if statement.level:
            absolute = _resolve_relative(found.name, found.is_package, statement.level, name)
            if absolute is None:
                self.warn(
                    f"{found.name}: relative import beyond top-level package "
                    f"(level {statement.level})"
                )
                return []
        else:
            absolute = name
        candidates: list[tuple[str, bool]] = []
        if absolute:
            candidates.append((absolute, False))
        if statement.fromlist:
            base = absolute or ""
            for item in statement.fromlist:
                if not _IDENTIFIER.match(item):
                    continue
                candidate = f"{base}.{item}" if base else item
                candidates.append((candidate, True))
        return candidates


def _resolve_relative(module: str, is_package: bool, level: int, target: str) -> str | None:
    if level <= 0:
        return target or None
    base = module if is_package else module.rpartition(".")[0]
    if not base:
        return None
    parts = base.split(".")
    drop = level - 1
    if drop >= len(parts):
        return None
    parts = parts[: len(parts) - drop]
    base = ".".join(parts)
    if target:
        return f"{base}.{target}" if base else target
    return base


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def analyze(
    *,
    entry: EntryPoint,
    project_root: Path,
    finder: ModuleFinder,
    hooks: HookRegistry,
    logger: Logger,
    hidden_imports: Iterable[str] = (),
    packages: Iterable[str] = (),
    exclusions: Iterable[str] = (),
    include_tests: bool = False,
    extra_data: Iterable[DataFile] = (),
    extra_binaries: Iterable[BinaryFile] = (),
) -> Analysis:
    resolution = resolve_entry(entry, project_root)
    analyzer = Analyzer(
        finder,
        project_root=project_root,
        exclusions=exclusions,
        include_tests=include_tests,
        logger=logger,
    )
    entry_record: FoundModule | None = None

    for item in hidden_imports:
        analyzer.enqueue(item, importer="<hidden>", soft=False)

    if resolution.mode == "script":
        source = resolution.script_source
        assert source is not None
        module_name = resolution.module or "__main__"
        record = FoundModule(
            name=module_name,
            kind="module",
            path=source,
            is_package=False,
            root=source.parent,
            rel=Path(source.name),
            category="project",
        )
        analyzer.modules[module_name] = record
        analyzer._scan(record)  # noqa: SLF001 - intentional internal hand-off
        entry_record = record
    elif resolution.module:
        analyzer.enqueue(resolution.module, importer="<entry>", soft=False)

    for package in packages:
        analyzer.include_package(package)

    for data in extra_data:
        analyzer.add_data(data.source, data.dest, origin=data.origin)
    for binary in extra_binaries:
        analyzer.add_binary(binary.source, binary.dest)

    analyzer.run()

    already_run: set[tuple[str, str]] = set()
    for _ in range(6):
        ran = hooks.run_pending(analyzer, already_run)
        if ran == 0:
            break
        analyzer.run()
    else:  # pragma: no cover - only for pathological hook graphs
        analyzer.warn("hooks kept adding imports for several rounds; stopping")

    if resolution.mode in ("module", "callable"):
        target = resolution.module
        if target and not analyzer.has(target):
            raise AnalysisError(
                f"entry module {target!r} was not found on the build environment's sys.path",
                hint=(
                    "check the entry point in your spec, and make sure the package is "
                    "installed in the environment PyBundler is analysing"
                ),
            )

    deduplicated: dict[tuple[str, str], MissingImport] = {}
    for item in analyzer.missing:
        key = (item.name, item.importer)
        previous = deduplicated.get(key)
        if previous is None or (previous.soft and not item.soft):
            deduplicated[key] = item
    modules = dict(sorted(analyzer.modules.items()))
    distributions = dict(sorted(analyzer.distributions.items()))
    analysis = Analysis(
        entry=resolution,
        modules=modules,
        distributions=distributions,
        data=list(analyzer.data),
        binaries=list(analyzer.binaries),
        missing=sorted(deduplicated.values(), key=lambda item: (item.name, item.importer)),
        warnings=list(analyzer.warnings),
        features=frozenset(analyzer.features),
        project_root=project_root,
        site_dirs=finder.site_dirs,
        stdlib_dirs=finder.stdlib_dirs,
        stdlib_seen=frozenset(analyzer.stdlib_seen),
    )
    if entry_record is not None:
        analysis.entry = resolution
    return analysis
