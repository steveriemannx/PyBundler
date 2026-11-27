"""Payload planning and materialisation.

A build is described as an ordered set of ``PayloadItem`` objects.  The same
description is used for:

* printing an inspection report (``pybundler inspect``),
* materialising an ``onedir`` bundle,
* packing a ``onefile`` container.

Keeping this as data (instead of copying directly from the analyzer) makes
builds deterministic and easy to test.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from ..errors import BuildError
from ..fsutil import ensure_dir, human_size, iter_files

TreeFilter = Callable[[Path], bool]


@dataclass
class PayloadItem:
    dest: str
    source: Path | None = None
    symlink: str | None = None
    text: str | None = None
    mode: int | None = None
    compress: bool = True
    origin: str = ""

    @property
    def is_symlink(self) -> bool:
        return self.symlink is not None

    @property
    def size(self) -> int:
        if self.text is not None:
            return len(self.text.encode("utf-8"))
        if self.source is not None:
            try:
                return self.source.stat().st_size
            except OSError:  # pragma: no cover - disappearing build inputs
                return 0
        return 0


@dataclass
class Payload:
    items: dict[str, PayloadItem] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)

    def add(self, item: PayloadItem) -> None:
        if item.dest.startswith(("/", "\\")):
            raise BuildError(f"refusing to add payload item with an absolute destination {item.dest!r}")
        dest = item.dest.replace("\\", "/")
        if not dest or dest.startswith("../") or "/../" in dest or dest == "..":
            raise BuildError(f"refusing to add payload item with unsafe destination {dest!r}")
        previous = self.items.get(dest)
        if previous is not None:
            if previous.source != item.source or previous.text != item.text:
                self.warnings.append(
                    f"duplicate bundle path {dest!r}: keeping {previous.source or '<text>'}, "
                    f"ignoring {item.source or '<text>'}"
                )
            return
        item.dest = dest
        self.items[dest] = item

    def add_file(
        self,
        source: Path,
        dest: str,
        *,
        mode: int | None = None,
        compress: bool = True,
        origin: str = "",
    ) -> None:
        self.add(
            PayloadItem(
                dest=dest,
                source=source,
                mode=mode,
                compress=compress,
                origin=origin,
            )
        )

    def add_text(self, text: str, dest: str, *, mode: int = 0o644, origin: str = "") -> None:
        self.add(PayloadItem(dest=dest, text=text, mode=mode, compress=True, origin=origin))

    def add_symlink(self, link_target: str, dest: str) -> None:
        self.add(PayloadItem(dest=dest, symlink=link_target, mode=0o777))

    def add_tree(
        self,
        source: Path,
        dest: str,
        *,
        filter: TreeFilter | None = None,
        origin: str = "",
        compress: bool = True,
    ) -> int:
        """Add every file below *source*; returns the number of files added."""
        if not source.is_dir():
            self.add_file(source, dest, origin=origin, compress=compress)
            return 1
        added = 0
        root = source
        for path in iter_files(root):
            relative = path.relative_to(root)
            if filter is not None and not filter(relative):
                continue
            self.add_file(
                path,
                f"{dest}/{relative.as_posix()}".strip("/"),
                origin=origin,
                compress=compress,
            )
            added += 1
        return added

    # -- reporting ------------------------------------------------------
    def total_size(self) -> int:
        return sum(item.size for item in self.items.values())

    def by_origin(self) -> dict[str, int]:
        totals: dict[str, int] = {}
        for item in self.items.values():
            totals[item.origin or "other"] = totals.get(item.origin or "other", 0) + item.size
        return dict(sorted(totals.items(), key=lambda pair: -pair[1]))

    def summary(self) -> dict[str, object]:
        return {
            "files": len(self.items),
            "size": self.total_size(),
            "size_human": human_size(self.total_size()),
            "by_origin": {key: human_size(value) for key, value in self.by_origin().items()},
        }

    # -- materialisation ------------------------------------------------
    def materialize(self, root: Path) -> int:
        """Write the payload below *root*; returns the number of files written."""
        written = 0
        for dest, item in self.items.items():
            target = root / dest
            if item.is_symlink:
                ensure_dir(target.parent)
                if target.is_symlink() or target.exists():
                    target.unlink()
                try:
                    os.symlink(item.symlink, target)
                except (OSError, NotImplementedError):  # pragma: no cover - Windows without privileges
                    self.warnings.append(f"cannot create symlink {dest}; copying target instead")
                written += 1
                continue
            if item.text is not None:
                ensure_dir(target.parent)
                target.write_text(item.text, encoding="utf-8")
                os.chmod(target, item.mode or 0o644)
                written += 1
                continue
            assert item.source is not None
            if not item.source.exists():
                raise BuildError(f"missing build input while materialising: {item.source} (for {dest})")
            ensure_dir(target.parent)
            with item.source.open("rb") as handle:
                data = handle.read()
            target.write_bytes(data)
            mode = item.mode
            if mode is None:
                try:
                    mode = stat.S_IMODE(item.source.stat().st_mode)
                except OSError:
                    mode = 0o644
            os.chmod(target, mode)
            written += 1
        return written


def relative_dest(root: str, path: Path, base: Path) -> str:
    return f"{root}/{path.relative_to(base).as_posix()}".strip("/")
