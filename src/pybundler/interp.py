"""Interrogating Python interpreters (both the build environment and the
standalone runtime) without importing them into the PyBundler process.

Import analysis has to reflect the *target* environment: its ``sys.path``, its
extension module suffix and the set of built-in modules.  Instead of guessing,
we run the interpreter once with a tiny probe script and cache the JSON result.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .errors import BuildError

_PROBE_SOURCE = r"""
import json, os, platform, struct, sys, sysconfig
paths = sysconfig.get_paths()
stdlib = paths.get("stdlib") or ""
dynload = os.path.join(paths.get("platstdlib") or stdlib, "lib-dynload")
print(json.dumps({
    "version": sys.version.split()[0],
    "hexversion": sys.hexversion,
    "implementation": sys.implementation.name,
    "executable": sys.executable,
    "prefix": sys.prefix,
    "base_prefix": getattr(sys, "base_prefix", sys.prefix),
    "sys_path": [p for p in sys.path if p],
    "platform": sys.platform,
    "machine": platform.machine(),
    "bits": struct.calcsize("P") * 8,
    "ext_suffix": sysconfig.get_config_var("EXT_SUFFIX") or "",
    "soabi": sysconfig.get_config_var("SOABI") or "",
    "stdlib": stdlib,
    "platstdlib": paths.get("platstdlib") or stdlib,
    "dynload": dynload,
    "purelib": paths.get("purelib") or "",
    "platlib": paths.get("platlib") or "",
    "builtin_modules": sorted(sys.builtin_module_names),
    "is_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
}))
"""


@dataclass(frozen=True)
class EnvInfo:
    """Facts about a Python environment, gathered by running it."""

    python: Path
    version: str
    hexversion: int
    implementation: str
    prefix: Path
    base_prefix: Path
    sys_path: tuple[Path, ...]
    sysname: str
    machine: str
    bits: int
    ext_suffix: str
    soabi: str
    stdlib: Path
    platstdlib: Path
    dynload: Path
    purelib: Path
    platlib: Path
    builtin_modules: frozenset[str]
    is_venv: bool

    @property
    def version_tuple(self) -> tuple[int, int, int]:
        parts = self.version.split(".")
        values = [int(p) for p in parts[:3]]
        while len(values) < 3:
            values.append(0)
        return tuple(values)  # type: ignore[return-value]

    @property
    def major_minor(self) -> tuple[int, int]:
        return self.version_tuple[:2]

    @property
    def major_minor_str(self) -> str:
        return f"{self.major_minor[0]}.{self.major_minor[1]}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "python": str(self.python),
            "version": self.version,
            "implementation": self.implementation,
            "prefix": str(self.prefix),
            "base_prefix": str(self.base_prefix),
            "sys_path": [str(p) for p in self.sys_path],
            "platform": self.sysname,
            "machine": self.machine,
            "bits": self.bits,
            "ext_suffix": self.ext_suffix,
            "soabi": self.soabi,
            "stdlib": str(self.stdlib),
            "purelib": str(self.purelib),
            "platlib": str(self.platlib),
            "is_venv": self.is_venv,
        }


def sanitized_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """Return a minimal, deterministic environment for child interpreters."""
    keep = ("PATH", "HOME", "TMPDIR", "TEMP", "TMP", "LANG", "LC_ALL", "SYSTEMROOT", "USERPROFILE")
    env = {key: os.environ[key] for key in keep if key in os.environ}
    env.setdefault("PATH", os.defpath)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra:
        env.update(extra)
    return env


def run_python(
    python: Path,
    args: Sequence[str],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    cwd: Path | None = None,
    stdin: str | None = None,
    timeout: float = 600.0,
) -> subprocess.CompletedProcess[str]:
    """Run *python* with a clean environment."""
    extra: dict[str, str] = dict(env or {})
    if home is not None:
        extra["PYTHONHOME"] = str(home)
    proc = subprocess.run(
        [str(python), *args],
        input=stdin,
        capture_output=True,
        text=True,
        cwd=str(cwd) if cwd else None,
        env=sanitized_env(extra),
        timeout=timeout,
    )
    return proc


def _parse_probe(python: Path, payload: Mapping[str, Any]) -> EnvInfo:
    def _path(key: str) -> Path:
        value = payload.get(key) or ""
        return Path(value) if value else Path()

    return EnvInfo(
        python=python,
        version=str(payload["version"]),
        hexversion=int(payload["hexversion"]),
        implementation=str(payload["implementation"]),
        prefix=_path("prefix"),
        base_prefix=_path("base_prefix"),
        sys_path=tuple(Path(p) for p in payload["sys_path"]),
        sysname=str(payload["platform"]),
        machine=str(payload["machine"]),
        bits=int(payload.get("bits") or 0),
        ext_suffix=str(payload.get("ext_suffix") or ""),
        soabi=str(payload.get("soabi") or ""),
        stdlib=_path("stdlib"),
        platstdlib=_path("platstdlib"),
        dynload=_path("dynload"),
        purelib=_path("purelib"),
        platlib=_path("platlib"),
        builtin_modules=frozenset(payload["builtin_modules"]),
        is_venv=bool(payload["is_venv"]),
    )


@lru_cache(maxsize=32)
def _probe_cached(python_str: str, home_str: str | None) -> EnvInfo:
    python = Path(python_str)
    home = Path(home_str) if home_str else None
    if not python.exists():
        raise BuildError(f"Python interpreter not found: {python}")
    proc = run_python(python, ["-c", _PROBE_SOURCE], home=home)
    if proc.returncode != 0:
        raise BuildError(
            "failed to interrogate Python interpreter "
            f"{python}" + (f" (PYTHONHOME={home})" if home else ""),
            hint=(proc.stderr or proc.stdout)[-2000:] or "the interpreter exited with an error",
        )
    try:
        payload = json.loads(proc.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError) as exc:  # pragma: no cover - defensive
        raise BuildError(f"unexpected probe output from {python}: {proc.stdout!r}") from exc
    return _parse_probe(python, payload)


def probe_interpreter(python: Path, *, home: Path | None = None) -> EnvInfo:
    """Inspect an interpreter, caching the result for the lifetime of the process."""
    python = python.expanduser().resolve()
    return _probe_cached(str(python), str(home.resolve()) if home else None)


def host_env() -> EnvInfo:
    return probe_interpreter(Path(sys.executable))


def check_compatible(env: EnvInfo, runtime: EnvInfo) -> list[str]:
    """Return warnings about mismatches between build env and runtime."""
    warnings: list[str] = []
    if env.major_minor != runtime.major_minor:
        raise BuildError(
            "build environment and bundled runtime use different Python versions: "
            f"{env.version} vs {runtime.version}",
            hint=(
                "compiled extension modules are only compatible within one minor "
                "version. Build with a virtualenv whose Python matches the runtime, "
                "or pass --runtime-path pointing at a matching interpreter."
            ),
        )
    if env.ext_suffix and runtime.ext_suffix and env.ext_suffix != runtime.ext_suffix:
        warnings.append(
            f"extension suffix differs ({env.ext_suffix} vs {runtime.ext_suffix}); "
            "compiled modules of third-party packages may not load"
        )
    if env.bits and runtime.bits and env.bits != runtime.bits:
        raise BuildError(
            f"bitness mismatch: build environment is {env.bits}-bit, "
            f"runtime is {runtime.bits}-bit",
            hint=(
                "install a 32-bit or 64-bit Python (and matching runtime) so the "
                "interpreter and its compiled extensions agree"
            ),
        )
    if env.machine and runtime.machine and env.bits == runtime.bits == 64:
        def family(value: str) -> str:
            lowered = value.lower()
            if lowered in ("arm64", "aarch64"):
                return "arm64"
            if lowered in ("x86_64", "amd64", "x64"):
                return "x86_64"
            if lowered in ("x86", "i386", "i486", "i586", "i686"):
                return "x86"
            return lowered

        if family(env.machine) != family(runtime.machine):
            raise BuildError(
                f"architecture mismatch: build environment {env.machine}, runtime {runtime.machine}",
                hint="use a runtime that matches the build machine's CPU architecture",
            )
    return warnings


def analyze_environment(
    *,
    project_root: Path,
    interpreter: Path | None = None,
    extra_paths: Iterable[Path] = (),
) -> tuple[EnvInfo, list[Path]]:
    """Select the interpreter used for analysis and compute its search path.

    Resolution order: explicit interpreter, ``<project>/.venv``, ``$VIRTUAL_ENV``,
    then the interpreter running PyBundler.
    """
    candidates: list[Path] = []
    if interpreter is not None:
        candidates.append(interpreter.expanduser())
    else:
        for name in (".venv", "venv", "env"):
            for candidate in (project_root / name / "bin" / "python", project_root / name / "Scripts" / "python.exe"):
                if candidate.exists():
                    candidates.append(candidate)
        env_dir = os.environ.get("VIRTUAL_ENV")
        if env_dir:
            candidates.extend(
                [Path(env_dir) / "bin" / "python", Path(env_dir) / "Scripts" / "python.exe"]
            )
        candidates.append(Path(sys.executable))
    python = next((c for c in candidates if c.exists()), None)
    if python is None:
        raise BuildError(
            "no usable Python interpreter found for the build environment",
            hint="create a virtualenv or pass --python /path/to/python",
        )
    env = probe_interpreter(python)
    paths: list[Path] = []
    seen: set[str] = set()

    def add(path: Path) -> None:
        key = str(path)
        if key in seen or not path.is_dir():
            return
        seen.add(key)
        paths.append(path)

    add(project_root)
    for entry in env.sys_path:
        if str(entry) in ("", "."):
            continue
        add(entry)
    for entry in extra_paths:
        add(entry)
    return env, paths
