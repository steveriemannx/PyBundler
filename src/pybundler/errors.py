"""Exception hierarchy used across PyBundler.

Every error that is expected to reach the user (bad config, missing runtime,
failed compilation) derives from :class:`PybundlerError`, which lets the CLI
print a clean message plus an optional remediation hint instead of a
traceback.
"""

from __future__ import annotations


class PybundlerError(Exception):
    """Base class for all errors raised by PyBundler.

    Parameters
    ----------
    message:
        Human readable description of what went wrong.
    hint:
        Optional second line telling the user how to fix it.
    """

    exit_code = 1

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.hint = hint

    def __str__(self) -> str:  # pragma: no cover - trivial
        if self.hint:
            return f"{self.message}\n  hint: {self.hint}"
        return self.message


class UsageError(PybundlerError):
    """Bad command line usage."""

    exit_code = 2


class ConfigError(PybundlerError):
    """The spec file is malformed or inconsistent."""

    exit_code = 2


class AnalysisError(PybundlerError):
    """Import analysis could not be completed."""


class BuildError(PybundlerError):
    """Bundling failed."""


class RuntimeProvisionError(BuildError):
    """The target Python runtime could not be provisioned."""


class ToolchainError(BuildError):
    """A required host tool (compiler, otool, ...) is unavailable."""


class UnsupportedPlatformError(PybundlerError):
    """The requested target platform cannot be built on this host."""

    exit_code = 2
