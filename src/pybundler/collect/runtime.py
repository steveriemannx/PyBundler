"""Trimming and planning of the bundled CPython runtime.

The runtime is copied almost verbatim, because CPython's standard library
relies on modules that are imported lazily or through extension modules.  What
we *do* remove is well understood and safe:

* test suites, ``idlelib``, ``ensurepip`` and other development-only trees,
* ``site-packages`` (third-party code is collected separately),
* headers, static libraries, pkg-config files and CLI wrappers,
* Tcl/Tk unless the application actually uses ``tkinter``.

Keeping the whole stdlib is a deliberate trade-off: bundles are a few MiB
larger than PyInstaller's, but "works on first try" beats aggressive pruning
that breaks on a lazily imported codec or platform module.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from ..errors import BuildError
from ..fsutil import human_size
from ..log import Logger
from ..runtimes import Runtime
from ..targets import Target
from ..build.plan import Payload

#: Stdlib directories that are never needed at runtime.
BLOCKED_STDLIB_DIRS = {
    "test",
    "tests",
    "idlelib",
    "turtledemo",
    "ensurepip",
    "lib2to3",
    "site-packages",
    "dist-packages",
    "config",
    "__pycache__",
    "__phello__",
}

#: Directories at the runtime root that only matter for building.
BLOCKED_ROOT_DIRS = {"include", "share", "pkgconfig", "doc", "docs"}


@dataclass(frozen=True)
class RuntimeLayout:
    """Where things ended up inside the bundle (paths relative to bundle root)."""

    home_rel: str
    exe_rel: str
    stdlib_rel: str
    version: str
    is_windows: bool

    def as_dict(self) -> dict[str, object]:
        return {
            "home": self.home_rel,
            "executable": self.exe_rel,
            "stdlib": self.stdlib_rel,
            "version": self.version,
        }


def plan_runtime(
    runtime: Runtime,
    *,
    target: Target,
    features: set[str],
    payload: Payload,
    prefix: str = "runtime/python",
    logger: Logger | None = None,
) -> tuple[RuntimeLayout, list[str]]:
    """Add a trimmed runtime tree to *payload* and describe its layout."""
    root = runtime.root
    if not root.is_dir():
        raise BuildError(f"runtime directory disappeared: {root}")
    tcl_tk = "tcl-tk" in features
    local_prefix = runtime.source == "local"
    major_minor = runtime.env.major_minor_str
    stdlib_name = f"python{major_minor}"
    warnings: list[str] = []
    added = 0

    def local_dir_allowed(relative: PurePosixPath) -> bool:
        """When the runtime is a system prefix (/usr, /usr/local, a framework or
        a venv base), copy only the interpreter's own files, not every package
        installed beside it."""
        parts = relative.parts
        if not parts:
            return True
        if len(parts) == 1:
            top = parts[0]
            if top in ("bin", "Lib", "DLLs", "lib"):
                return True
            if tcl_tk and top.startswith(("tcl", "tk")):
                return True
            return False
        if parts[0] == "lib" and len(parts) == 2:
            name = parts[1]
            if name == stdlib_name or name.startswith("libpython"):
                return True
            if "linux-gnu" in name:  # Debian/Ubuntu multiarch: libpython lives here
                return True
            if tcl_tk and name.startswith(("tcl", "tk", "itcl", "thread")):
                return True
            return False
        if parts[0] == "lib" and len(parts) == 3 and "linux-gnu" in parts[1]:
            # Only libpython* files directly inside the multiarch directory.
            return False
        return True

    def local_file_allowed(relative: PurePosixPath) -> bool:
        parts = relative.parts
        name = relative.name
        if len(parts) == 1:
            lowered = name.lower()
            return (
                (lowered.startswith(("python", "pythonw")) and lowered.endswith((".exe", ".dll")))
                or lowered.startswith("vcruntime")
            )
        if parts[0] == "lib" and len(parts) == 2:
            if name.startswith("libpython"):
                return True
            if "linux-gnu" in parts[1]:
                return name.startswith("libpython")
            if tcl_tk and name.startswith(("libtcl", "libtk", "libitcl", "libthread")):
                return True
            return False
        if parts[0] == "lib" and len(parts) == 3 and "linux-gnu" in parts[1]:
            return name.startswith("libpython")
        return True

    def skip_dir(relative: PurePosixPath) -> bool:
        parts = relative.parts
        if not parts:
            return False
        if any(part == "__pycache__" for part in parts):
            return True
        if parts[0] in BLOCKED_ROOT_DIRS:
            return True
        if parts[0] == "lib" and len(parts) >= 2 and parts[1] == "pkgconfig":
            return True
        # Stdlib root: lib/pythonX.Y on POSIX, Lib on Windows.
        if parts[0] == "lib" and len(parts) >= 3 and parts[1].startswith("python"):
            subject = parts[2]
            if subject in BLOCKED_STDLIB_DIRS:
                return True
            if subject.startswith("config-"):
                return True
            if subject in ("tkinter",) and not tcl_tk:
                return True
            if not tcl_tk and subject.startswith(("tcl", "tk", "itcl", "thread")):
                return True
        if parts[0] == "Lib" and len(parts) >= 2:
            subject = parts[1]
            if subject in BLOCKED_STDLIB_DIRS:
                return True
            if subject == "tkinter" and not tcl_tk:
                return True
        if not tcl_tk and parts[0] in ("tcl", "tk", "tcl8", "tk8"):
            return True
        if not tcl_tk and parts[0].startswith(("tcl", "tk")):
            return True
        if parts[0] == "lib" and len(parts) >= 2:
            name = parts[1]
            if not tcl_tk and name.startswith(("tcl", "tk", "itcl", "thread")):
                return True
        return False

    def skip_file(relative: PurePosixPath) -> bool:
        name = relative.name
        if name in (".DS_Store", "Thumbs.db") or name.endswith((".pyc", ".pyo", ".a", ".la", ".lib", ".exp")):
            return True
        if not tcl_tk and name.startswith("_tkinter"):
            return True
        parts = relative.parts
        if parts and parts[0] == "bin" and not tcl_tk:
            if not name.startswith("python"):
                return True
        if parts and parts[0] == "lib" and len(parts) >= 2:
            if not tcl_tk and parts[1].startswith(("libtcl", "libtk", "libitcl")):
                return True
            if name.endswith(".pc"):
                return True
        if parts and parts[0] == "share":
            return True
        return False

    for directory, dirnames, filenames in os.walk(root, followlinks=False):
        directory_path = Path(directory)
        relative_dir = PurePosixPath(directory_path.relative_to(root).as_posix())
        keep_dirs = []
        for name in sorted(dirnames):
            child = relative_dir / name if relative_dir.parts else PurePosixPath(name)
            path = directory_path / name
            if local_prefix and not local_dir_allowed(child):
                continue
            if path.is_symlink():
                if skip_dir(child):
                    continue
                link = os.readlink(path)
                if os.path.isabs(link) or ".." in link.split("/"):
                    warnings.append(f"skipping unsafe directory symlink in runtime: {child} -> {link}")
                    continue
                payload.add_symlink(link, f"{prefix}/{child.as_posix()}")
                added += 1
                continue
            if skip_dir(child):
                continue
            keep_dirs.append(name)
        dirnames[:] = keep_dirs
        for name in sorted(filenames):
            path = directory_path / name
            relative = relative_dir / name if relative_dir.parts else PurePosixPath(name)
            if local_prefix and not local_file_allowed(relative):
                continue
            if skip_file(relative):
                continue
            dest = f"{prefix}/{relative.as_posix()}"
            if path.is_symlink():
                link = os.readlink(path)
                if os.path.isabs(link):
                    warnings.append(
                        f"skipping absolute symlink in runtime: {relative} -> {link}"
                    )
                    continue
                payload.add_symlink(link, dest)
                added += 1
                continue
            if not path.is_file():
                continue
            mode = stat.S_IMODE(path.stat().st_mode)
            payload.add_file(path, dest, mode=mode, origin="runtime")
            added += 1

    if logger is not None:
        logger.detail(f"runtime payload: {added} files, {human_size(payload.total_size())}")

    python_rel = _relative_to_root(runtime.python, root, "Python executable")
    stdlib_rel = runtime.env.stdlib
    stdlib_relative = _relative_to_root(stdlib_rel, root, "standard library", required=False)
    if stdlib_relative is None:
        stdlib_relative = Path("lib") / f"python{runtime.env.major_minor_str}"
        warnings.append(
            "the bundled interpreter's stdlib lives outside its prefix; "
            f"assuming {stdlib_relative} inside the bundle"
        )
    exe_rel = f"{prefix}/{python_rel.as_posix()}"
    stdlib_bundle_rel = f"{prefix}/{stdlib_relative.as_posix()}"
    if not any(item.dest == exe_rel for item in payload.items.values()):
        raise BuildError(
            f"the Python executable {python_rel} was removed by runtime trimming",
            hint="this is a bug in PyBundler; please report the runtime layout",
        )
    layout = RuntimeLayout(
        home_rel=prefix,
        exe_rel=exe_rel,
        stdlib_rel=stdlib_bundle_rel,
        version=runtime.version,
        is_windows=target.is_windows,
    )
    return layout, warnings


def _relative_to_root(path: Path, root: Path, what: str, *, required: bool = True) -> Path | None:
    for candidate, base in ((path, root), (path.resolve(), root.resolve())):
        try:
            return candidate.relative_to(base)
        except ValueError:
            continue
    if not required:
        return None
    raise BuildError(
        f"the runtime's {what} ({path}) is outside the runtime root ({root})",
        hint=(
            "this interpreter cannot be relocated. Use `--runtime download` "
            "or point --runtime-path at a python-build-standalone runtime."
        ),
    )
