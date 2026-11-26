from __future__ import annotations

import unittest
from pathlib import Path

from pybundler.analysis.finder import ModuleFinder, SearchEntry, matches_exclusion

from support import TempDirTestCase


class ModuleFinderTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.site = self.tmp / "site"
        self.stdlib = self.tmp / "stdlib"
        self.write("site/simple.py", "x = 1\n")
        self.write("site/pkg/__init__.py", "")
        self.write("site/pkg/mod.py", "")
        self.write("site/pkg/data.txt", "data")
        self.write("site/extmod.cpython-314-darwin.so", "")
        self.write("site/namespace/part.py", "")
        self.write("site/pkg/sub/__init__.py", "")
        self.write("stdlib/json.py", "")
        self.finder = ModuleFinder(
            [
                SearchEntry(self.site, "site"),
                SearchEntry(self.stdlib, "stdlib"),
            ],
            ext_suffixes=[".cpython-314-darwin.so", ".so"],
            builtin_modules={"sys"},
            stdlib_dirs=[self.stdlib],
            site_dirs=[self.site],
        )

    def test_regular_module(self):
        found = self.finder.find("simple")
        self.assertIsNotNone(found)
        assert found is not None
        self.assertEqual(found.kind, "module")
        self.assertEqual(found.category, "thirdparty")
        self.assertEqual(found.rel, Path("simple.py"))

    def test_package_and_submodule(self):
        package = self.finder.find("pkg")
        assert package is not None
        self.assertTrue(package.is_package)
        self.assertEqual(package.rel, Path("pkg") / "__init__.py")
        submodule = self.finder.find("pkg.sub")
        assert submodule is not None
        self.assertTrue(submodule.is_package)
        plain = self.finder.find("pkg.mod")
        assert plain is not None
        self.assertEqual(plain.kind, "module")

    def test_extension_module(self):
        found = self.finder.find("extmod")
        assert found is not None
        self.assertEqual(found.kind, "extension")

    def test_namespace_package(self):
        found = self.finder.find("namespace")
        assert found is not None
        self.assertEqual(found.kind, "namespace")
        sub = self.finder.find("namespace.part")
        assert sub is not None
        self.assertEqual(sub.kind, "module")

    def test_builtin_and_missing(self):
        builtin = self.finder.find("sys")
        assert builtin is not None
        self.assertEqual(builtin.kind, "builtin")
        self.assertIsNone(self.finder.find("does_not_exist"))
        self.assertIsNone(self.finder.find("..relative"))

    def test_stdlib_classification(self):
        found = self.finder.find("json")
        assert found is not None
        self.assertEqual(found.category, "stdlib")

    def test_package_dir_and_submodules(self):
        self.assertIsNotNone(self.finder.package_dir("pkg"))
        submodules = set(self.finder.iter_submodules("pkg"))
        self.assertIn("pkg.mod", submodules)
        self.assertIn("pkg.sub", submodules)

    def test_chained_extensions_suffixes(self):
        # ".so" is a valid suffix even when the longer one exists.
        self.write("site/other.so", "")
        found = self.finder.find("other")
        assert found is not None
        self.assertEqual(found.kind, "extension")


class ExclusionTests(unittest.TestCase):
    def test_exact_and_prefix(self):
        self.assertTrue(matches_exclusion("tkinter", ["tkinter"]))
        self.assertTrue(matches_exclusion("tkinter.filedialog", ["tkinter"]))
        self.assertFalse(matches_exclusion("tkinterx", ["tkinter"]))

    def test_globs(self):
        self.assertTrue(matches_exclusion("pkg.tests.test_a", ["pkg.tests.*"]))
        self.assertTrue(matches_exclusion("pkg.mod", ["pkg.m?d"]))
        self.assertFalse(matches_exclusion("pkg.mod", ["pkg.other"]))


if __name__ == "__main__":
    unittest.main()
