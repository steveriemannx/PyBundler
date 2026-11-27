"""The onefile container format.

Layout of a onefile executable::

    [ launcher Mach-O / ELF / PE ][ container ][ footer (24 bytes) ]

Container::

    magic     8  "PBFC1\\r\\n\\0"
    version   u32 little endian (1)
    count     u32
    entries, each:
        name_len  u16
        method    u8   (0 = stored, 8 = zlib)
        kind      u8   (0 = file, 1 = symlink)
        mode      u32  (permission bits)
        usize     u64
        csize     u64
        name      name_len bytes (UTF-8, "/" separators)
        data      csize bytes (file content, or the link target for kind 1)

Footer::

    magic   8  "PBFEND01"
    offset  u64  (start of the container)
    length  u64  (container length)

The format is deliberately simple: the launcher can extract it with libc plus
zlib, and a corrupted bundle fails with a clear error instead of executing
half-extracted code.
"""

from __future__ import annotations

import hashlib
import os
import stat
import struct
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterator

from ..errors import BuildError
from ..fsutil import ensure_dir, human_size, iter_files

CONTAINER_MAGIC = b"PBFC1\r\n\0"
FOOTER_MAGIC = b"PBFEND01"
FOOTER = struct.Struct("<8sQQ")
HEADER = struct.Struct("<8sII")
ENTRY_HEADER = struct.Struct("<HBBIQQ")

METHOD_STORED = 0
METHOD_ZLIB = 8


@dataclass(frozen=True)
class ContainerInfo:
    count: int
    size: int
    sha256: str

    @property
    def build_id(self) -> str:
        return self.sha256[:12]


class _HashingWriter:
    def __init__(self, handle: BinaryIO) -> None:
        self.handle = handle
        self.digest = hashlib.sha256()
        self.size = 0

    def write(self, data: bytes) -> None:
        self.handle.write(data)
        self.digest.update(data)
        self.size += len(data)

    def hexdigest(self) -> str:
        return self.digest.hexdigest()


def write_container(
    root: Path,
    destination: Path,
    *,
    compress: bool = True,
    level: int = 6,
) -> ContainerInfo:
    """Pack every regular file below *root* into a container at *destination*."""
    files = [
        (path, path.relative_to(root).as_posix())
        for path in iter_files(root)
        if not path.name.endswith(".pbtmp")
    ]
    if not files:
        raise BuildError("refusing to create an empty onefile payload")
    ensure_dir(destination.parent)
    tmp = destination.with_name(destination.name + ".part")
    count = 0
    with tmp.open("wb") as raw:
        writer = _HashingWriter(raw)
        writer.write(HEADER.pack(CONTAINER_MAGIC, 1, len(files)))
        for path, name in files:
            kind = 0
            method = METHOD_STORED
            mode = stat.S_IMODE(path.stat().st_mode) or 0o644
            if path.is_symlink():
                link_target = os.readlink(path)
                # Only simple relative links are stored as links; anything with
                # ".." is dereferenced to avoid escapes and portability issues.
                if not os.path.isabs(link_target) and ".." not in link_target.split("/"):
                    data = link_target.encode("utf-8")
                    payload = data
                    kind = 1
                    mode = 0o777
                else:
                    data = path.read_bytes()
                    payload = data
                    mode = 0o644
            else:
                try:
                    data = path.read_bytes()
                except OSError as exc:
                    raise BuildError(
                        f"cannot read {path} while packing onefile payload: {exc}"
                    ) from exc
                payload = data
            if compress and kind == 0 and len(data) > 64:
                compressed = zlib.compress(data, level)
                if len(compressed) < len(data):
                    method = METHOD_ZLIB
                    payload = compressed
            encoded = name.encode("utf-8")
            if len(encoded) > 0xFFFF:
                raise BuildError(f"bundle path is too long for the container format: {name}")
            writer.write(
                ENTRY_HEADER.pack(
                    len(encoded),
                    method,
                    kind,
                    mode,
                    len(data),
                    len(payload),
                )
            )
            writer.write(encoded)
            writer.write(payload)
            count += 1
    os.replace(tmp, destination)
    return ContainerInfo(count=count, size=writer.size, sha256=writer.hexdigest())


def payload_bounds(path: Path) -> tuple[int, int, int]:
    """Return ``(file_size, payload_offset, payload_length)``.

    Accepts both a onefile executable (footer present) and a bare ``.pbf``
    container produced during the build (no footer).
    """
    size = path.stat().st_size
    if size >= FOOTER.size:
        with path.open("rb") as handle:
            handle.seek(size - FOOTER.size)
            footer = handle.read(FOOTER.size)
        magic, offset, length = FOOTER.unpack(footer)
        if magic == FOOTER_MAGIC:
            if offset + length > size:
                raise BuildError(f"{path} has a corrupt onefile footer")
            return size, offset, length
    with path.open("rb") as handle:
        header = handle.read(HEADER.size)
    if len(header) < HEADER.size or header[:8] != CONTAINER_MAGIC:
        raise BuildError(f"{path} does not contain a PyBundler payload")
    return size, 0, size


def read_footer(path: Path) -> tuple[int, int, int]:
    """Return ``(file_size, payload_offset, payload_length)`` of a onefile binary."""
    size, offset, length = payload_bounds(path)
    if offset == 0:
        raise BuildError(f"{path} does not contain a PyBundler onefile payload")
    return size, offset, length


@dataclass(frozen=True)
class Entry:
    name: str
    method: int
    mode: int
    size: int
    data: bytes
    kind: int = 0

    @property
    def is_symlink(self) -> bool:
        return self.kind == 1


def _safe_name(name: str) -> bool:
    if not name or name.startswith(("/", "\\")):
        return False
    if len(name) > 1 and name[1] == ":":
        return False
    if "\\" in name:
        return False
    parts = name.split("/")
    return all(part not in ("", ".", "..") for part in parts)


def iter_entries(handle: BinaryIO, *, offset: int = 0, length: int | None = None) -> Iterator[Entry]:
    """Parse a container from *handle* starting at *offset*."""
    handle.seek(offset)
    header = handle.read(HEADER.size)
    if len(header) != HEADER.size:
        raise BuildError("onefile payload is truncated (header)")
    magic, version, count = HEADER.unpack(header)
    if magic != CONTAINER_MAGIC:
        raise BuildError("onefile payload has a bad magic header")
    if version != 1:
        raise BuildError(f"unsupported onefile payload version {version}")
    for _ in range(count):
        raw = handle.read(ENTRY_HEADER.size)
        if len(raw) != ENTRY_HEADER.size:
            raise BuildError("onefile payload is truncated (entry)")
        name_length, method, kind, mode, size, compressed_size = ENTRY_HEADER.unpack(raw)
        name_bytes = handle.read(name_length)
        name = name_bytes.decode("utf-8", errors="replace")
        if not _safe_name(name):
            raise BuildError(f"refusing to extract unsafe path from bundle: {name!r}")
        data = handle.read(compressed_size)
        if len(data) != compressed_size:
            raise BuildError(f"onefile payload is truncated ({name})")
        if kind not in (0, 1):
            raise BuildError(f"unknown entry kind {kind} for {name} in bundle")
        if kind == 1:
            target = data.decode("utf-8", errors="replace")
            if target.startswith(("/", "\\")) or ".." in target.split("/"):
                raise BuildError(f"refusing to link outside the bundle: {name} -> {target!r}")
            yield Entry(name=name, method=method, mode=mode, size=size, data=data, kind=kind)
            continue
        if method == METHOD_ZLIB:
            try:
                data = zlib.decompress(data)
            except zlib.error as exc:
                raise BuildError(f"corrupt compressed entry in bundle: {name}: {exc}") from exc
        elif method != METHOD_STORED:
            raise BuildError(f"unknown compression method {method} for {name}")
        if len(data) != size:
            raise BuildError(f"size mismatch while reading {name} from bundle")
        yield Entry(name=name, method=method, mode=mode, size=size, data=data, kind=kind)


def list_container(path: Path) -> list[tuple[str, int, int]]:
    """Return ``(name, size, mode)`` for every entry in a payload."""
    _size, offset, length = payload_bounds(path)
    entries: list[tuple[str, int, int]] = []
    with path.open("rb") as handle:
        for entry in iter_entries(handle, offset=offset, length=length):
            entries.append((entry.name, entry.size, entry.mode))
    return entries


def extract_container(path: Path, destination: Path) -> tuple[int, int]:
    """Extract a onefile binary with the Python implementation (used by tests).

    Returns ``(file_count, total_bytes)``.
    """
    _size, offset, length = payload_bounds(path)
    ensure_dir(destination)
    count = 0
    total = 0
    links: list[Entry] = []
    with path.open("rb") as handle:
        for entry in iter_entries(handle, offset=offset, length=length):
            if entry.is_symlink:
                links.append(entry)
                continue
            target = destination / entry.name
            ensure_dir(target.parent)
            target.write_bytes(entry.data)
            os.chmod(target, entry.mode or 0o644)
            count += 1
            total += entry.size
    for entry in links:
        target = destination / entry.name
        ensure_dir(target.parent)
        link_target = entry.data.decode("utf-8")
        try:
            if target.is_symlink() or target.exists():
                target.unlink()
            os.symlink(link_target, target)
        except OSError:  # Windows without symlink privileges
            resolved = target.parent / link_target
            if resolved.exists():
                target.write_bytes(resolved.read_bytes())
                os.chmod(target, entry.mode or 0o644)
            else:
                continue
        count += 1
    return count, total


def append_container(
    launcher: Path,
    container: Path,
    destination: Path,
    *,
    keep_launcher: bool = True,
) -> int:
    """Concatenate *launcher* and *container* plus the footer into *destination*.

    Returns the payload offset.  ``destination`` may be the launcher itself.
    """
    launcher_size = launcher.stat().st_size
    tmp = destination.with_name(destination.name + ".part")
    ensure_dir(destination.parent)
    with tmp.open("wb") as out:
        with launcher.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        with container.open("rb") as source:
            while True:
                chunk = source.read(1024 * 1024)
                if not chunk:
                    break
                out.write(chunk)
        out.write(FOOTER.pack(FOOTER_MAGIC, launcher_size, container.stat().st_size))
    os.chmod(tmp, 0o755)
    os.replace(tmp, destination)
    if not keep_launcher:
        launcher.unlink(missing_ok=True)
    return launcher_size


def read_entry(path: Path, name: str) -> bytes | None:
    """Read a single file out of a container without extracting everything."""
    _size, offset, length = payload_bounds(path)
    with path.open("rb") as handle:
        for entry in iter_entries(handle, offset=offset, length=length):
            if entry.name == name:
                return entry.data
    return None


def describe(path: Path) -> dict[str, object]:
    size, offset, length = payload_bounds(path)
    return {
        "path": str(path),
        "size": size,
        "size_human": human_size(size),
        "payload_offset": offset,
        "payload_size": length,
        "payload_size_human": human_size(length),
    }
