from __future__ import annotations

import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from pybundler.interp import EnvInfo
from pybundler.runtimes import Runtime

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
EXAMPLE_HELLO = REPO_ROOT / "examples" / "hello"


def make_env_info(root: Path, *, version: str = "3.14.10") -> EnvInfo:
    major_minor = ".".join(version.split(".")[:2])
    stdlib = root / "lib" / f"python{major_minor}"
    return EnvInfo(
        python=root / "bin" / f"python{major_minor}",
        version=version,
        hexversion=(3 << 24) | (14 << 16),
        implementation="cpython",
        prefix=root,
        base_prefix=root,
        sys_path=(stdlib,),
        sysname=sys.platform,
        machine="arm64" if "arm" in (sys.platform + "arm64") else "x86_64",
        bits=64,
        ext_suffix=f".cpython-{major_minor.replace('.', '')}-darwin.so",
        soabi=f"cpython-{major_minor.replace('.', '')}",
        stdlib=stdlib,
        platstdlib=stdlib,
        dynload=stdlib / "lib-dynload",
        purelib=stdlib / "site-packages",
        platlib=stdlib / "site-packages",
        builtin_modules=frozenset({"sys", "builtins", "_imp"}),
        is_venv=False,
    )


def make_fake_runtime(base: Path, *, version: str = "3.14.10") -> tuple[Runtime, Path]:
    root = base / "python"
    major_minor = ".".join(version.split(".")[:2])
    stdlib = root / "lib" / f"python{major_minor}"
    files = {
        root / "bin" / f"python{major_minor}": "#!/bin/sh\n",
        root / "bin" / "pip3": "#!/bin/sh\n",
        root / "lib" / f"libpython{major_minor}.dylib": "dylib",
        stdlib / "os.py": "raise SystemExit\n",
        stdlib / "encodings" / "__init__.py": "",
        stdlib / "json" / "__init__.py": "",
        stdlib / "test" / "test_os.py": "",
        stdlib / "idlelib" / "__init__.py": "",
        stdlib / "tkinter" / "__init__.py": "",
        stdlib / "lib-dynload" / "_tkinter.so": "so",
        stdlib / "site-packages" / "pip" / "__init__.py": "",
        stdlib / f"config-{major_minor}-darwin" / "Makefile": "",
        root / "include" / f"python{major_minor}" / "Python.h": "",
        root / "share" / "man" / "man1" / "python.1": "",
        root / "lib" / "tcl9" / "init.tcl": "",
        root / "lib" / "pkgconfig" / "python.pc": "",
    }
    for path, content in files.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    (root / "bin" / f"python{major_minor}").chmod(0o755)
    env = make_env_info(root, version=version)
    return Runtime(root=root, python=env.python, env=env, source="test"), root


class TempDirTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="pybundler-test-")).resolve()
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def write(self, relative: str, content: str = "") -> Path:
        path = self.tmp / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path
