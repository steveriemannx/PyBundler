"""macOS ``.app`` bundle assembly.

A windowed macOS application should be a real ``.app`` directory: Finder,
Gatekeeper and NSApplication all rely on the bundle structure and Info.plist.
PyBundler keeps the payload inside ``Contents/MacOS`` (the launcher resolves
its own directory), which means the same launcher works for console and
windowed builds.
"""

from __future__ import annotations

import plistlib
import re
import shutil
from pathlib import Path

from ..errors import BuildError
from ..fsutil import ensure_dir, write_bytes

_VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+){0,2}$")


def sanitize_bundle_id(name: str, bundle_id: str | None) -> str:
    if bundle_id:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9.-]+", bundle_id):
            raise BuildError(
                f"invalid macOS bundle identifier {bundle_id!r}",
                hint="use reverse-DNS characters only, for example org.example.App",
            )
        return bundle_id
    sanitized = re.sub(r"[^A-Za-z0-9-]", "-", name).strip("-") or "app"
    return f"org.pybundler.{sanitized}"


def _bundle_version(version: str) -> str:
    if _VERSION_RE.match(version):
        return version
    cleaned = re.sub(r"[^0-9.]", "", version)
    return cleaned or "0"


def write_app_metadata(
    app_dir: Path,
    *,
    name: str,
    version: str,
    executable_name: str,
    bundle_id: str | None = None,
    icon: Path | None = None,
    minimum_system: str = "11.0",
) -> None:
    contents = ensure_dir(app_dir / "Contents")
    resources = ensure_dir(contents / "Resources")
    info = {
        "CFBundleName": name,
        "CFBundleDisplayName": name,
        "CFBundleExecutable": executable_name,
        "CFBundleIdentifier": sanitize_bundle_id(name, bundle_id),
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": _bundle_version(version),
        "CFBundleVersion": _bundle_version(version),
        "LSMinimumSystemVersion": minimum_system,
        "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
    }
    if icon is not None:
        target = resources / "AppIcon.icns"
        shutil.copy2(icon, target)
        info["CFBundleIconFile"] = "AppIcon"
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle, fmt=plistlib.FMT_XML, sort_keys=True)
    write_bytes(contents / "PkgInfo", b"APPL????")


def assemble_app(
    app_dir: Path,
    *,
    payload_root: Path,
    executable_source: Path | None,
    name: str,
    version: str,
    bundle_id: str | None,
    icon: Path | None,
    onefile: bool,
) -> Path:
    """Create a ``.app`` directory with the payload in ``Contents/MacOS``.

    For onedir builds *payload_root* is moved into ``Contents/MacOS``.  For
    onefile builds only *executable_source* is copied.
    """
    macos_dir = ensure_dir(app_dir / "Contents" / "MacOS")
    executable_name = name
    if onefile:
        if executable_source is None:
            raise BuildError("onefile .app assembly requires an executable")
        shutil.copy2(executable_source, macos_dir / executable_name)
    else:
        if payload_root.is_dir():
            for child in payload_root.iterdir():
                destination = macos_dir / child.name
                if destination.exists():
                    if destination.is_dir():
                        shutil.rmtree(destination)
                    else:
                        destination.unlink()
                shutil.move(str(child), destination)
            payload_root.rmdir()
    write_app_metadata(
        app_dir,
        name=name,
        version=version,
        executable_name=executable_name,
        bundle_id=bundle_id,
        icon=icon,
    )
    return app_dir
