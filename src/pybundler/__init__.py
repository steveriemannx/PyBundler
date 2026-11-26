"""PyBundler -- bundle Python applications into relocatable executables.

PyBundler is an independent, BSD-2-Clause licensed alternative to
PyInstaller.  It has no runtime dependencies outside CPython's standard
library.
"""

from __future__ import annotations

__version__ = "0.2.1"
__license__ = "BSD-2-Clause"

#: Minimum Python version required to *run* PyBundler itself.
MIN_BUILD_PYTHON: tuple[int, int] = (3, 11)

__all__ = ["__version__", "__license__", "MIN_BUILD_PYTHON"]
