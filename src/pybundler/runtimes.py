"""Provisioning of relocatable CPython runtimes.

To produce a bundle that works on a machine without Python installed, the
interpreter itself has to be shipped.  PyBundler uses `python-build-standalone`
(PBS) builds, which are:

* relocatable (Mach-O ``@rpath`` / ELF ``$ORIGIN`` based),
* statically complete (stdlib, libpython, Tcl/Tk where applicable),
* permissively licensed (PSF-2.0 for CPython, plus MIT/BSD/Apache components).

Downloads are cached per ``(version, triple, tag)`` in the user cache directory
and verified against the SHA-256 digest published by GitHub.
"""

from __future__ import annotations

import json
import os
import platform
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from . import __version__
from .errors import RuntimeProvisionError
from .fsutil import (
    default_cache_dir,
    ensure_dir,
    extract_tar_gz,
    human_size,
    iter_files,
    make_executable,
    remove_tree,
    sha256_file,
    write_json,
)
from .interp import EnvInfo, probe_interpreter
from .log import Logger
from .targets import Target, host_target

PBS_REPO = "astral-sh/python-build-standalone"
#: Pinned for reproducibility.  Override with --pbs-tag or PYBUNDLER_PBS_TAG.
PBS_TAG = "20261124"
API_ROOT = f"https://api.github.com/repos/{PBS_REPO}"

_VERSION_RE = re.compile(r"^cpython-(?P<version>\d+\.\d+\.\d+)\+(?P<tag>[^-]+)-(?P<triple>.+?)-(?P<kind>install_only_stripped|install_only|pgo\+lto-full|debug-full)\.tar\.(?:gz|zst)$")


@dataclass(frozen=True)
class Runtime:
    """A provisioned interpreter plus where it came from."""

    root: Path
    python: Path
    env: EnvInfo
    source: str
    tag: str | None = None
    asset: str | None = None

    @property
    def version(self) -> str:
        return self.env.version

    @property
    def is_downloaded(self) -> bool:
        return self.source.startswith("download")

    def describe(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "python": str(self.python),
            "version": self.env.version,
            "source": self.source,
            "tag": self.tag,
            "asset": self.asset,
        }


# ---------------------------------------------------------------------------
# Cache locations
# ---------------------------------------------------------------------------


def cache_dir() -> Path:
    return default_cache_dir()


def runtimes_dir() -> Path:
    return ensure_dir(cache_dir() / "runtimes")


def downloads_dir() -> Path:
    return ensure_dir(cache_dir() / "downloads")


def _runtime_dir(version: str, triple: str, tag: str) -> Path:
    return runtimes_dir() / f"cpython-{version}-{triple}-{tag}"


def pbs_available(target: Target) -> bool:
    """python-build-standalone publishes binaries for these OS/arch pairs."""
    if target.is_macos:
        return target.arch in ("x86_64", "aarch64")
    if target.is_windows:
        return target.arch in ("x86_64", "x86", "i686", "aarch64")
    if target.is_linux:
        # PBS does not publish 32-bit Linux builds; use --runtime local there.
        return target.arch in ("x86_64", "aarch64", "riscv64")
    return False


# ---------------------------------------------------------------------------
# Local interpreter discovery
# ---------------------------------------------------------------------------


def local_runtime(python: Path | None = None, *, home: Path | None = None) -> Runtime:
    """Describe the interpreter running the build as a candidate runtime."""
    python = (python or Path(sys.executable)).expanduser()
    env = probe_interpreter(python, home=home)
    root = Path(env.base_prefix).resolve()
    if env.is_venv:
        for name in (f"python{env.major_minor_str}", "python3", "python"):
            possible = root / "bin" / name
            if possible.exists():
                python = possible
                break
    python = python.resolve()
    if home is None:
        # Probe again with PYTHONHOME pointing at the canonical prefix so that
        # every reported path (stdlib, purelib, ...) is inside the tree we will
        # copy, even for framework builds with symlinked prefixes.
        try:
            env = probe_interpreter(python, home=root)
        except Exception:
            env = probe_interpreter(python)
    return Runtime(root=root, python=python, env=env, source="local")


def _otool_libraries(binary: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["otool", "-L", str(binary)], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    libraries: list[str] = []
    for line in out.stdout.splitlines()[1:]:
        line = line.strip()
        if not line:
            continue
        libraries.append(line.split(" (compatibility")[0].strip())
    return libraries


_SYSTEM_PREFIXES = ("/usr/lib/", "/System/", "/Library/Frameworks/", "@executable_path/", "@loader_path/")


_SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/System/",
    "/usr/libexec/",
)

#: Absolute Mach-O references are only portable when they point at the OS.
_RELOCATABLE_SYSTEM_PREFIXES = (
    "/usr/lib/",
    "/System/",
    "/usr/libexec/",
)


def _rpaths(binary: Path) -> list[str]:
    try:
        out = subprocess.run(
            ["otool", "-l", str(binary)], capture_output=True, text=True, timeout=60
        )
    except (OSError, subprocess.SubprocessError):
        return []
    if out.returncode != 0:
        return []
    return [match.group(1) for match in re.finditer(r"path\s+(\S+)\s+\(offset", out.stdout)]


def looks_relocatable(runtime: Runtime) -> bool:
    """Heuristic: can this interpreter tree be moved to another machine?

    Homebrew and python.org framework builds record absolute library paths that
    happen to exist on the build machine, so they are *not* relocatable.
    python-build-standalone uses ``@rpath``/``$ORIGIN`` and passes.
    """
    if runtime.source != "local":
        return True
    if runtime.env.sysname == "darwin":
        binaries = [runtime.python]
        binaries.extend(sorted(runtime.root.glob("lib/libpython*.dylib")))
        for binary in binaries:
            if not binary.exists():
                continue
            for lib in _otool_libraries(binary):
                if lib.startswith("@"):
                    continue
                if lib.startswith(_RELOCATABLE_SYSTEM_PREFIXES):
                    continue
                return False
            for rpath in _rpaths(binary):
                if not rpath.startswith("@"):
                    return False
        return True
    if runtime.env.sysname.startswith(("linux", "freebsd")):
        for binary in [runtime.python, *sorted(runtime.root.glob("lib/libpython*.so*"))]:
            try:
                out = subprocess.run(
                    ["readelf", "-d", str(binary)], capture_output=True, text=True, timeout=60
                )
            except (OSError, subprocess.SubprocessError):
                return False
            if out.returncode != 0:
                return False
            for line in out.stdout.splitlines():
                stripped = line.strip()
                if stripped.startswith(("RPATH", "RUNPATH")) and "$ORIGIN" not in stripped:
                    return False
        return True
    return True  # Windows installers ship pythonXY.dll next to python.exe


# ---------------------------------------------------------------------------
# python-build-standalone release metadata and download
# ---------------------------------------------------------------------------


def _http_json(url: str, *, timeout: float = 60.0) -> Any:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"pybundler/{__version__}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _release_cache_path(tag: str) -> Path:
    return ensure_dir(cache_dir() / "releases") / f"{tag}.json"


def fetch_release(tag: str | None, *, logger: Logger | None = None) -> tuple[str, list[dict[str, Any]]]:
    """Return ``(tag, assets)`` for a PBS release, caching the API response."""
    tag = tag or os.environ.get("PYBUNDLER_PBS_TAG") or PBS_TAG
    cached = _release_cache_path(tag)
    if cached.exists():
        try:
            payload = json.loads(cached.read_text("utf-8"))
            return tag, payload["assets"]
        except (OSError, ValueError, KeyError):
            pass
    url = f"{API_ROOT}/releases/tags/{tag}"
    try:
        payload = _http_json(url)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise RuntimeProvisionError(
                f"unknown python-build-standalone release {tag!r}",
                hint="pass --pbs-tag with a release from "
                "https://github.com/astral-sh/python-build-standalone/releases",
            ) from exc
        raise RuntimeProvisionError(f"cannot query GitHub releases: HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise RuntimeProvisionError(
            f"cannot reach GitHub to download a Python runtime: {exc}",
            hint=(
                "check your network, or bundle the interpreter you already use with "
                "`--runtime local` / `python.runtime_path` in the spec"
            ),
        ) from exc
    assets = payload.get("assets", [])
    if not isinstance(assets, list):
        raise RuntimeProvisionError(f"unexpected release metadata for {tag!r}")
    write_json(
        cached,
        {
            "tag": tag,
            "fetched": int(time.time()),
            "assets": [
                {
                    "name": asset.get("name"),
                    "size": asset.get("size", 0),
                    "digest": asset.get("digest"),
                    "browser_download_url": asset.get("browser_download_url"),
                }
                for asset in assets
            ],
        },
    )
    return tag, assets


def _select_asset(
    assets: Iterable[Mapping[str, Any]], version: str | None, triple: str
) -> Mapping[str, Any]:
    candidates: list[tuple[tuple[int, ...], Mapping[str, Any]]] = []
    for asset in assets:
        name = str(asset.get("name") or "")
        match = _VERSION_RE.match(name)
        if not match or match.group("kind") not in ("install_only_stripped", "install_only"):
            continue
        if match.group("triple") != triple:
            continue
        full = match.group("version")
        if version and not _version_matches(version, full):
            continue
        # Prefer stripped builds when both exist for the same version.
        rank = (tuple(int(p) for p in full.split(".")), match.group("kind") == "install_only_stripped")
        candidates.append((rank, asset))
    if not candidates:
        available = sorted(
            {
                m.group("version")
                for asset in assets
                if (m := _VERSION_RE.match(str(asset.get("name") or "")))
                and m.group("triple") == triple
                and m.group("kind") in ("install_only_stripped", "install_only")
            },
            key=lambda v: tuple(int(p) for p in v.split(".")),
        )
        wanted = version or "any"
        raise RuntimeProvisionError(
            f"no standalone Python {wanted} runtime published for {triple}",
            hint=(
                "available versions: " + ", ".join(available[-12:])
                if available
                else "check the python-build-standalone release"
            ),
        )
    candidates.sort(key=lambda item: item[0])
    return candidates[-1][1]


def _version_matches(requested: str, full: str) -> bool:
    requested_parts = requested.split(".")
    full_parts = full.split(".")
    if len(requested_parts) > 3:
        return False
    return requested_parts == full_parts[: len(requested_parts)]


def _download(url: str, dest: Path, *, expected_size: int | None, logger: Logger) -> None:
    ensure_dir(dest.parent)
    tmp = dest.with_name(dest.name + ".part")
    request = urllib.request.Request(url, headers={"User-Agent": f"pybundler/{__version__}"})
    logger.detail(f"downloading {url}")
    try:
        with urllib.request.urlopen(request, timeout=120) as response, tmp.open("wb") as out:
            total = int(response.headers.get("Content-Length") or 0) or (expected_size or 0)
            read = 0
            last_report = 0.0
            while True:
                chunk = response.read(1024 * 256)
                if not chunk:
                    break
                out.write(chunk)
                read += len(chunk)
                now = time.monotonic()
                if logger.verbosity >= 4 and now - last_report > 1.0:
                    last_report = now
                    if total:
                        logger.detail(f"  {human_size(read)} / {human_size(total)}")
                    else:
                        logger.detail(f"  {human_size(read)}")
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        tmp.unlink(missing_ok=True)
        raise RuntimeProvisionError(
            f"failed to download Python runtime: {exc}",
            hint="check your network connection and try again, or use --runtime local",
        ) from exc
    os.replace(tmp, dest)


class _Lock:
    """Very small cross-process lock used while populating the cache."""

    def __init__(self, path: Path, *, timeout: float = 600.0) -> None:
        self.path = path
        self.timeout = timeout

    def __enter__(self) -> "_Lock":
        deadline = time.monotonic() + self.timeout
        while True:
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                return self
            except FileExistsError:
                try:
                    age = time.time() - self.path.stat().st_mtime
                except OSError:
                    age = 0
                if age > 900:
                    self.path.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise RuntimeProvisionError(
                        "timed out waiting for another PyBundler process to finish "
                        f"populating {self.path.parent}"
                    )
                time.sleep(0.1)

    def __exit__(self, *exc: object) -> None:
        self.path.unlink(missing_ok=True)


def _find_python_executable(root: Path, version: str) -> Path:
    major_minor = ".".join(version.split(".")[:2])
    candidates = [
        root / "python.exe",
        root / "bin" / f"python{major_minor}",
        root / "bin" / "python3",
        root / "bin" / "python",
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    for pattern in (f"bin/python{major_minor}", "bin/python3*", "python3*.exe"):
        for candidate in sorted(root.glob(pattern)):
            if candidate.is_file():
                return candidate
    raise RuntimeProvisionError(f"standalone runtime at {root} does not contain a Python executable")


def ensure_runtime(
    *,
    version: str | None = None,
    target: Target | None = None,
    source: str = "auto",
    local_python: Path | None = None,
    local_root: Path | None = None,
    tag: str | None = None,
    logger: Logger,
    allow_download: bool = True,
) -> Runtime:
    """Return a usable runtime, downloading and extracting it if necessary.

    Parameters
    ----------
    version:
        Requested ``X.Y`` or ``X.Y.Z``.  ``None`` means "the build
        environment's version".
    source:
        ``auto`` (prefer a relocatable local interpreter, otherwise download),
        ``download`` (always fetch PBS), or ``local`` (copy from
        ``local_python`` / ``local_root``).
    """
    target = target or host_target()
    if version is None:
        version = platform.python_version()
    version = _normalise_version(version)

    if not pbs_available(target):
        if source == "download":
            if target.is_freebsd:
                hint = (
                    "python-build-standalone publishes no FreeBSD builds. Use the "
                    "interpreter from ports/pkg: `--runtime local` (or "
                    "`python.runtime = \"local\"`)."
                )
            elif target.is_linux and target.is_32bit:
                hint = (
                    "python-build-standalone publishes no 32-bit Linux builds. "
                    "Use a 32-bit interpreter installed on the machine with "
                    "`--runtime local`."
                )
            else:
                hint = (
                    f"no standalone build exists for {target.triple}; "
                    "use a matching local interpreter with `--runtime local`."
                )
            raise RuntimeProvisionError(
                f"there is no python-build-standalone runtime for {target.triple}",
                hint=hint,
            )
        if source == "auto":
            source = "local"

    if source == "local":
        if local_root is not None:
            python = _find_python_executable(local_root, version)
            env = probe_interpreter(python, home=local_root)
            runtime = Runtime(root=local_root, python=python, env=env, source="local")
        else:
            runtime = local_runtime(local_python)
        if not _version_matches(version, runtime.version):
            raise RuntimeProvisionError(
                f"local runtime is Python {runtime.version}, but {version} was requested",
                hint="use --runtime download, or build with an interpreter of the requested version",
            )
        if not looks_relocatable(runtime):
            logger.warn(
                "the local interpreter does not look relocatable; the bundle will "
                "depend on libraries installed on this machine. Use "
                "`--runtime download` for portable builds."
            )
        return runtime

    if source == "auto":
        candidate = None
        try:
            candidate = local_runtime(local_python)
        except Exception:  # pragma: no cover - probing failures are not fatal here
            candidate = None
        if not pbs_available(target):
            if candidate is None or not _version_matches(version, candidate.version):
                raise RuntimeProvisionError(
                    f"no usable local Python {version} interpreter was found on {target.sysname}",
                    hint=(
                        "install it with pkg/ports (e.g. `pkg install python311`) and make "
                        "sure it is the interpreter PyBundler is running with"
                    ),
                )
            if not looks_relocatable(candidate):
                logger.warn(
                    "the system Python is not fully relocatable; PyBundler copies its "
                    "runtime files and ELF dependencies, but native libraries loaded from "
                    "absolute paths may require the same packages on the target machine"
                )
            else:
                logger.detail(f"using local interpreter {candidate.python}")
            return candidate
        if (
            candidate is not None
            and _version_matches(version, candidate.version)
            and looks_relocatable(candidate)
        ):
            logger.detail(f"using relocatable local interpreter {candidate.python}")
            return candidate
        if not allow_download:
            raise RuntimeProvisionError(
                "no relocatable local interpreter available and downloads are disabled",
                hint="pass --runtime-path with a python-build-standalone runtime",
            )

    if not allow_download:
        raise RuntimeProvisionError("runtime downloads are disabled")

    resolved_tag, assets = fetch_release(tag, logger=logger)
    asset = _select_asset(assets, version, target.triple)
    name = str(asset["name"])
    full_version = _VERSION_RE.match(name).group("version")  # type: ignore[union-attr]
    destination = _runtime_dir(full_version, target.triple, resolved_tag)

    lock = _Lock(destination.with_suffix(".lock"))
    with lock:
        runtime = _load_cached_runtime(destination, full_version, target.triple, resolved_tag)
        if runtime is not None:
            logger.detail(f"using cached runtime {destination}")
            return runtime
        if destination.exists():
            logger.warn(f"removing incomplete runtime cache entry {destination}")
            remove_tree(destination)
        archive = downloads_dir() / name
        if not archive.exists() or _digest_mismatch(archive, asset.get("digest")):
            _download(
                str(asset["browser_download_url"]),
                archive,
                expected_size=int(asset.get("size") or 0),
                logger=logger,
            )
        digest = _verify_digest(archive, asset.get("digest"), name)
        logger.detail(f"extracting {name} ({human_size(archive.stat().st_size)})")
        staging = destination.with_name(destination.name + f".tmp.{os.getpid()}")
        remove_tree(staging)
        try:
            extract_tar_gz(archive, staging, strip_components=1)
            root = staging
            python = _find_python_executable(root, full_version)
            env = probe_interpreter(python, home=root)
            if env.version.split(".")[:2] != full_version.split(".")[:2]:
                raise RuntimeProvisionError(
                    f"downloaded runtime reports Python {env.version}, expected {full_version}"
                )
            marker = {
                "version": env.version,
                "triple": target.triple,
                "tag": resolved_tag,
                "asset": name,
                "sha256": digest,
                "installed": int(time.time()),
            }
            write_json(root / "PYBUNDLER_RUNTIME.json", marker)
            make_executable(root / "bin" / python.name if python.parent.name == "bin" else python)
            if destination.exists():
                remove_tree(destination)
            os.replace(staging, destination)
        except Exception:
            remove_tree(staging)
            raise
        python = _find_python_executable(destination, full_version)
        env = probe_interpreter(python, home=destination)
        logger.ok(f"runtime Python {env.version} ready ({human_size(directory_size(destination))})")
        return Runtime(
            root=destination,
            python=python,
            env=env,
            source=f"download:{resolved_tag}",
            tag=resolved_tag,
            asset=name,
        )


def _normalise_version(version: str) -> str:
    parts = version.strip().split(".")
    if not 2 <= len(parts) <= 3 or not all(part.isdigit() for part in parts):
        raise RuntimeProvisionError(
            f"invalid Python version {version!r}",
            hint="use a version such as 3.12 or 3.12.16",
        )
    return version.strip()


def _load_cached_runtime(root: Path, version: str, triple: str, tag: str) -> Runtime | None:
    marker = root / "PYBUNDLER_RUNTIME.json"
    if not marker.exists():
        return None
    try:
        python = _find_python_executable(root, version)
        env = probe_interpreter(python, home=root)
        if env.version.split(".")[:2] != version.split(".")[:2]:
            return None
    except Exception:
        return None
    source = f"download:{tag}"
    return Runtime(root=root, python=python, env=env, source=source, tag=tag)


def _digest_mismatch(archive: Path, digest: str | None) -> bool:
    if not digest or not digest.startswith("sha256:"):
        return False
    expected = digest.split(":", 1)[1]
    try:
        return sha256_file(archive) != expected
    except OSError:
        return True


def _verify_digest(archive: Path, digest: str | None, name: str) -> str | None:
    actual = sha256_file(archive)
    if digest and digest.startswith("sha256:"):
        expected = digest.split(":", 1)[1]
        if actual != expected:
            archive.unlink(missing_ok=True)
            raise RuntimeProvisionError(
                f"checksum mismatch for downloaded runtime {name}",
                hint="the download was corrupted; run the build again",
            )
    return actual


def directory_size(root: Path) -> int:
    total = 0
    for path in iter_files(root):
        try:
            total += path.lstat().st_size
        except OSError:  # pragma: no cover
            pass
    return total


# ---------------------------------------------------------------------------
# Cache management (used by `pybundler cache`)
# ---------------------------------------------------------------------------


def list_cached_runtimes() -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    root = runtimes_dir()
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.endswith(".lock"):
            continue
        marker = path / "PYBUNDLER_RUNTIME.json"
        if marker.exists():
            try:
                data = json.loads(marker.read_text("utf-8"))
            except (OSError, ValueError):
                data = {}
        else:
            data = {}
        entries.append(
            {
                "path": str(path),
                "name": path.name,
                "version": data.get("version"),
                "tag": data.get("tag"),
                "size": directory_size(path),
            }
        )
    return entries


def prune_runtimes(*, keep: int = 2, logger: Logger | None = None) -> list[Path]:
    """Remove all but the *keep* most recent cached runtimes."""
    entries = [entry for entry in list_cached_runtimes() if entry["version"]]
    entries.sort(key=lambda entry: entry["path"], reverse=True)
    removed: list[Path] = []
    for entry in entries[keep:]:
        path = Path(entry["path"])
        remove_tree(path)
        removed.append(path)
    if logger:
        for path in removed:
            logger.info(f"removed {path}")
    return removed
