from __future__ import annotations

import unittest

from pybundler.analysis.bytecode import dynamic_imports, imports_from_code


def statements(source: str):
    return imports_from_code(compile(source, "<test>", "exec"))


class ImportScanningTests(unittest.TestCase):
    def test_plain_imports(self):
        found = statements("import os\nimport package.sub as alias\n")
        pairs = {(stmt.name, stmt.level) for stmt in found}
        self.assertIn(("os", 0), pairs)
        self.assertIn(("package.sub", 0), pairs)

    def test_from_import(self):
        found = statements("from collections import ordered_dict\n")
        self.assertTrue(any(stmt.name == "collections" and stmt.fromlist == ("ordered_dict",) for stmt in found))

    def test_relative_imports(self):
        found = statements("from . import sibling\nfrom ..parent import thing\n")
        by_name = {(stmt.name, stmt.level, stmt.fromlist) for stmt in found}
        self.assertIn(("", 1, ("sibling",)), by_name)
        self.assertIn(("parent", 2, ("thing",)), by_name)

    def test_nested_imports_are_found(self):
        source = """
def factory():
    try:
        import optional.accelerator
    except ImportError:
        return None
    return optional
"""
        names = {stmt.name for stmt in statements(source)}
        self.assertIn("optional.accelerator", names)

    def test_imports_in_comprehensions_and_classes(self):
        source = """
class Thing:
    import inside_class

values = [__import__("dynamic") for _ in range(1)]
"""
        names = {stmt.name for stmt in statements(source)}
        self.assertIn("inside_class", names)

    def test_dynamic_imports(self):
        source = """
import importlib
importlib.import_module("plugins.one")
__import__("plugins.two")
module = importlib.import_module(name="plugins.three")
computed = importlib.import_module(variable)
"""
        found = dynamic_imports(source)
        self.assertEqual(found, ["plugins.one", "plugins.two"])

    def test_syntax_error_returns_empty_dynamic(self):
        self.assertEqual(dynamic_imports("def broken(:\n"), [])


if __name__ == "__main__":
    unittest.main()
