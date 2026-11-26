#!/usr/bin/env python3
"""PyBundler bootstrap executed by the bundled interpreter.

The launcher sets ``PYTHONHOME``/``PYTHONPATH`` and then runs this file with the
bundled CPython.  Everything else (entry point, ``sys.frozen`` compatibility,
``sys._MEIPASS``) is handled here so that the launcher can stay tiny and the
logic stays testable in plain Python.
"""

from __future__ import annotations

import json
import os
import runpy
import sys
import traceback


def _root() -> str:
    override = os.environ.get("PYBUNDLER_ROOT")
    if override:
        return override
    # <root>/site/_pybundler_boot.py
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_config(root: str) -> dict:
    path = os.path.join(root, "pybundler.json")
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main() -> None:
    root = _root()
    config = _load_config(root)
    app_path = os.environ.get("PYBUNDLER_APP_PATH")
    if app_path:
        # Match PyInstaller's convention: inside a bundle, sys.executable is the
        # application itself.  ``multiprocessing`` relies on this to re-launch
        # the bundle (which then calls ``freeze_support()``) instead of trying
        # to pass bootloader flags to a plain interpreter.
        sys.argv[0] = app_path
        sys.executable = app_path
        sys._base_executable = app_path

    # Compatibility flags used by the ecosystem (and by PyInstaller-aware
    # packages) to detect a frozen application.
    sys.frozen = True
    sys._MEIPASS = root
    sys.pybundler = {
        "root": root,
        "mode": config.get("mode", "onedir"),
        "version": config.get("version", "0"),
        "build_id": config.get("build_id", ""),
    }

    # Environment variables from [launch.env].  Existing variables win so that
    # operators can still override baked defaults at runtime.
    for key, value in (config.get("env") or {}).items():
        os.environ.setdefault(str(key), str(value))

    if sys.platform == "win32":
        # Wheel DLL directories are siblings of the package directory; wheels
        # usually register them themselves, but doing it centrally also covers
        # packages that rely on the loader search path.  add_dll_directory is
        # available since Python 3.8.
        site_dir = os.path.join(root, "site")
        candidates = [os.path.join(root, "lib"), site_dir]
        if os.path.isdir(site_dir):
            for entry in os.listdir(site_dir):
                if entry.endswith((".libs", ".dylibs")):
                    candidate = os.path.join(site_dir, entry)
                    if os.path.isdir(candidate):
                        candidates.append(candidate)
        for candidate in candidates:
            if os.path.isdir(candidate):
                try:
                    os.add_dll_directory(candidate)
                except (AttributeError, OSError):
                    pass

    entry = config.get("entry") or {}
    kind = entry.get("type")
    if kind == "module":
        runpy.run_module(entry["module"], run_name="__main__", alter_sys=False)
        return
    if kind == "callable":
        import importlib

        module = importlib.import_module(entry["module"])
        target = module
        for part in str(entry.get("function", "")).split("."):
            if not part:
                continue
            target = getattr(target, part)
        raise SystemExit(target())
    path = entry.get("path")
    if not path:
        raise RuntimeError("pybundler.json does not describe an entry point")
    absolute = os.path.join(root, path)
    if not os.path.exists(absolute):
        raise RuntimeError(f"entry point is missing from the bundle: {absolute}")
    _run_script(absolute)


def _run_script(path: str) -> None:
    """Execute *path* as ``__main__`` without clobbering ``sys.argv[0]``.

    ``runpy.run_path`` would replace ``sys.argv[0]`` with the script's path,
    which makes ``argparse`` report the internal file name instead of the
    application name.  A tiny module object gives the same semantics while
    preserving ``sys.argv``.
    """
    import builtins
    import types

    module = types.ModuleType("__main__")
    module.__dict__["__builtins__"] = builtins
    module.__file__ = path
    module.__loader__ = None
    module.__package__ = None
    module.__spec__ = None
    sys.modules["__main__"] = module
    with open(path, "rb") as handle:
        source = handle.read()
    code = compile(source, path, "exec")
    exec(code, module.__dict__)


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except BaseException:
        # Match CPython's behaviour: print the traceback, but make the bundle
        # boundary obvious when `PYBUNDLER_DEBUG` is requested.
        traceback.print_exc()
        if os.environ.get("PYBUNDLER_DEBUG"):
            sys.stderr.write(
                f"\n[pybundler] failed to start application (root: {_root()})\n"
            )
        raise SystemExit(1)
