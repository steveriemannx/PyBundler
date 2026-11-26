from __future__ import annotations

import unittest

from pybundler.build.bundle import resolve_extra_entries

from support import TempDirTestCase


class ExtraEntryTests(TempDirTestCase):
    def test_recursive_glob_keeps_layout(self):
        self.write("assets/root.txt", "r")
        self.write("assets/nested/deep.txt", "d")
        entries = resolve_extra_entries(
            ["assets/**/*.txt"], project_root=self.tmp, kind="data"
        )
        dests = sorted(dest for _source, dest in entries)
        self.assertEqual(dests, ["assets/nested/deep.txt", "assets/root.txt"])

    def test_explicit_directory_destination(self):
        self.write("conf/a.json", "{}")
        entries = resolve_extra_entries(
            ["conf/*.json:settings"], project_root=self.tmp, kind="data"
        )
        self.assertEqual([dest for _source, dest in entries], ["settings/a.json"])

    def test_missing_entry_is_an_error(self):
        with self.assertRaises(Exception):
            resolve_extra_entries(["nope.txt"], project_root=self.tmp, kind="data")

    def test_empty_glob_only_warns(self):
        entries = resolve_extra_entries(["nothing/*.txt"], project_root=self.tmp, kind="data")
        self.assertEqual(entries, [])


if __name__ == "__main__":
    unittest.main()
