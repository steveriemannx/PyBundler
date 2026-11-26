"""Platform identifiers and naming conventions for build targets.

PyBundler builds for the host it runs on.  The target records the OS, the CPU
architecture and the bitness of the *interpreter being bundled* — a 32-bit
Python on 64-bit Windows is a 32-bit target, even though the OS is x86_64.

macOS is supported on x86_64 and arm64 (Apple no longer ships 32-bit runtimes).
Windows supports x86_64, i686 and arm64 via python-build-standalone.  Linux
supports x86_64, aarch64 and riscv64 through PBS, and any local 32-bit
interpreter via ``runtime = "local"``.  FreeBSD uses the system interpreter and
supports both 64-bit and 32-bit builds.
"""

from __future__ import annotations

import platform as _platform
import sys
from dataclasses import dataclass

from .errors import UnsupportedPlatformError

_64BIT_ALIASES = {
    "arm64": "aarch64",
    "aarch64": "aarch64",
    "x86_64": "x86_64",
    "amd64": "x86_64",
    "riscv64": "riscv64",
}

_32BIT_ALIASES = {"x86", "i386", "i486", "i586", "i686"}


@dataclass(frozen=True)
class Target:
    """A build target described in terms of the *host* it can run on."""

    sysname: str  # "darwin", "linux", "win32", "freebsd14"
    arch: str  # "aarch64", "x86_64", "x86", "i686", "riscv64"
    triple: str  # python-build-standalone style identifier

    @property
    def is_macos(self) -> bool:
        return self.sysname == "darwin"

    @property
    def is_windows(self) -> bool:
        return self.sysname == "win32"

    @property
    def is_linux(self) -> bool:
        return self.sysname.startswith("linux")

    @property
    def is_freebsd(self) -> bool:
        return self.sysname.startswith("freebsd")

    @property
    def is_elf(self) -> bool:
        return self.is_linux or self.is_freebsd

    @property
    def is_posix(self) -> bool:
        return not self.is_windows

    @property
    def bits(self) -> int:
        return 32 if self.arch in ("x86", "i686") else 64

    @property
    def is_32bit(self) -> bool:
        return self.bits == 32

    @property
    def exe_suffix(self) -> str:
        return ".exe" if self.is_windows else ""

    @property
    def pathsep(self) -> str:
        return ";" if self.is_windows else ":"

    @property
    def shared_lib_suffixes(self) -> tuple[str, ...]:
        if self.is_macos:
            return (".dylib", ".so")
        if self.is_windows:
            return (".dll", ".pyd")
        return (".so",)

    def __str__(self) -> str:  # pragma: no cover - cosmetic
        return f"{self.sysname}-{self.arch}"


def _arch_for(sysname: str, machine: str, bits: int | None) -> str:
    key = machine.lower()
    if bits is None:
        bits = 32 if key in _32BIT_ALIASES else 64
    if bits == 32:
        if sysname == "darwin":
            raise UnsupportedPlatformError(
                "32-bit Python is not supported on macOS",
                hint=(
                    "Apple removed 32-bit support in macOS 10.15. Build for "
                    "x86_64 or arm64 instead (both are supported)."
                ),
            )
        if key in ("arm64", "aarch64"):
            raise UnsupportedPlatformError("32-bit ARM Windows/Linux is not supported")
        return "x86" if sysname == "win32" else "i686"
    if key in ("arm64", "aarch64"):
        return "aarch64"
    if key == "riscv64":
        return "riscv64"
    if key in ("x86_64", "amd64"):
        return "x86_64"
    if key in _32BIT_ALIASES:
        # A 64-bit interpreter reporting an i686 machine name (unusual but
        # possible in emulated shells): trust the pointer size.
        return "x86_64"
    raise UnsupportedPlatformError(
        f"unsupported CPU architecture {machine!r}",
        hint="PyBundler supports x86_64/amd64, x86/i686, arm64/aarch64 and riscv64.",
    )


def normalize_arch(machine: str, bits: int | None = None) -> str:
    """Return a canonical architecture name (32-bit requires ``bits=32``)."""
    return _arch_for(sys.platform, machine, bits)


def pbs_triple(sysname: str, arch: str, release: str = "") -> str:
    """Return the python-build-standalone target triple for a platform."""
    is_32 = arch in ("x86", "i686")
    if sysname == "darwin":
        return f"{arch}-apple-darwin"
    if sysname == "win32":
        prefix = "i686" if is_32 else arch
        return f"{prefix}-pc-windows-msvc"
    if sysname.startswith("linux"):
        prefix = "i686" if is_32 else arch
        return f"{prefix}-unknown-linux-gnu"
    if sysname.startswith("freebsd"):
        major = ""
        for char in release:
            if char.isdigit():
                major += char
            elif major:
                break
        prefix = "i386" if is_32 else arch
        suffix = f"freebsd{major}" if major else "freebsd"
        return f"{prefix}-unknown-{suffix}"
    raise UnsupportedPlatformError(f"no standalone runtime available for {sysname!r}")


def make_target(sysname: str, machine: str, release: str = "", *, bits: int | None = None) -> Target:
    arch = _arch_for(sysname, machine, bits)
    return Target(sysname=sysname, arch=arch, triple=pbs_triple(sysname, arch, release))


def host_target() -> Target:
    """Describe the machine and interpreter PyBundler is currently using."""
    bits = 64 if sys.maxsize > 2**32 else 32
    return make_target(
        sys.platform,
        _platform.machine(),
        _platform.release(),
        bits=bits,
    )


def describe() -> dict[str, str]:  # pragma: no cover - diagnostics helper
    target = host_target()
    return {
        "sysname": target.sysname,
        "arch": target.arch,
        "bits": str(target.bits),
        "triple": target.triple,
        "python": sys.version.split()[0],
        "executable": sys.executable,
    }
