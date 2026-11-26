from __future__ import annotations

import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent / "src" / "pybundler"


def absolute_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text("utf-8"), filename=str(path))
    found: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                found.add(alias.name.split(".", 1)[0])
        elif isinstance(node, ast.ImportFrom):
            if node.level == 0 and node.module:
                found.add(node.module.split(".", 1)[0])
    return found


class ZeroDependencyTests(unittest.TestCase):
    """PyBundler must not import anything outside the standard library."""

    def test_only_stdlib_imports(self):
        allowed = set(sys.stdlib_module_names) | {"pybundler"}
        offenders: dict[str, set[str]] = {}
        for path in SRC.rglob("*.py"):
            if "hooks" in path.parts:  # hook files import nothing at module level
                continue
            extra = absolute_imports(path) - allowed
            if extra:
                offenders[str(path.relative_to(SRC))] = extra
        self.assertEqual(offenders, {}, f"non-stdlib imports: {offenders}")

    def test_resources_are_present(self):
        resources = SRC / "resources"
        self.assertTrue((resources / "launcher.c").is_file())
        self.assertTrue((resources / "boot.py").is_file())
        self.assertTrue((SRC / "hooks" / "tkinter.py").is_file())

    def test_licence_metadata_is_consistent(self):
        import pybundler

        self.assertEqual(pybundler.__license__, "BSD-2-Clause")
        licence = (SRC.parent.parent / "LICENSE").read_text("utf-8")
        self.assertIn("BSD 2-Clause License", licence)
        pyproject = (SRC.parent.parent / "pyproject.toml").read_text("utf-8")
        self.assertIn('license = "BSD-2-Clause"', pyproject)
        self.assertIn("dependencies = []", pyproject)
        self.assertIn("steveriemannx", licence)
        self.assertIn("steveriemannx", pyproject)
        from pybundler.collect.licenses import PYBUNDLER_LICENSE

        self.assertIn("steveriemannx", PYBUNDLER_LICENSE)
        self.assertIn("PyBundler contributors", PYBUNDLER_LICENSE)


if __name__ == "__main__":
    unittest.main()
