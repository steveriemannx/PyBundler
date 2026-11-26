"""Spec file parsing for ``pybundler.toml`` (and ``[tool.pybundler]``).

A spec is intentionally small: application identity, entry point, bundling
mode, and the knobs needed to fix up import analysis.  Every path in the spec
is resolved relative to the spec file's directory so that builds are
reproducible from any working directory.
"""

from __future__ import annotations

import os
import re
import shlex
import sys
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Mapping

from .errors import ConfigError

NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
MODES = ("onedir", "onefile")
RUNTIME_SOURCES = ("auto", "download", "local")

_ENTRY_DEFAULTS = (
    "src/__main__.py",
    "__main__.py",
    "src/main.py",
    "main.py",
    "src/app.py",
    "app.py",
)


@dataclass(frozen=True)
class EntryPoint:
    """Where execution starts.

    ``kind`` is one of:

    ``script``
        Run a ``.py`` file with ``__name__ == "__main__"``.
    ``module``
        Run ``python -m module`` semantics.
    ``callable``
        Import a module and call a function (console-scripts style).
    """

    kind: str
    value: str
    source: Path | None = None  # for scripts
    module: str | None = None
    function: str | None = None

    @property
    def display(self) -> str:
        if self.kind == "script" and self.source:
            return str(self.source)
        if self.kind == "callable":
            return f"{self.module}:{self.function}"
        return self.module or self.value


@dataclass
class AppConfig:
    name: str
    version: str = "0.0.0"
    entry: EntryPoint = field(default_factory=lambda: EntryPoint(kind="script", value=""))
    mode: str = "onedir"
    console: bool = True
    icon: Path | None = None
    bundle_id: str | None = None
    company: str = ""


@dataclass
class PythonConfig:
    executable: Path | None = None
    version: str | None = None
    runtime: str = "auto"
    runtime_path: Path | None = None
    pbs_tag: str | None = None


@dataclass
class CollectConfig:
    hidden_imports: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    packages: list[str] = field(default_factory=list)
    data: list[str] = field(default_factory=list)
    binaries: list[str] = field(default_factory=list)
    hooks: list[Path] = field(default_factory=list)
    include_tests: bool = False


@dataclass
class BuildConfig:
    output_dir: Path = Path("dist")
    work_dir: Path = Path("build")
    #: Ship ``.pyc`` files instead of sources (PyInstaller's default too).
    #: Pass ``--keep-sources`` / set ``build.strip_sources = false`` to ship
    #: readable tracebacks.
    strip_sources: bool = True
    compress: bool = True
    verify: bool = False
    keep_work: bool = False


@dataclass
class LaunchConfig:
    env: dict[str, str] = field(default_factory=dict)
    args: list[str] = field(default_factory=list)


@dataclass
class Spec:
    project_root: Path
    spec_path: Path | None
    app: AppConfig
    python: PythonConfig = field(default_factory=PythonConfig)
    collect: CollectConfig = field(default_factory=CollectConfig)
    build: BuildConfig = field(default_factory=BuildConfig)
    launch: LaunchConfig = field(default_factory=LaunchConfig)
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def is_onefile(self) -> bool:
        return self.app.mode == "onefile"


# ---------------------------------------------------------------------------
# Parsing helpers
# ---------------------------------------------------------------------------


def _as_str_list(value: Any, where: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise ConfigError(f"{where}: expected a list of strings")
    return list(value)


def _bool(value: Any, where: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ConfigError(f"{where}: expected a boolean")
    return value


def _resolve(root: Path, value: str | os.PathLike[str] | None) -> Path | None:
    if value is None or value == "":
        return None
    path = Path(value).expanduser()
    return path if path.is_absolute() else (root / path)


def _section(data: Mapping[str, Any], name: str, where: str) -> Mapping[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, Mapping):
        raise ConfigError(f"{where}: [{name}] must be a table")
    return value


def _parse_entry(
    raw: Any,
    *,
    root: Path,
    name: str,
    where: str,
    entry_overrides: Mapping[str, Any] | None = None,
) -> EntryPoint:
    if raw is None and entry_overrides:
        # [project.scripts] with exactly one entry
        if len(entry_overrides) == 1:
            script_name, target = next(iter(entry_overrides.items()))
            if isinstance(target, str) and ":" in target:
                module, _, function = target.partition(":")
                return EntryPoint(kind="callable", value=target, module=module.strip(), function=function.strip())
    if raw is None:
        for candidate in _ENTRY_DEFAULTS:
            path = root / candidate
            if path.is_file():
                return EntryPoint(kind="script", value=candidate, source=path, module=_script_module_name(candidate))
        for candidate in (f"src/{name}/__main__.py", f"{name}/__main__.py"):
            path = root / candidate
            if path.is_file():
                return EntryPoint(kind="script", value=candidate, source=path, module=_script_module_name(candidate))
        raise ConfigError(
            f"{where}: no entry point configured and none could be detected",
            hint=(
                "add `entry = \"path/to/main.py\"` to the [app] table, or create "
                "main.py / __main__.py in the project root"
            ),
        )

    if isinstance(raw, Mapping):
        raise ConfigError(f"{where}: entry must be a string")
    if not isinstance(raw, str) or not raw.strip():
        raise ConfigError(f"{where}: entry must be a non-empty string")
    value = raw.strip()

    if ":" in value and not value.endswith(".py"):
        module, _, function = value.partition(":")
        if not module.strip() or not function.strip():
            raise ConfigError(f"{where}: malformed entry {value!r}, expected 'module:function'")
        return EntryPoint(kind="callable", value=value, module=module.strip(), function=function.strip())

    candidate = _resolve(root, value)
    if candidate is not None and candidate.is_file():
        return EntryPoint(kind="script", value=value, source=candidate, module=_script_module_name(value))
    if value.endswith(".py"):
        raise ConfigError(f"{where}: entry script not found: {candidate}")
    return EntryPoint(kind="module", value=value, module=value)


def _script_module_name(relative_path: str) -> str | None:
    parts = list(Path(relative_path).with_suffix("").parts)
    if parts and parts[-1] in ("__main__",):
        parts = parts[:-1]
    if not parts:
        return None
    if not all(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", part) for part in parts):
        return None
    return ".".join(parts)


def _parse_app(data: Mapping[str, Any], *, root: Path, where: str, project: Mapping[str, Any]) -> AppConfig:
    app = _section(data, "app", where)
    name = app.get("name") or project.get("name") or root.name
    if not isinstance(name, str) or not NAME_RE.match(name):
        raise ConfigError(
            f"{where}: invalid application name {name!r}",
            hint="names may contain letters, digits, dots, dashes and underscores",
        )
    version_value = app.get("version", project.get("version", "0.0.0"))
    if not isinstance(version_value, str):
        version_value = str(version_value)
    mode = app.get("mode", "onedir")
    if mode not in MODES:
        raise ConfigError(f"{where}: app.mode must be one of {', '.join(MODES)}")
    console = _bool(app.get("console"), f"{where}: app.console", True)
    icon = _resolve(root, app.get("icon"))
    bundle_id = app.get("macos_bundle_id") or app.get("bundle_id")
    company = app.get("company", "")
    if bundle_id is not None and not isinstance(bundle_id, str):
        raise ConfigError(f"{where}: app.macos_bundle_id must be a string")
    entry = _parse_entry(
        app.get("entry"),
        root=root,
        name=name,
        where=f"{where}: app.entry",
        entry_overrides=project.get("scripts") if isinstance(project.get("scripts"), Mapping) else None,
    )
    return AppConfig(
        name=name,
        version=version_value,
        entry=entry,
        mode=mode,
        console=console,
        icon=icon,
        bundle_id=bundle_id,
        company=str(company),
    )


def _parse_python(data: Mapping[str, Any], *, root: Path, where: str) -> PythonConfig:
    section = _section(data, "python", where)
    executable = _resolve(root, section.get("executable"))
    version = section.get("version")
    if version is not None and not isinstance(version, str):
        version = str(version)
    runtime = section.get("runtime", "auto")
    if runtime not in RUNTIME_SOURCES:
        raise ConfigError(f"{where}: python.runtime must be one of {', '.join(RUNTIME_SOURCES)}")
    runtime_path = _resolve(root, section.get("runtime_path"))
    tag = section.get("pbs_tag")
    if tag is not None and not isinstance(tag, str):
        raise ConfigError(f"{where}: python.pbs_tag must be a string")
    return PythonConfig(
        executable=executable,
        version=version,
        runtime=runtime,
        runtime_path=runtime_path,
        pbs_tag=tag,
    )


def _parse_collect(data: Mapping[str, Any], *, root: Path, where: str) -> CollectConfig:
    section = _section(data, "collect", where)
    hooks = [
        _resolve(root, item)
        for item in _as_str_list(section.get("hooks"), f"{where}: collect.hooks")
    ]
    return CollectConfig(
        hidden_imports=_as_str_list(section.get("hidden_imports"), f"{where}: collect.hidden_imports"),
        exclude=_as_str_list(section.get("exclude"), f"{where}: collect.exclude"),
        packages=_as_str_list(section.get("packages"), f"{where}: collect.packages"),
        data=_as_str_list(section.get("data"), f"{where}: collect.data"),
        binaries=_as_str_list(section.get("binaries"), f"{where}: collect.binaries"),
        hooks=[h for h in hooks if h is not None],
        include_tests=_bool(section.get("include_tests"), f"{where}: collect.include_tests", False),
    )


def _parse_build(data: Mapping[str, Any], *, root: Path, where: str) -> BuildConfig:
    section = _section(data, "build", where)
    output_dir = _resolve(root, section.get("output_dir", "dist"))
    work_dir = _resolve(root, section.get("work_dir", "build"))
    assert output_dir is not None and work_dir is not None
    return BuildConfig(
        output_dir=output_dir,
        work_dir=work_dir,
        strip_sources=_bool(section.get("strip_sources"), f"{where}: build.strip_sources", True),
        compress=_bool(section.get("compress"), f"{where}: build.compress", True),
        verify=_bool(section.get("verify"), f"{where}: build.verify", False),
        keep_work=_bool(section.get("keep_work"), f"{where}: build.keep_work", False),
    )


def _parse_launch(data: Mapping[str, Any], where: str) -> LaunchConfig:
    section = _section(data, "launch", where)
    env_raw = section.get("env", {})
    if not isinstance(env_raw, Mapping):
        raise ConfigError(f"{where}: [launch.env] must be a table")
    env: dict[str, str] = {}
    for key, value in env_raw.items():
        if not isinstance(key, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ConfigError(f"{where}: invalid environment variable name {key!r}")
        if not isinstance(value, (str, int, float, bool)):
            raise ConfigError(f"{where}: launch.env.{key} must be a scalar")
        env[key] = str(value)
    args_raw = section.get("args", [])
    if isinstance(args_raw, str):
        args = shlex.split(args_raw)
    elif isinstance(args_raw, list) and all(isinstance(item, str) for item in args_raw):
        args = list(args_raw)
    else:
        raise ConfigError(f"{where}: launch.args must be a list of strings")
    return LaunchConfig(env=env, args=args)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def find_spec_file(path: str | Path | None) -> Path | None:
    """Locate the spec file, either explicitly or by searching the project."""
    if path:
        candidate = Path(path).expanduser()
        if not candidate.exists():
            raise ConfigError(f"spec file not found: {candidate}")
        if candidate.is_dir():
            candidate = candidate / "pybundler.toml"
            if not candidate.exists():
                raise ConfigError(f"no pybundler.toml in {path}")
        return candidate.resolve()
    cwd = Path.cwd()
    for candidate in (cwd / "pybundler.toml", cwd / "pybundler.build.toml"):
        if candidate.exists():
            return candidate
    pyproject = cwd / "pyproject.toml"
    if pyproject.exists():
        try:
            data = tomllib.loads(pyproject.read_text("utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise ConfigError(f"{pyproject}: {exc}") from exc
        if isinstance(data.get("tool", {}).get("pybundler"), Mapping):
            return pyproject.resolve()
    return None


def load_spec(path: str | Path | None = None) -> Spec:
    """Load and validate a build spec.

    When *path* is omitted the current directory is searched for
    ``pybundler.toml`` and then for ``[tool.pybundler]`` inside
    ``pyproject.toml``.
    """
    spec_path = find_spec_file(path)
    if spec_path is None:
        raise ConfigError(
            f"no pybundler.toml found in {Path.cwd()}",
            hint="run `pybundler init` to create one, or pass the path to a spec file",
        )
    try:
        text = spec_path.read_text("utf-8")
        document = tomllib.loads(text)
    except OSError as exc:
        raise ConfigError(f"cannot read {spec_path}: {exc}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{spec_path}: invalid TOML: {exc}") from exc

    root = spec_path.parent
    where = str(spec_path)
    if spec_path.name == "pyproject.toml":
        tool = document.get("tool", {})
        if not isinstance(tool, Mapping) or "pybundler" not in tool:
            raise ConfigError(f"{where}: missing [tool.pybundler] table")
        raw = tool["pybundler"]
        if not isinstance(raw, Mapping):
            raise ConfigError(f"{where}: [tool.pybundler] must be a table")
        data = dict(raw)
        # Accept both [tool.pybundler.app] and a flat [tool.pybundler] table
        # with application/collect/build keys directly in it.
        if "app" not in data:
            buckets: dict[str, dict[str, object]] = {
                "app": {},
                "python": {},
                "build": {},
                "collect": {},
                "launch": {},
            }
            key_map = {
                "name": "app",
                "version": "app",
                "entry": "app",
                "mode": "app",
                "console": "app",
                "icon": "app",
                "bundle_id": "app",
                "macos_bundle_id": "app",
                "company": "app",
                "executable": "python",
                "runtime": "python",
                "runtime_path": "python",
                "pbs_tag": "python",
                "output_dir": "build",
                "work_dir": "build",
                "strip_sources": "build",
                "compress": "build",
                "verify": "build",
                "keep_work": "build",
                "hidden_imports": "collect",
                "exclude": "collect",
                "packages": "collect",
                "data": "collect",
                "binaries": "collect",
                "hooks": "collect",
                "include_tests": "collect",
                "env": "launch",
                "args": "launch",
            }
            remaining: dict[str, object] = {}
            for key, value in data.items():
                section = key_map.get(key)
                if section is None:
                    remaining[key] = value
                else:
                    buckets[section][key] = value
            if buckets["app"]:
                data = {**remaining, **{name: values for name, values in buckets.items() if values}}
        project = document.get("project", {})
        project = project if isinstance(project, Mapping) else {}
    else:
        data = document
        project = {}
        # A standalone spec may reuse [project] fields for convenience.
        maybe_project = document.get("project")
        if isinstance(maybe_project, Mapping):
            project = maybe_project

    app = _parse_app(data, root=root, where=where, project=project)
    spec = Spec(
        project_root=root,
        spec_path=spec_path,
        app=app,
        python=_parse_python(data, root=root, where=where),
        collect=_parse_collect(data, root=root, where=where),
        build=_parse_build(data, root=root, where=where),
        launch=_parse_launch(data, where=where),
        raw=dict(data),
    )
    _validate(spec)
    return spec


def _validate(spec: Spec) -> None:
    entry = spec.app.entry
    if entry.kind == "script" and entry.source is None:
        raise ConfigError("internal error: script entry point has no source path")
    if spec.app.icon is not None and not spec.app.icon.exists():
        raise ConfigError(f"icon not found: {spec.app.icon}")
    for hook in spec.collect.hooks:
        if hook is None or not hook.exists():
            raise ConfigError(f"hook file not found: {hook}")
    if spec.app.name in ("con", "prn", "aux", "nul") and sys.platform == "win32":
        raise ConfigError(f"{spec.app.name!r} is a reserved filename on Windows")
    if len(spec.app.name) > 120:
        raise ConfigError("application name is too long (max 120 characters)")


def apply_overrides(
    spec: Spec,
    *,
    mode: str | None = None,
    name: str | None = None,
    console: bool | None = None,
    python_executable: Path | None = None,
    python_version: str | None = None,
    runtime: str | None = None,
    runtime_path: Path | None = None,
    output_dir: Path | None = None,
    work_dir: Path | None = None,
    strip_sources: bool | None = None,
    compress: bool | None = None,
    verify: bool | None = None,
    keep_work: bool | None = None,
    hidden_imports: list[str] | None = None,
    exclude: list[str] | None = None,
    add_data: list[str] | None = None,
    add_binaries: list[str] | None = None,
    hook_dir: Path | None = None,
    include_tests: bool | None = None,
    pbs_tag: str | None = None,
) -> Spec:
    """Return a copy of *spec* with command line overrides applied."""
    if mode is not None and mode not in MODES:
        raise ConfigError(f"invalid mode {mode!r}")
    app = replace(
        spec.app,
        mode=mode or spec.app.mode,
        name=name or spec.app.name,
        console=spec.app.console if console is None else console,
    )
    python = replace(
        spec.python,
        executable=python_executable if python_executable is not None else spec.python.executable,
        version=python_version or spec.python.version,
        runtime=runtime or spec.python.runtime,
        runtime_path=runtime_path if runtime_path is not None else spec.python.runtime_path,
        pbs_tag=pbs_tag or spec.python.pbs_tag,
    )
    build = replace(
        spec.build,
        output_dir=output_dir if output_dir is not None else spec.build.output_dir,
        work_dir=work_dir if work_dir is not None else spec.build.work_dir,
        strip_sources=spec.build.strip_sources if strip_sources is None else strip_sources,
        compress=spec.build.compress if compress is None else compress,
        verify=spec.build.verify if verify is None else verify,
        keep_work=spec.build.keep_work if keep_work is None else keep_work,
    )
    hooks = list(spec.collect.hooks)
    if hook_dir is not None:
        if not hook_dir.is_dir():
            raise ConfigError(f"hook directory not found: {hook_dir}")
        hooks.extend(sorted(hook_dir.glob("*.py")))
    collect = replace(
        spec.collect,
        hidden_imports=list(spec.collect.hidden_imports) + list(hidden_imports or []),
        exclude=list(spec.collect.exclude) + list(exclude or []),
        data=list(spec.collect.data) + list(add_data or []),
        binaries=list(spec.collect.binaries) + list(add_binaries or []),
        hooks=hooks,
        include_tests=spec.collect.include_tests if include_tests is None else include_tests,
    )
    updated = replace(spec, app=app, python=python, build=build, collect=collect)
    _validate(updated)
    return updated
