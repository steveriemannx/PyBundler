from __future__ import annotations

import unittest

from pybundler.config import ConfigError, apply_overrides, load_spec

from support import TempDirTestCase


class SpecLoadingTests(TempDirTestCase):
    def make_spec(self, content: str, name: str = "pybundler.toml"):
        path = self.write(name, content)
        return path

    def test_minimal_spec(self):
        self.write("main.py", "print('hi')\n")
        path = self.make_spec(
            """
[app]
name = "demo"
entry = "main.py"

[collect]
hidden_imports = ["a", "b"]
data = ["assets:assets"]
"""
        )
        spec = load_spec(path)
        self.assertEqual(spec.app.name, "demo")
        self.assertEqual(spec.app.mode, "onedir")
        self.assertTrue(spec.app.console)
        self.assertEqual(spec.app.entry.kind, "script")
        self.assertEqual(spec.app.entry.source, self.tmp / "main.py")
        self.assertEqual(spec.collect.hidden_imports, ["a", "b"])
        self.assertEqual(spec.build.output_dir, self.tmp / "dist")
        self.assertEqual(spec.collect.data, ["assets:assets"])

    def test_entry_detection(self):
        self.write("__main__.py", "")
        path = self.make_spec("[app]\nname = 'auto'\n")
        spec = load_spec(path)
        self.assertEqual(spec.app.entry.source, self.tmp / "__main__.py")

    def test_package_main_entry_becomes_module(self):
        self.write("pkg/__init__.py", "")
        self.write("pkg/__main__.py", "")
        path = self.make_spec("[app]\nname = 'pkgapp'\nentry = 'pkg/__main__.py'\n")
        spec = load_spec(path)
        self.assertEqual(spec.app.entry.kind, "script")
        overridden = apply_overrides(spec)
        self.assertEqual(overridden.app.entry.kind, "script")

    def test_callable_entry(self):
        path = self.make_spec("[app]\nname = 'tool'\nentry = 'mytool.cli:main'\n")
        spec = load_spec(path)
        self.assertEqual(spec.app.entry.kind, "callable")
        self.assertEqual(spec.app.entry.module, "mytool.cli")
        self.assertEqual(spec.app.entry.function, "main")

    def test_pyproject_spec_uses_project_metadata(self):
        self.write("src/app.py", "")
        path = self.make_spec(
            """
[project]
name = "from-project"
version = "2.3.4"

[tool.pybundler.app]
entry = "src/app.py"
mode = "onefile"
""",
            name="pyproject.toml",
        )
        spec = load_spec(path)
        self.assertEqual(spec.app.name, "from-project")
        self.assertEqual(spec.app.version, "2.3.4")
        self.assertEqual(spec.app.mode, "onefile")

    def test_pyproject_flat_table(self):
        self.write("src/app.py", "")
        path = self.make_spec(
            """
[project]
name = "flat-demo"
version = "0.2.0"

[tool.pybundler]
entry = "src/app.py"
mode = "onefile"
hidden_imports = ["extra"]
""",
            name="pyproject.toml",
        )
        spec = load_spec(path)
        self.assertEqual(spec.app.name, "flat-demo")
        self.assertEqual(spec.app.mode, "onefile")
        self.assertEqual(spec.collect.hidden_imports, ["extra"])

    def test_console_script_fallback(self):
        path = self.make_spec(
            """
[project]
name = "scripts-demo"

[project.scripts]
demo = "demo.cli:run"

[tool.pybundler.app]
mode = "onedir"
""",
            name="pyproject.toml",
        )
        spec = load_spec(path)
        self.assertEqual(spec.app.entry.kind, "callable")
        self.assertEqual(spec.app.entry.module, "demo.cli")
        self.assertEqual(spec.app.entry.function, "run")

    def test_invalid_specs(self):
        self.write("main.py", "")
        cases = {
            "bad name": "[app]\nname = '../evil'\nentry = 'main.py'\n",
            "bad mode": "[app]\nname = 'x'\nentry = 'main.py'\nmode = 'zip'\n",
            "missing entry": "[app]\nname = 'x'\nentry = 'nope.py'\n",
            "bad runtime": "[app]\nname = 'x'\nentry = 'main.py'\n[python]\nruntime = 'ftp'\n",
            "missing hooks": "[app]\nname = 'x'\nentry = 'main.py'\n[collect]\nhooks = ['nope.py']\n",
        }
        for label, content in cases.items():
            with self.subTest(label=label):
                path = self.make_spec(content.replace("main.py", "main.py"), name=f"{label.replace(' ', '_')}.toml")
                with self.assertRaises(ConfigError):
                    load_spec(path)

    def test_missing_file(self):
        with self.assertRaises(ConfigError):
            load_spec(self.tmp / "nope.toml")

    def test_overrides(self):
        self.write("main.py", "")
        path = self.make_spec("[app]\nname = 'x'\nentry = 'main.py'\n")
        spec = load_spec(path)
        updated = apply_overrides(
            spec,
            mode="onefile",
            name="renamed",
            console=False,
            hidden_imports=["extra"],
            exclude=["tkinter"],
            output_dir=self.tmp / "out",
        )
        self.assertEqual(updated.app.mode, "onefile")
        self.assertEqual(updated.app.name, "renamed")
        self.assertFalse(updated.app.console)
        self.assertEqual(updated.collect.hidden_imports, ["extra"])
        self.assertEqual(updated.build.output_dir, self.tmp / "out")
        self.assertEqual(spec.app.mode, "onedir")  # original untouched

    def test_invalid_override(self):
        self.write("main.py", "")
        spec = load_spec(self.make_spec("[app]\nname = 'x'\nentry = 'main.py'\n"))
        with self.assertRaises(ConfigError):
            apply_overrides(spec, mode="zip")


if __name__ == "__main__":
    unittest.main()
