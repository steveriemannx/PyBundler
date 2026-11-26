from __future__ import annotations

import sys
import unittest

from pybundler.build.launcher import precompile
from pybundler.log import Logger

from support import TempDirTestCase


class PrecompileTests(TempDirTestCase):
    def test_strip_and_protect(self):
        site = self.tmp / "site"
        (site / "pkg").mkdir(parents=True)
        (site / "pkg" / "__init__.py").write_text("VALUE = 1\n", encoding="utf-8")
        entry = site / "main.py"
        entry.write_text("print('hello')\n", encoding="utf-8")
        failures = precompile(
            sys.executable,
            home=sys.base_prefix,
            site_dir=site,
            stdlib_dir=None,
            strip_sources=True,
            protect={entry},
        )
        self.assertEqual(failures, [])
        self.assertTrue(entry.is_file(), "protected entry script must survive stripping")
        self.assertFalse((site / "pkg" / "__init__.py").exists())
        self.assertTrue((site / "pkg" / "__init__.pyc").is_file())
        self.assertTrue((site / "__pycache__" / "main.cpython-314.pyc").exists() or list(site.glob("__pycache__/main*.pyc")))

    def test_sources_kept_when_requested(self):
        site = self.tmp / "site"
        site.mkdir()
        source = site / "mod.py"
        source.write_text("x = 1\n", encoding="utf-8")
        failures = precompile(
            sys.executable,
            home=sys.base_prefix,
            site_dir=site,
            stdlib_dir=None,
            strip_sources=False,
        )
        self.assertEqual(failures, [])
        self.assertTrue(source.is_file())


if __name__ == "__main__":
    unittest.main()
