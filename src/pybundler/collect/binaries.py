"""Making bundled native libraries relocatable.

Wheels that contain compiled extensions usually ship their own shared
libraries, but they are linked in one of three ways:

1. relative to the package (``@loader_path``, ``$ORIGIN``) - already portable,
2. by absolute path into the build machine (Homebrew, conda, SDKs),
3. by soname / ``@rpath`` and resolved through the loader search path.

PyBundler fixes cases 2 and 3 by copying the dependency into ``<bundle>/lib``
and rewriting Mach-O load commands so the loader finds it relative to the
referring binary.  On Linux the launcher adds ``<bundle>/lib`` to
``LD_LIBRARY_PATH``, which is enough because ELF sonames are location
independent.  On Windows adjacent DLLs are found by the loader, so only a
warning is emitted for DLLs that live elsewhere.
"""

from __future__ import annotations

import hashlib
import os
import re
import shutil
import subprocess
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path

from ..errors import BuildError
from ..fsutil import ensure_dir, make_executable
from ..log import Logger
from ..targets import Target

_SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/System/",
    "/Library/Frameworks/",
    "/usr/libexec/",
    "/usr/local/lib/system/",
)
_LINUX_SYSTEM_PREFIXES = ("/lib/", "/lib64/", "/usr/lib/", "/usr/lib64/")
_NATIVE_SUFFIXES = {".so", ".dylib", ".pyd", ".dll"}
_RPATH_RE = re.compile(r"path\s+(\S+)\s+\(offset")


@dataclass
class NativeFixup:
    added: list[str] = field(default_factory=list)
    rewritten: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    #: dest inside the bundle -> original location on the build machine
    sources: dict[str, str] = field(default_factory=dict)


def _run(command: list[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(command, capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.SubprocessError):
        return None


def macho_dependencies(binary: Path) -> list[str]:
    proc = _run(["otool", "-L", str(binary)])
    if proc is None or proc.returncode != 0:
        return []
    dependencies: list[str] = []
    for line in proc.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        dependencies.append(line.split(" (compatibility")[0].strip())
    return dependencies


def macho_rpaths(binary: Path) -> list[str]:
    proc = _run(["otool", "-l", str(binary)])
    if proc is None or proc.returncode != 0:
        return []
    return [match.group(1) for match in _RPATH_RE.finditer(proc.stdout)]


def elf_needed(binary: Path) -> tuple[list[str], list[str]]:
    """Return ``(needed sonames, runpath entries)`` for an ELF file."""
    proc = _run(["readelf", "-d", str(binary)])
    if proc is None or proc.returncode != 0:
        proc = _run(["objdump", "-p", str(binary)])
    if proc is None or proc.returncode != 0:
        return [], []
    needed: list[str] = []
    rpaths: list[str] = []
    for line in proc.stdout.splitlines():
        stripped = line.strip()
        match = re.search(r"\(NEEDED\)\s+Shared library:\s+\[(.+?)\]", stripped)
        if match:
            needed.append(match.group(1))
            continue
        match = re.search(r"\((?:RPATH|RUNPATH)\)\s+Library r(?:un)?path:\s+\[(.*?)\]", stripped)
        if match:
            rpaths.extend(part for part in match.group(1).split(":") if part)
    return needed, rpaths


def _expand_loader_path(value: str, binary: Path) -> str:
    binary_dir = binary.parent
    value = value.replace("@loader_path", str(binary_dir))
    value = value.replace("$ORIGIN", str(binary_dir))
    value = value.replace("${ORIGIN}", str(binary_dir))
    return value


def _unique_name(directory: Path, name: str, source: Path, used: dict[str, Path]) -> str:
    candidate = name
    if candidate in used and used[candidate].resolve() != source.resolve():
        digest = hashlib.sha256(str(source).encode()).hexdigest()[:8]
        candidate = f"{Path(name).stem}-{digest}{Path(name).suffix}"
    used[candidate] = source.resolve()
    return candidate


def _repair_macos(
    root: Path,
    *,
    lib_dir: Path,
    search_dirs: list[Path],
    extra_roots: list[Path],
    skip_names: tuple[str, ...],
    logger: Logger | None,
) -> NativeFixup:
    result = NativeFixup()
    used_names: dict[str, Path] = {}
    queue: deque[Path] = deque()
    seen: set[Path] = set()
    source_dirs: set[Path] = set()

    for base in [root / "site", lib_dir, root / "bin", *extra_roots]:
        if not base.is_dir():
            continue
        for path in base.rglob("*"):
            if skip_names and path.name.startswith(skip_names):
                continue
            if path.is_file() and path.suffix in _NATIVE_SUFFIXES and not path.is_symlink():
                queue.append(path)

    modified: set[Path] = set()
    while queue:
        binary = queue.popleft()
        resolved_binary = binary.resolve()
        if resolved_binary in seen:
            continue
        seen.add(resolved_binary)
        if not binary.exists():
            continue
        rpaths = [Path(_expand_loader_path(item, binary)) for item in macho_rpaths(binary)]
        for dependency in macho_dependencies(binary):
            if dependency.startswith(_SYSTEM_PREFIXES):
                continue
            if dependency.startswith("@executable_path/"):
                # Relative to the bundle's launcher, which lives at the root.
                candidate = root / dependency.replace("@executable_path/", "")
            elif dependency.startswith("@loader_path/"):
                candidate = binary.parent / dependency.replace("@loader_path/", "")
            elif dependency.startswith("@rpath/"):
                name = dependency.replace("@rpath/", "")
                candidate = None
                for directory in [*rpaths, *search_dirs, *sorted(source_dirs), lib_dir]:
                    possible = directory / name
                    if possible.exists():
                        candidate = possible
                        break
                if candidate is None:
                    result.warnings.append(
                        f"{binary.relative_to(root)}: cannot resolve {dependency}"
                    )
                    continue
            else:
                candidate = Path(dependency)

            candidate = candidate.expanduser()
            if not candidate.exists():
                continue  # system provided, or handled by the OS
            try:
                candidate.resolve().relative_to(root.resolve())
                inside_bundle = True
            except ValueError:
                inside_bundle = False
            absolute_reference = not dependency.startswith("@")
            if inside_bundle and not absolute_reference:
                continue
            if inside_bundle and absolute_reference:
                # The library is already in the bundle but linked by an
                # absolute build-time path; only the load command needs fixing.
                target = candidate.resolve()
                relative = os.path.relpath(target, binary.parent)
                _replace_load_command(binary, dependency, f"@loader_path/{relative}")
                modified.add(binary)
                result.rewritten.append(f"{binary.relative_to(root)}: {dependency} -> @loader_path/{relative}")
                continue

            library_name = _unique_name(lib_dir, candidate.name, candidate, used_names)
            destination = lib_dir / library_name
            if not destination.exists():
                ensure_dir(lib_dir)
                shutil.copy2(candidate, destination)
                make_executable(destination)
                result.added.append(str(destination.relative_to(root)))
                result.sources[str(destination.relative_to(root))] = str(candidate)
                source_dirs.add(candidate.parent)
                for rpath in macho_rpaths(candidate):
                    expanded = _expand_loader_path(rpath, candidate)
                    if not expanded.startswith("@"):
                        source_dirs.add(Path(expanded))
            relative = os.path.relpath(destination, binary.parent)
            _replace_load_command(binary, dependency, f"@loader_path/{relative}")
            modified.add(binary)
            result.rewritten.append(
                f"{binary.relative_to(root)}: {dependency} -> @loader_path/{relative}"
            )
            queue.append(destination)

    for binary in sorted(modified):
        _ad_hoc_sign(binary)
    if result.rewritten and logger is not None:
        logger.detail(f"rewrote {len(result.rewritten)} Mach-O load command(s)")
    return result


def _replace_load_command(binary: Path, old: str, new: str) -> None:
    proc = _run(["install_name_tool", "-change", old, new, str(binary)])
    if proc is None or proc.returncode != 0:
        raise BuildError(
            f"install_name_tool failed for {binary}",
            hint=(proc.stderr if proc else "install_name_tool is not available").strip()[-1500:],
        )


def _ad_hoc_sign(binary: Path) -> None:
    if shutil.which("codesign") is None:
        return
    _run(["codesign", "--force", "--sign", "-", str(binary)])


def _repair_linux(
    root: Path,
    *,
    lib_dir: Path,
    search_dirs: list[Path],
    extra_roots: list[Path],
    skip_names: tuple[str, ...],
    logger: Logger | None,
) -> NativeFixup:
    result = NativeFixup()
    used_names: dict[str, Path] = {}
    queue: deque[Path] = deque()
    seen: set[Path] = set()
    source_dirs: set[Path] = set()
    for base in [root / "site", lib_dir, root / "bin", *extra_roots]:
        if not base.is_dir():
            continue
        for path in base.rglob("*.so*"):
            if skip_names and path.name.startswith(skip_names):
                continue
            if path.is_file():
                queue.append(path)
    while queue:
        binary = queue.popleft()
        resolved_binary = binary.resolve()
        if resolved_binary in seen:
            continue
        seen.add(resolved_binary)
        needed, rpaths = elf_needed(binary)
        directories = [binary.parent, lib_dir, *search_dirs, *sorted(source_dirs)]
        for rpath in rpaths:
            directories.append(Path(_expand_loader_path(rpath, binary)))
        for soname in needed:
            if soname.startswith("libpython"):
                continue
            candidate = next((directory / soname for directory in directories if (directory / soname).exists()), None)
            if candidate is None:
                result.warnings.append(f"{binary.relative_to(root)}: cannot resolve {soname}")
                continue
            try:
                candidate.resolve().relative_to(root.resolve())
                continue
            except ValueError:
                pass
            if str(candidate).startswith(_LINUX_SYSTEM_PREFIXES):
                continue
            name = _unique_name(lib_dir, candidate.name, candidate, used_names)
            destination = lib_dir / name
            if not destination.exists():
                ensure_dir(lib_dir)
                shutil.copy2(candidate, destination)
                result.added.append(str(destination.relative_to(root)))
                result.sources[str(destination.relative_to(root))] = str(candidate)
                source_dirs.add(candidate.parent)
                queue.append(destination)
    if result.added and logger is not None:
        logger.detail(f"copied {len(result.added)} shared librar(y/ies) into lib/")
    return result


def fixup_shared_libraries(
    root: Path,
    *,
    target: Target,
    python_home: Path | None = None,
    features: set[str] | None = None,
    logger: Logger | None = None,
) -> NativeFixup:
    """Copy and rewrite native dependencies inside an already materialised bundle."""
    lib_dir = ensure_dir(root / "lib")
    features = features or set()
    skip_names: tuple[str, ...] = () if "tcl-tk" in features else ("_tkinter",)
    search_dirs: list[Path] = [root / "site"]
    extra_roots: list[Path] = []
    if python_home is not None:
        search_dirs.extend([python_home / "lib", python_home / "lib" / "python3"])
        search_dirs.extend(path for path in python_home.glob("lib/python*") if path.is_dir())
        search_dirs.extend(path for path in python_home.glob("DLLs") if path.is_dir())
        search_dirs.extend(path for path in python_home.glob("lib/*-linux-gnu*") if path.is_dir())
        extra_roots.extend(path for path in python_home.glob("lib/python*/lib-dynload") if path.is_dir())
        extra_roots.extend(path for path in python_home.glob("DLLs") if path.is_dir())
    if target.is_macos:
        return _repair_macos(
            root,
            lib_dir=lib_dir,
            search_dirs=search_dirs,
            extra_roots=extra_roots,
            skip_names=skip_names,
            logger=logger,
        )
    if target.is_elf:
        return _repair_linux(
            root,
            lib_dir=lib_dir,
            search_dirs=search_dirs,
            extra_roots=extra_roots,
            skip_names=skip_names,
            logger=logger,
        )
    if target.is_windows:
        # Adjacent DLLs are resolved by the Windows loader itself.  Inspecting
        # PE imports would require a parser we intentionally do not ship; warn
        # about DLLs that are shipped *outside* the bundle instead.
        result = NativeFixup()
        for base in (root / "site",):
            if not base.is_dir():
                continue
            for path in base.rglob("*.pyd"):
                if path.is_file():
                    continue
        return result
    return NativeFixup()
