"""Small terminal logger with colour support and verbosity levels.

We deliberately avoid third-party logging libraries: the build output is a
first-class part of the product, so it is hand rolled and dependency free.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from typing import IO, Iterator

_LEVELS = {"quiet": 0, "error": 1, "warn": 2, "info": 3, "verbose": 4, "debug": 5}

_COLORS = {
    "step": "\033[1;36m",
    "ok": "\033[1;32m",
    "warn": "\033[1;33m",
    "error": "\033[1;31m",
    "dim": "\033[2m",
    "reset": "\033[0m",
}


class Logger:
    """Line oriented logger writing to stderr.

    Parameters
    ----------
    level:
        One of ``quiet``, ``error``, ``warn``, ``info``, ``verbose``, ``debug``.
    stream:
        Output stream; defaults to ``sys.stderr``.
    color:
        Force (``True``) or disable (``False``) ANSI colours.  ``None`` means
        auto-detect from the stream.
    """

    def __init__(
        self,
        level: str = "info",
        stream: IO[str] | None = None,
        color: bool | None = None,
    ) -> None:
        if level not in _LEVELS:
            raise ValueError(f"unknown log level {level!r}")
        self.level = level
        self.stream = stream if stream is not None else sys.stderr
        if color is None:
            color = (
                hasattr(self.stream, "isatty")
                and self.stream.isatty()
                and not os.environ.get("NO_COLOR")
                and os.environ.get("TERM") != "dumb"
            )
        self.color = bool(color)
        self._step_started = time.monotonic()

    # -- low level -----------------------------------------------------
    @property
    def verbosity(self) -> int:
        return _LEVELS[self.level]

    def _paint(self, kind: str, text: str) -> str:
        if not self.color:
            return text
        return f"{_COLORS[kind]}{text}{_COLORS['reset']}"

    def _emit(self, level: str, text: str, prefix: str = "") -> None:
        if self.verbosity < _LEVELS[level]:
            return
        print(f"{prefix}{text}", file=self.stream, flush=True)

    # -- public API ----------------------------------------------------
    def step(self, title: str) -> None:
        """Start a new build step and print its title."""
        self._step_started = time.monotonic()
        if self.verbosity >= _LEVELS["info"]:
            print(f"{self._paint('step', '==>')} {title}", file=self.stream, flush=True)

    def result(self, message: str) -> None:
        """Report the outcome of the current step with its duration."""
        if self.verbosity >= _LEVELS["info"]:
            elapsed = time.monotonic() - self._step_started
            print(
                f"{self._paint('ok', '  ok')} {message} "
                f"{self._paint('dim', f'({elapsed:.2f}s)')}",
                file=self.stream,
                flush=True,
            )

    def ok(self, message: str) -> None:
        if self.verbosity >= _LEVELS["info"]:
            print(f"{self._paint('ok', '  ok')} {message}", file=self.stream, flush=True)

    def info(self, message: str) -> None:
        self._emit("info", message)

    def detail(self, message: str) -> None:
        if self.verbosity >= _LEVELS["verbose"]:
            print(f"     {self._paint('dim', message)}", file=self.stream, flush=True)

    def warn(self, message: str) -> None:
        if self.verbosity >= _LEVELS["warn"]:
            print(f"{self._paint('warn', 'warning:')} {message}", file=self.stream, flush=True)

    def error(self, message: str) -> None:
        if self.verbosity >= _LEVELS["error"]:
            print(f"{self._paint('error', 'error:')} {message}", file=self.stream, flush=True)

    def debug(self, message: str) -> None:
        if self.verbosity >= _LEVELS["debug"]:
            print(f"{self._paint('dim', 'debug:')} {message}", file=self.stream, flush=True)

    @contextmanager
    def timed(self, title: str) -> Iterator[None]:
        self.step(title)
        started = time.monotonic()
        try:
            yield
        finally:
            if self.verbosity >= _LEVELS["info"]:
                elapsed = time.monotonic() - started
                print(
                    f"     {self._paint('dim', f'{elapsed:.2f}s')}",
                    file=self.stream,
                    flush=True,
                )


def make_logger(level: str, color: bool | None = None) -> Logger:
    return Logger(level=level, color=color)
