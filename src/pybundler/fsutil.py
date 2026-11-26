"""Filesystem helpers shared by the collector and bundler.

All writes that matter for reproducibility are copy-then-rename so that a
crash never leaves a half written file behind, and every archive extraction is
validated against path traversal.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Iterable, Iterator

from .errors import BuildError

CHUNK = 1024 * 1024


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return True


def copy_file(
    src: Path,
    dst: Path,
    *,
    mode: int | None = None,
    preserve_mtime: bool = True,
) -> None:
    """Copy a single regular file, creating parent directories as needed."""
    ensure_dir(dst.parent)
    if src.is_symlink() and not src.exists():
        raise BuildError(f"broken symlink while bundling: {src}")
    tmp = dst.with_name(dst.name + ".pbtmp")
    shutil.copyfile(src, tmp, follow_symlinks=True)
    if mode is not None:
        os.chmod(tmp, mode)
    elif preserve_mtime:
        shutil.copystat(src, tmp, follow_symlinks=True)
    os.replace(tmp, dst)


def write_text(dst: Path, text: str, *, mode: int = 0o644) -> None:
    ensure_dir(dst.parent)
    tmp = dst.with_name(dst.name + ".pbtmp")
    tmp.write_text(text, encoding="utf-8")
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def write_bytes(dst: Path, data: bytes, *, mode: int = 0o644) -> None:
    ensure_dir(dst.parent)
    tmp = dst.with_name(dst.name + ".pbtmp")
    tmp.write_bytes(data)
    os.chmod(tmp, mode)
    os.replace(tmp, dst)


def write_json(dst: Path, data: object, *, mode: int = 0o644) -> None:
    write_text(dst, json.dumps(data, indent=2, sort_keys=True) + "\n", mode=mode)


def make_executable(path: Path) -> None:
    mode = path.stat().st_mode
    path.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def iter_files(root: Path) -> Iterator[Path]:
    """Yield every regular file below *root*, following file symlinks.

    Following symlinks is required in practice: distro packages (Homebrew, Nix,
    Debian) install module files as symlinks into a versioned store.  Directory
    symlink cycles are prevented by tracking resolved directories.
    """
    seen_dirs: set[str] = set()
    for dirpath, dirnames, filenames in os.walk(root, followlinks=True):
        resolved_dir = os.path.realpath(dirpath)
        if resolved_dir in seen_dirs:
            dirnames[:] = []
            continue
        seen_dirs.add(resolved_dir)
        kept: list[str] = []
        for name in sorted(dirnames):
            child = os.path.join(dirpath, name)
            if os.path.realpath(child) in seen_dirs:
                continue
            kept.append(name)
        dirnames[:] = kept
        for name in sorted(filenames):
            path = Path(dirpath) / name
            if path.is_file():  # follows symlinks; broken links are skipped
                yield path


def tree_size(root: Path) -> int:
    total = 0
    for path in iter_files(root):
        try:
            total += path.stat().st_size
        except OSError:  # pragma: no cover - races with other tooling
            pass
    return total


def human_size(num: int) -> str:
    value = float(num)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f} {unit}" if unit != "B" else f"{int(value)} B"
        value /= 1024
    return f"{value:.1f} TiB"  # pragma: no cover


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def remove_tree(path: Path) -> None:
    if path.is_symlink() or path.is_file():
        path.unlink(missing_ok=True)
    elif path.exists():
        shutil.rmtree(path)


def _validate_archive_member(member: tarfile.TarInfo, dest: Path) -> None:
    name = member.name
    if name.startswith(("/", "\\")) or ".." in Path(name).parts:
        raise BuildError(f"refusing to extract unsafe path from archive: {name!r}")
    target = dest / name
    if not is_within(target, dest):
        raise BuildError(f"refusing to extract path outside of destination: {name!r}")
    if member.issym() or member.islnk():
        link = member.linkname
        if link.startswith(("/", "\\")) or ".." in Path(link).parts:
            raise BuildError(f"refusing to extract unsafe symlink: {name!r} -> {link!r}")


def extract_tar_gz(archive: Path, dest: Path, *, strip_components: int = 0) -> None:
    """Safely extract a ``.tar.gz`` archive into *dest*.

    ``tarfile.extractall(filter="data")`` exists on Python 3.12+, but PyBundler
    supports running on 3.11, so the checks are implemented explicitly.
    """
    ensure_dir(dest)
    with tarfile.open(archive, "r:gz") as tar:
        members = tar.getmembers()
        for member in members:
            _validate_archive_member(member, dest)
        for member in members:
            parts = Path(member.name).parts
            if len(parts) < strip_components:
                continue
            member.name = str(Path(*parts[strip_components:])) if len(parts) > strip_components else ""
            if not member.name:
                continue
            if member.isdir():
                ensure_dir(dest / member.name)
                continue
            if not (member.isfile() or member.issym()):
                continue  # devices, fifos, sockets never belong in a runtime
            target = dest / member.name
            ensure_dir(target.parent)
            if member.issym():
                if target.is_symlink() or target.exists():
                    target.unlink()
                os.symlink(member.linkname, target)
                continue
            source = tar.extractfile(member)
            if source is None:  # pragma: no cover - defensive
                continue
            tmp = target.with_name(target.name + ".pbtmp")
            with source, tmp.open("wb") as out:
                shutil.copyfileobj(source, out, CHUNK)
            os.chmod(tmp, member.mode & 0o777 or 0o644)
            os.replace(tmp, target)


def atomic_replace_dir(src: Path, dst: Path) -> None:
    """Move *src* to *dst*, replacing an existing directory if possible."""
    if dst.exists():
        remove_tree(dst)
    os.replace(src, dst)


def default_cache_dir() -> Path:
    """Return the per-user cache directory for runtimes and downloads."""
    override = os.environ.get("PYBUNDLER_CACHE_DIR")
    if override:
        return Path(override).expanduser()
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Caches" / "pybundler"
    if sys.platform == "win32":  # pragma: no cover - not exercised on CI here
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "pybundler"
    base = os.environ.get("XDG_CACHE_HOME")
    if base:
        return Path(base) / "pybundler"
    return Path.home() / ".cache" / "pybundler"


def temp_dir(prefix: str = "pybundler-") -> Path:
    """Create a temporary working directory (not the system default tmp)."""
    override = os.environ.get("PYBUNDLER_TMPDIR")
    root = Path(override) if override else None
    if root is not None:
        ensure_dir(root)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=root))


def iter_glob(root: Path, patterns: Iterable[str]) -> list[Path]:
    """Expand several glob patterns relative to *root*, de-duplicated."""
    found: dict[str, Path] = {}
    for pattern in patterns:
        if not pattern:
            continue
        matches = root.glob(pattern)
        for match in matches:
            found[str(match)] = match
    return [found[key] for key in sorted(found)]
