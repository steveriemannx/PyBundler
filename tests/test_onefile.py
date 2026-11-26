from __future__ import annotations

import io
import os
import struct
import unittest

from pybundler.build import onefile
from pybundler.errors import BuildError

from support import TempDirTestCase


class ContainerTests(TempDirTestCase):
    def make_tree(self) -> object:
        root = self.tmp / "payload"
        (root / "site" / "pkg").mkdir(parents=True)
        (root / "site" / "pkg" / "__init__.py").write_text("x = 1\n")
        (root / "site" / "data.bin").write_bytes(b"\x00\x01\x02" * 1000)
        (root / "pybundler.json").write_text('{"name": "demo"}\n')
        executable = root / "bin" / "tool"
        executable.parent.mkdir(parents=True)
        executable.write_text("#!/bin/sh\n")
        executable.chmod(0o755)
        return root

    def test_roundtrip_compressed(self):
        root = self.make_tree()
        container = self.tmp / "app.pbf"
        info = onefile.write_container(root, container, compress=True)
        self.assertGreater(info.count, 0)
        self.assertEqual(len(info.sha256), 64)
        extracted = self.tmp / "out"
        count, total = onefile.extract_container(container, extracted)
        self.assertEqual(count, info.count)
        self.assertEqual((extracted / "site" / "pkg" / "__init__.py").read_text(), "x = 1\n")
        self.assertEqual((extracted / "site" / "data.bin").read_bytes(), b"\x00\x01\x02" * 1000)
        self.assertTrue(os.access(extracted / "bin" / "tool", os.X_OK))

    def test_roundtrip_stored(self):
        root = self.make_tree()
        container = self.tmp / "stored.pbf"
        info = onefile.write_container(root, container, compress=False)
        self.assertLess(info.size, (root / "site" / "data.bin").stat().st_size * 2)
        entries = onefile.list_container(container)
        names = {name for name, _size, _mode in entries}
        self.assertIn("pybundler.json", names)

    def test_compression_reduces_size(self):
        root = self.make_tree()
        stored = onefile.write_container(root, self.tmp / "a.pbf", compress=False)
        compressed = onefile.write_container(root, self.tmp / "b.pbf", compress=True)
        self.assertLess(compressed.size, stored.size)

    def test_append_and_footer(self):
        root = self.make_tree()
        container = self.tmp / "app.pbf"
        onefile.write_container(root, container, compress=False)
        launcher = self.tmp / "launcher"
        launcher.write_bytes(b"\x7fELF" + b"\x00" * 100)
        output = self.tmp / "demo"
        offset = onefile.append_container(launcher, container, output)
        self.assertEqual(offset, launcher.stat().st_size)
        size, read_offset, length = onefile.read_footer(output)
        self.assertEqual(read_offset, offset)
        self.assertEqual(size, output.stat().st_size)
        self.assertEqual(length, container.stat().st_size)
        extracted = self.tmp / "extracted"
        count, _total = onefile.extract_container(output, extracted)
        self.assertEqual(count, 4)
        self.assertTrue((extracted / "pybundler.json").is_file())

    def test_unsafe_paths_are_rejected(self):
        name = b"../escape"
        payload = (
            onefile.HEADER.pack(onefile.CONTAINER_MAGIC, 1, 1)
            + onefile.ENTRY_HEADER.pack(len(name), 0, 0, 0o644, 1, 1)
            + name
            + b"x"
        )
        path = self.tmp / "evil"
        path.write_bytes(b"launcher" + payload + onefile.FOOTER.pack(onefile.FOOTER_MAGIC, 8, len(payload)))
        with self.assertRaises(BuildError):
            onefile.extract_container(path, self.tmp / "out")
        self.assertFalse((self.tmp / "escape").exists())

    def test_symlinks_are_preserved(self):
        if os.name == "nt":
            self.skipTest("symlinks need privileges on Windows")
        root = self.make_tree()
        os.symlink("data.bin", root / "site" / "link.bin")
        os.symlink("../site/data.bin", root / "site" / "link-up.txt")
        container = self.tmp / "links.pbf"
        info = onefile.write_container(root, container, compress=True)
        extracted = self.tmp / "links"
        count, _total = onefile.extract_container(container, extracted)
        self.assertEqual(count, info.count)
        link = extracted / "site" / "link.bin"
        self.assertTrue(link.is_symlink())
        self.assertEqual(link.read_bytes(), b"\x00\x01\x02" * 1000)
        # Targets containing ".." are dereferenced rather than recreated.
        outside = extracted / "site" / "link-up.txt"
        self.assertFalse(outside.is_symlink())
        self.assertEqual(outside.read_bytes(), b"\x00\x01\x02" * 1000)

    def test_missing_footer(self):
        path = self.tmp / "plain"
        path.write_bytes(b"not a bundle" * 10)
        with self.assertRaises(BuildError):
            onefile.read_footer(path)

    def test_empty_payload_rejected(self):
        empty = self.tmp / "empty"
        empty.mkdir()
        with self.assertRaises(BuildError):
            onefile.write_container(empty, self.tmp / "x.pbf")


if __name__ == "__main__":
    unittest.main()
