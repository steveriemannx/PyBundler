from __future__ import annotations

import unittest

from pybundler.build.plan import Payload
from pybundler.collect.runtime import plan_runtime
from pybundler.errors import BuildError
from pybundler.targets import host_target

from support import TempDirTestCase, make_fake_runtime


class PayloadTests(TempDirTestCase):
    def test_duplicate_destination_keeps_first(self):
        first = self.write("a.txt", "a")
        second = self.write("b.txt", "b")
        payload = Payload()
        payload.add_file(first, "data/file.txt")
        payload.add_file(second, "data/file.txt")
        self.assertEqual(len(payload.items), 1)
        self.assertEqual(payload.items["data/file.txt"].source, first)
        self.assertTrue(payload.warnings)

    def test_duplicate_identical_is_silent(self):
        source = self.write("a.txt", "a")
        payload = Payload()
        payload.add_file(source, "file.txt")
        payload.add_file(source, "file.txt")
        self.assertEqual(payload.warnings, [])

    def test_unsafe_destinations(self):
        source = self.write("a.txt", "a")
        for dest in ("../escape", "/absolute", "a/../../b"):
            with self.subTest(dest=dest):
                with self.assertRaises(BuildError):
                    payload = Payload()
                    payload.add_file(source, dest)

    def test_materialize(self):
        source = self.write("a.txt", "hello")
        payload = Payload()
        payload.add_file(source, "data/a.txt")
        payload.add_text("text", "site/module.py")
        payload.add_symlink("a.txt", "data/link.txt")
        target = self.tmp / "out"
        written = payload.materialize(target)
        self.assertEqual(written, 3)
        self.assertEqual((target / "data" / "a.txt").read_text(), "hello")
        self.assertEqual((target / "site" / "module.py").read_text(), "text")
        self.assertTrue((target / "data" / "link.txt").is_symlink())
        self.assertEqual(payload.summary()["files"], 3)


class RuntimeTrimTests(TempDirTestCase):
    def plan(self, features: set[str]):
        runtime, root = make_fake_runtime(self.tmp)
        payload = Payload()
        layout, warnings = plan_runtime(
            runtime, target=host_target(), features=features, payload=payload
        )
        return root, payload, layout, warnings

    def test_default_trims_development_files(self):
        root, payload, layout, _warnings = self.plan(set())
        dests = set(payload.items)
        self.assertIn("runtime/python/bin/python3.14", dests)
        self.assertIn("runtime/python/lib/python3.14/os.py", dests)
        self.assertIn("runtime/python/lib/python3.14/encodings/__init__.py", dests)
        self.assertNotIn("runtime/python/lib/python3.14/lib-dynload/_tkinter.so", dests)
        for dest in dests:
            self.assertNotIn("site-packages", dest)
            self.assertNotIn("/test/", dest)
            self.assertNotIn("idlelib", dest)
            self.assertNotIn("/include/", dest)
            self.assertNotIn("/share/", dest)
            self.assertNotIn("config-3.14", dest)
            self.assertNotIn("tcl9", dest)
            self.assertNotIn("/tkinter/", dest)
        self.assertNotIn("runtime/python/bin/pip3", dests)

    def test_tcl_tk_feature(self):
        _root, payload, _layout, _warnings = self.plan({"tcl-tk"})
        dests = set(payload.items)
        self.assertIn("runtime/python/lib/python3.14/tkinter/__init__.py", dests)
        self.assertIn("runtime/python/lib/python3.14/lib-dynload/_tkinter.so", dests)
        self.assertIn("runtime/python/lib/tcl9/init.tcl", dests)

    def test_layout_matches_runtime(self):
        root, _payload, layout, _warnings = self.plan(set())
        self.assertEqual(layout.home_rel, "runtime/python")
        self.assertEqual(layout.exe_rel, "runtime/python/bin/python3.14")
        self.assertEqual(layout.stdlib_rel, "runtime/python/lib/python3.14")
        self.assertEqual(layout.version, "3.14.10")
        self.assertTrue(layout.is_windows == (host_target().is_windows))

    def test_directory_symlinks_are_preserved(self):
        runtime, root = make_fake_runtime(self.tmp)
        (root / "lib" / "tcl9").mkdir(exist_ok=True)
        (root / "lib" / "tcl9" / "init.tcl").write_text("tcl")
        (root / "lib" / "datalink").symlink_to("tcl9")
        payload = Payload()
        plan_runtime(runtime, target=host_target(), features={"tcl-tk"}, payload=payload)
        item = payload.items.get("runtime/python/lib/datalink")
        self.assertIsNotNone(item)
        assert item is not None
        self.assertTrue(item.is_symlink)
        self.assertEqual(item.symlink, "tcl9")
        self.assertIn("runtime/python/lib/tcl9/init.tcl", payload.items)

    def test_executable_mode_is_preserved(self):
        _root, payload, _layout, _warnings = self.plan(set())
        item = payload.items["runtime/python/bin/python3.14"]
        self.assertEqual(item.mode, 0o755)


if __name__ == "__main__":
    unittest.main()
