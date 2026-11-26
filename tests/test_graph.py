from __future__ import annotations

import io
import unittest

from pybundler.analysis.distinfo import scan_distributions
from pybundler.analysis.finder import ModuleFinder, SearchEntry
from pybundler.analysis.graph import analyze, resolve_entry
from pybundler.analysis.hooks import HookRegistry
from pybundler.config import EntryPoint
from pybundler.log import Logger

from support import TempDirTestCase


class GraphTests(TempDirTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.tmp / "project"
        self.site = self.tmp / "site"
        self.stdlib = self.tmp / "stdlib"
        self.write("project/main.py", "import pkg\nimport thirdparty\nimport helper\nimport missing_mod\n")
        self.write("project/helper.py", "VALUE = 1\n")
        self.write("project/pkg/__init__.py", "from . import mod\n")
        self.write("project/pkg/mod.py", "import json\nimport optional_accelerator\n")
        self.write("project/pkg/data.json", "{}")
        self.write("project/pkg/tests/test_mod.py", "")
        self.write("site/thirdparty/__init__.py", "")
        self.write("site/thirdparty/impl.py", "")
        self.write(
            "site/thirdparty-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: thirdparty\nVersion: 1.0\n"
            "License-Expression: MIT\nSummary: demo\n",
        )
        self.write("site/thirdparty-1.0.dist-info/top_level.txt", "thirdparty\n")
        self.write("site/thirdparty-1.0.dist-info/LICENSE", "MIT\n")
        self.write("stdlib/json.py", "")
        self.write("site/extras.py", "")
        self.write("site/extra_dynamic.py", "")

    def make_finder(self):
        return ModuleFinder(
            [
                SearchEntry(self.project, "project"),
                SearchEntry(self.site, "site"),
                SearchEntry(self.stdlib, "stdlib"),
            ],
            ext_suffixes=[".so"],
            builtin_modules={"sys"},
            distributions=scan_distributions([self.site]),
            stdlib_dirs=[self.stdlib],
            site_dirs=[self.site],
        )

    def run_analysis(self, *, hidden=("thirdparty",), exclusions=(), hooks=None, entry=None):
        entry = entry or EntryPoint(
            kind="script", value="main.py", source=self.project / "main.py", module="main"
        )
        return analyze(
            entry=entry,
            project_root=self.project,
            finder=self.make_finder(),
            hooks=hooks or HookRegistry([]),
            logger=Logger("quiet", stream=io.StringIO()),
            hidden_imports=list(hidden),
            exclusions=exclusions,
        )

    def test_imports_and_distributions(self):
        analysis = self.run_analysis()
        self.assertIn("pkg", analysis.modules)
        self.assertIn("pkg.mod", analysis.modules)
        self.assertIn("helper", analysis.modules)
        self.assertIn("thirdparty", analysis.modules)
        self.assertIn("json", analysis.stdlib_seen)
        self.assertIn("thirdparty", analysis.distributions)
        self.assertEqual(analysis.distributions["thirdparty"].version, "1.0")
        self.assertEqual(analysis.entry.mode, "script")

    def test_missing_imports(self):
        analysis = self.run_analysis()
        missing = {(item.name, item.soft) for item in analysis.missing}
        self.assertIn(("missing_mod", False), missing)
        self.assertIn(("optional_accelerator", False), missing)
        # stdlib modules are never reported as missing
        self.assertFalse(any(name == "json" for name, _ in missing))

    def test_exclusions(self):
        analysis = self.run_analysis(exclusions=["pkg.mod"])
        self.assertIn("pkg", analysis.modules)
        self.assertNotIn("pkg.mod", analysis.modules)

    def test_hooks_add_imports_and_data(self):
        hook = self.write(
            "hook_pkg.py",
            "PACKAGES = ['pkg']\n"
            "HIDDEN_IMPORTS = ['extras']\n"
            "DATA = [('data.json', 'pkg/data.json')]\n"
            "def hook(ctx):\n"
            "    ctx.hidden_import('extra_dynamic')\n",
        )
        analysis = self.run_analysis(hooks=HookRegistry([hook]))
        self.assertIn("extras", analysis.modules)
        self.assertIn("extra_dynamic", analysis.modules)
        destinations = {data.dest for data in analysis.data}
        self.assertIn("pkg/data.json", destinations)

    def test_package_entry_becomes_module(self):
        self.write("project/app/__init__.py", "")
        self.write("project/app/__main__.py", "import pkg\n")
        entry = EntryPoint(
            kind="script", value="app/__main__.py", source=self.project / "app" / "__main__.py"
        )
        resolution = resolve_entry(entry, self.project)
        self.assertEqual(resolution.mode, "module")
        self.assertEqual(resolution.module, "app.__main__")
        analysis = analyze(
            entry=entry,
            project_root=self.project,
            finder=self.make_finder(),
            hooks=HookRegistry([]),
            logger=Logger("quiet", stream=io.StringIO()),
        )
        self.assertIn("app.__main__", analysis.modules)
        self.assertEqual(analysis.entry.mode, "module")

    def test_callable_entry(self):
        entry = EntryPoint(kind="callable", value="pkg.mod:main", module="pkg.mod", function="main")
        analysis = analyze(
            entry=entry,
            project_root=self.project,
            finder=self.make_finder(),
            hooks=HookRegistry([]),
            logger=Logger("quiet", stream=io.StringIO()),
        )
        self.assertIn("pkg.mod", analysis.modules)
        self.assertEqual(analysis.entry.function, "main")

    def test_unknown_entry_module_raises(self):
        entry = EntryPoint(kind="module", value="does.not.exist", module="does.not.exist")
        with self.assertRaises(Exception):
            analyze(
                entry=entry,
                project_root=self.project,
                finder=self.make_finder(),
                hooks=HookRegistry([]),
                logger=Logger("quiet", stream=io.StringIO()),
            )

    def test_stats(self):
        stats = self.run_analysis().stats()
        self.assertGreaterEqual(stats["modules"], 4)
        self.assertEqual(stats["distributions"], 1)


if __name__ == "__main__":
    unittest.main()
