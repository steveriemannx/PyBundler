"""Hook system for packages with runtime-only import behaviour.

PyBundler's default strategy is deliberately conservative: it copies an entire
imported package directory (minus tests and caches).  That makes the vast
majority of plugins, data files and lazily imported submodules work without
any hook at all.

Hooks exist for the remaining cases:

* files that live outside the package tree (``ctx.add_data``),
* shared libraries that must be copied next to the bundle (``ctx.add_binary``),
* runtime features such as Tcl/Tk (``ctx.feature("tcl-tk")``),
* namespace packages where only imported files are copied by default
  (``ctx.collect_all("pkg")``).

Built-in hooks are small Python files in ``pybundler/hooks``.  User hooks use
the same API and are loaded from ``collect.hooks`` in the spec.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Iterable

from ..errors import AnalysisError

if TYPE_CHECKING:  # pragma: no cover
    from .graph import Analyzer

BUILTIN_HOOK_DIR = Path(__file__).resolve().parent.parent / "hooks"


@dataclass
class HookFile:
    path: Path
    packages: tuple[str, ...]
    function: Callable[["HookContext"], None] | None
    hidden_imports: tuple[str, ...] = ()
    data: tuple[tuple[str, str], ...] = ()
    binaries: tuple[tuple[str, str], ...] = ()
    builtin: bool = True

    @property
    def label(self) -> str:
        return self.path.name if self.builtin else str(self.path)


class HookContext:
    """API handed to hook functions."""

    def __init__(self, analyzer: "Analyzer", package: str, hook: HookFile) -> None:
        self.analyzer = analyzer
        self.package = package
        self.hook = hook
        self._data: list[tuple[Path, str]] = []
        self._binaries: list[tuple[Path, str]] = []
        self._hidden: list[str] = []

    # -- imports --------------------------------------------------------
    def hidden_import(self, *names: str) -> None:
        for name in names:
            if name not in self._hidden:
                self._hidden.append(name)

    def collect_all(self, package: str | None = None) -> None:
        """Import every submodule of *package* (defaults to the hooked one)."""
        self.analyzer.include_package(package or self.package)

    def exclude(self, *names: str) -> None:
        self.analyzer.exclude(*names)

    def has(self, module: str) -> bool:
        """True if *module* is included in the bundle or part of the stdlib."""
        return self.analyzer.has(module)

    def included(self, module: str) -> bool:
        """True only if *module* has actually been pulled into the bundle."""
        return self.analyzer.included(module)

    def package_dir(self, package: str | None = None) -> Path:
        name = package or self.package
        directory = self.analyzer.finder.package_dir(name)
        if directory is None:
            raise AnalysisError(
                f"hook {self.hook.label}: package {name!r} was not found in the build environment"
            )
        return directory

    # -- payload --------------------------------------------------------
    def add_data(
        self,
        source: str | Path,
        dest: str | None = None,
        *,
        relative_to_package: bool = True,
    ) -> None:
        """Ship a file or directory inside the bundle.

        ``source`` is resolved relative to the hooked package's directory unless
        it is absolute.  ``dest`` defaults to ``<package>/<basename>``.
        """
        root = self.package_dir()
        path = Path(source)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise AnalysisError(f"hook {self.hook.label}: data path not found: {path}")
        if dest is None:
            dest = f"{self.package}/{path.name}"
        self._data.append((path, dest.replace("\\", "/").lstrip("/")))

    def add_binary(
        self,
        source: str | Path,
        dest: str | None = None,
        *,
        relative_to_package: bool = True,
    ) -> None:
        root = self.package_dir()
        path = Path(source)
        if not path.is_absolute():
            path = root / path
        if not path.exists():
            raise AnalysisError(f"hook {self.hook.label}: binary path not found: {path}")
        if dest is None:
            dest = f"bin/{path.name}"
        self._binaries.append((path, dest.replace("\\", "/").lstrip("/")))

    def feature(self, name: str) -> None:
        """Request an optional runtime feature (currently ``tcl-tk``)."""
        self.analyzer.features.add(name)

    def warn(self, message: str) -> None:
        self.analyzer.warn(f"hook {self.package}: {message}")

    # -- internal -------------------------------------------------------
    def commit(self) -> None:
        for name in self._hidden:
            self.analyzer.enqueue(name, importer=f"hook:{self.package}", soft=False)
        for source, dest in self._data:
            self.analyzer.add_data(source, dest)
        for source, dest in self._binaries:
            self.analyzer.add_binary(source, dest)


def _load_hook_module(path: Path, *, builtin: bool) -> Any:
    unique = f"_pybundler_hook_{abs(hash((str(path), builtin)))}"
    spec = importlib.util.spec_from_file_location(unique, path)
    if spec is None or spec.loader is None:  # pragma: no cover - defensive
        raise AnalysisError(f"cannot load hook {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[unique] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        raise AnalysisError(f"hook {path} failed to load: {exc}") from exc
    return module


def load_hook_file(path: Path, *, builtin: bool) -> HookFile:
    path = path.resolve()
    module = _load_hook_module(path, builtin=builtin)
    declared = getattr(module, "PACKAGES", None)
    if declared is None:
        packages = (path.stem,)
    elif isinstance(declared, str):
        packages = (declared,)
    else:
        packages = tuple(str(item) for item in declared)
    if not packages:
        raise AnalysisError(f"hook {path} declares an empty PACKAGES list")
    hidden = tuple(str(item) for item in getattr(module, "HIDDEN_IMPORTS", ()))
    data_items = getattr(module, "DATA", ())
    binaries = getattr(module, "BINARIES", ())
    data: list[tuple[str, str]] = []
    for item in data_items:
        if isinstance(item, str):
            data.append((item, item))
        elif isinstance(item, (tuple, list)) and len(item) == 2:
            data.append((str(item[0]), str(item[1])))
        else:
            raise AnalysisError(f"hook {path}: DATA entries must be 'src' or ('src', 'dest')")
    binary_items: list[tuple[str, str]] = []
    for item in binaries:
        if isinstance(item, (tuple, list)) and len(item) == 2:
            binary_items.append((str(item[0]), str(item[1])))
        else:
            raise AnalysisError(f"hook {path}: BINARIES entries must be ('src', 'dest')")
    function = getattr(module, "hook", None)
    if function is not None and not callable(function):
        raise AnalysisError(f"hook {path}: `hook` must be a function")
    return HookFile(
        path=path,
        packages=packages,
        function=function,
        hidden_imports=hidden,
        data=tuple(data),
        binaries=tuple(binary_items),
        builtin=builtin,
    )


class HookRegistry:
    """Selects and runs hooks for the packages that ended up in the bundle."""

    def __init__(self, user_hooks: Iterable[Path] = (), *, logger: Any = None) -> None:
        self.logger = logger
        self.hooks: dict[str, list[HookFile]] = {}
        for path in sorted(BUILTIN_HOOK_DIR.glob("*.py")):
            if path.name.startswith("_"):
                continue
            self._register(load_hook_file(path, builtin=True))
        for path in user_hooks:
            self._register(load_hook_file(Path(path), builtin=False))

    def _register(self, hook: HookFile) -> None:
        for package in hook.packages:
            self.hooks.setdefault(package, []).append(hook)

    def package_names(self) -> set[str]:
        return set(self.hooks)

    def run_pending(self, analyzer: "Analyzer", already_run: set[tuple[str, str]]) -> int:
        """Run hooks for newly included top-level packages; return how many ran."""
        ran = 0
        for name in sorted(analyzer.included_names()):
            top = name.split(".", 1)[0]
            for hook in self.hooks.get(top, ()):
                key = (hook.label, top)
                if key in already_run:
                    continue
                already_run.add(key)
                self._run(hook, analyzer, top)
                ran += 1
        return ran

    def _run(self, hook: HookFile, analyzer: "Analyzer", package: str) -> None:
        context = HookContext(analyzer, package, hook)
        for name in hook.hidden_imports:
            context.hidden_import(name)
        for source, dest in hook.data:
            try:
                context.add_data(source, dest)
            except AnalysisError as exc:
                analyzer.warn(str(exc))
        for source, dest in hook.binaries:
            try:
                context.add_binary(source, dest)
            except AnalysisError as exc:
                analyzer.warn(str(exc))
        if hook.function is not None:
            if self.logger is not None:
                self.logger.detail(f"running hook {hook.label} for {package}")
            try:
                hook.function(context)
            except AnalysisError:
                raise
            except Exception as exc:
                raise AnalysisError(f"hook {hook.label} failed for {package!r}: {exc}") from exc
        context.commit()
