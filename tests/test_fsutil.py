from __future__ import annotations

import os
import unittest

from pybundler.fsutil import iter_files

from support import TempDirTestCase


class FilesystemTests(TempDirTestCase):
    def test_follows_file_symlinks(self):
        real = self.tmp / "store" / "pkg"
        real.mkdir(parents=True)
        (real / "__init__.py").write_text("x = 1\n")
        tree = self.tmp / "site" / "pkg"
        tree.mkdir(parents=True)
        os.symlink(real / "__init__.py", tree / "__init__.py")
        (tree / "data.txt").write_text("d")
        names = {path.relative_to(self.tmp / "site").as_posix() for path in iter_files(self.tmp / "site")}
        self.assertEqual(names, {"pkg/__init__.py", "pkg/data.txt"})
        contents = (self.tmp / "site" / "pkg" / "__init__.py").read_text()
        self.assertEqual(contents, "x = 1\n")

    def test_directory_symlink_cycle_is_safe(self):
        root = self.tmp / "tree"
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "a.txt").write_text("a")
        os.symlink(root, root / "sub" / "loop")
        names = {path.name for path in iter_files(root)}
        self.assertIn("a.txt", names)
        self.assertLess(len(list(iter_files(root))), 10)


if __name__ == "__main__":
    unittest.main()
