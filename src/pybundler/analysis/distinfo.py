"""Reading installed distribution metadata (*.dist-info) without pip.

PyBundler needs three things from the build environment:

* which top-level import names belong to which distribution (for licence
  reporting and hook selection),
* the distribution version, and
* the licence files that must be shipped with the bundle.

All of it is available in the ``*.dist-info`` directories that pip writes, so
``importlib.metadata`` is not needed and builds do not depend on the packages
being importable from the PyBundler process.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from email.parser import Parser
from pathlib import Path
from typing import Iterable

_LICENSE_PREFIXES = ("license", "licence", "copying", "copyright", "notice", "authors", "unlicense")

#: Distribution names whose import name differs, used when a distro strips
#: RECORD/top_level.txt (Homebrew does this).
TOP_LEVEL_ALIASES: dict[str, tuple[str, ...]] = {
    "pillow": ("PIL",),
    "pyyaml": ("yaml",),
    "beautifulsoup4": ("bs4",),
    "opencv-python": ("cv2",),
    "opencv-python-headless": ("cv2",),
    "scikit-learn": ("sklearn",),
    "python-dateutil": ("dateutil",),
    "pyqt5": ("PyQt5",),
    "pyqt6": ("PyQt6",),
    "pyside2": ("PySide2",),
    "pyside6": ("PySide6",),
    "pycryptodome": ("Crypto",),
    "msgpack-python": ("msgpack",),
    "pymupdf": ("pymupdf", "fitz"),
    "protobuf": ("google",),
    "attrs": ("attr", "attrs"),
}


@dataclass(frozen=True)
class DistInfo:
    name: str
    version: str
    dist_info: Path
    root: Path
    top_level: tuple[str, ...] = ()
    license_expression: str | None = None
    license_classifiers: tuple[str, ...] = ()
    summary: str = ""
    requires_dist: tuple[str, ...] = ()
    metadata_fields: dict[str, tuple[str, ...]] = field(default_factory=dict)

    @property
    def canonical(self) -> str:
        return self.name.lower().replace("_", "-")

    @property
    def license_summary(self) -> str:
        if self.license_expression:
            return self.license_expression
        if self.license_classifiers:
            return "; ".join(
                classifier.rsplit("::", 1)[-1].strip() for classifier in self.license_classifiers
            )
        licenses = self.metadata_fields.get("License", ())
        if licenses:
            value = licenses[0].strip()
            if value and "\n" not in value and len(value) < 120:
                return value
        return "unknown"

    def license_files(self) -> list[Path]:
        files: list[Path] = []
        if not self.dist_info.is_dir():
            return files
        for path in sorted(self.dist_info.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(self.dist_info)
            lowered = relative.name.lower()
            if any(lowered.startswith(prefix) for prefix in _LICENSE_PREFIXES) or (
                relative.parts and relative.parts[0].lower() == "licenses"
            ):
                files.append(path)
        return files


def parse_dist_info(directory: Path) -> DistInfo | None:
    """Parse a single ``*.dist-info`` directory."""
    metadata_path = directory / "METADATA"
    if not metadata_path.is_file():
        return None
    try:
        message = Parser().parsestr(metadata_path.read_text("utf-8", errors="replace"))
    except OSError:
        return None
    name = message.get("Name") or directory.name.rsplit("-", 1)[0]
    version = message.get("Version") or "0"
    top_level = _read_top_level(directory)
    classifiers = tuple(value for value in message.get_all("Classifier", []) if "License ::" in value)
    fields: dict[str, tuple[str, ...]] = {}
    for key in ("License", "License-Expression", "Home-page", "Summary"):
        values = message.get_all(key, [])
        if values:
            fields[key] = tuple(values)
    return DistInfo(
        name=name,
        version=version,
        dist_info=directory,
        root=directory.parent,
        top_level=top_level,
        license_expression=message.get("License-Expression"),
        license_classifiers=classifiers,
        summary=(message.get("Summary") or "").strip(),
        requires_dist=tuple(message.get_all("Requires-Dist", [])),
        metadata_fields=fields,
    )


def _read_top_level(directory: Path) -> tuple[str, ...]:
    top_level = directory / "top_level.txt"
    if top_level.is_file():
        names = [
            line.strip()
            for line in top_level.read_text("utf-8", errors="replace").splitlines()
            if line.strip() and not line.startswith("#")
        ]
        if names:
            return tuple(dict.fromkeys(names))
    record = directory / "RECORD"
    if not record.is_file():
        # Homebrew and some distro packages strip RECORD/top_level.txt.  Fall
        # back to the distribution name, which matches the import name for the
        # vast majority of packages (numpy, pandas, requests, ...).
        stem = directory.name.rsplit(".dist-info", 1)[0]
        name = stem.rsplit("-", 1)[0]
        canonical = name.lower().replace("_", "-")
        aliases = TOP_LEVEL_ALIASES.get(canonical)
        if aliases:
            return aliases
        candidates = {name.lower(), name.lower().replace("-", "_"), name.lower().replace("_", "-")}
        return tuple(sorted(candidate for candidate in candidates if candidate))
    names = []
    try:
        for line in record.read_text("utf-8", errors="replace").splitlines():
            if not line:
                continue
            path = Path(line.split(",", 1)[0])
            if path.parts and path.parts[0] not in (".", "..") and not path.parts[0].endswith(".dist-info"):
                top = path.parts[0]
                if top.endswith(".py"):
                    top = top[:-3]
                names.append(top)
    except OSError:
        return ()
    return tuple(dict.fromkeys(names))


@dataclass
class DistributionIndex:
    """All distributions found on the build environment's search path."""

    by_top_level: dict[str, list[DistInfo]] = field(default_factory=dict)
    by_name: dict[str, DistInfo] = field(default_factory=dict)
    ambiguous: set[str] = field(default_factory=set)

    def resolve(self, top_level_name: str) -> DistInfo | None:
        matches = self.by_top_level.get(top_level_name)
        if not matches:
            lowered = top_level_name.lower()
            for key, value in self.by_top_level.items():
                if key.lower() == lowered:
                    matches = value
                    break
        if not matches:
            return None
        if len(matches) > 1:
            self.ambiguous.add(top_level_name)
            # Prefer the distribution whose own name matches the module name.
            canonical = top_level_name.lower().replace("_", "-")
            for dist in matches:
                if dist.canonical == canonical:
                    return dist
        return matches[0]

    def add(self, dist: DistInfo) -> None:
        self.by_name.setdefault(dist.canonical, dist)
        for top in dist.top_level:
            self.by_top_level.setdefault(top, []).append(dist)

    def __iter__(self) -> Iterable[DistInfo]:
        return iter(self.by_name.values())


def scan_distributions(site_dirs: Iterable[Path]) -> DistributionIndex:
    """Scan ``site-packages`` style directories for ``*.dist-info`` metadata."""
    index = DistributionIndex()
    seen: set[Path] = set()
    for root in site_dirs:
        if not root.is_dir():
            continue
        try:
            candidates = sorted(root.glob("*.dist-info"))
        except OSError:
            continue
        for directory in candidates:
            resolved = directory.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            dist = parse_dist_info(directory)
            if dist is not None:
                index.add(dist)
    return index
