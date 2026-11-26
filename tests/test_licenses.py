from __future__ import annotations

import json
import unittest

from pybundler.analysis.distinfo import parse_dist_info
from pybundler.analysis.graph import Analysis, EntryResolution
from pybundler.build.plan import Payload
from pybundler.collect.licenses import classify_license, plan_licenses

from support import TempDirTestCase, make_fake_runtime


def make_analysis(root, distributions):
    return Analysis(
        entry=EntryResolution(
            mode="script",
            module="main",
            function=None,
            script_source=None,
            script_dest=None,
            search_root=root,
        ),
        modules={},
        distributions=distributions,
        data=[],
        binaries=[],
        missing=[],
        warnings=[],
        features=frozenset(),
        project_root=root,
        site_dirs=(),
        stdlib_dirs=(),
    )


class LicenseClassificationTests(unittest.TestCase):
    def test_classification(self):
        cases = {
            "MIT": "permissive",
            "MIT OR Apache-2.0": "permissive",
            "BSD-3-Clause": "permissive",
            "Apache-2.0": "permissive",
            "PSF-2.0": "permissive",
            "ISC": "permissive",
            "MPL-2.0": "weak-copyleft",
            "Mozilla Public License 2.0 (MPL 2.0)": "weak-copyleft",
            "GPL-3.0-only": "copyleft",
            "GNU General Public License v3 (GPLv3)": "copyleft",
            "AGPL-3.0": "copyleft",
            "LGPL-2.1-only": "weak-copyleft",
            "Proprietary": "unknown",
            "": "unknown",
            None: "unknown",
        }
        for expression, expected in cases.items():
            with self.subTest(expression=expression):
                self.assertEqual(classify_license(expression), expected)


class DistInfoParsingTests(TempDirTestCase):
    def test_top_level_falls_back_to_distribution_name(self):
        self.write(
            "site/numpy-2.4.4.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: numpy\nVersion: 2.4.4\n"
            "License-Expression: BSD-3-Clause\n",
        )
        dist = parse_dist_info(self.tmp / "site" / "numpy-2.4.4.dist-info")
        assert dist is not None
        self.assertEqual(dist.top_level, ("numpy",))
        self.assertEqual(dist.license_summary, "BSD-3-Clause")

    def test_licence_files_are_discovered(self):
        self.write(
            "site/demo-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.0\nLicense: MIT\n",
        )
        self.write("site/demo-1.0.dist-info/LICENSE", "MIT\n")
        self.write("site/demo-1.0.dist-info/licenses/NOTICE", "notice\n")
        dist = parse_dist_info(self.tmp / "site" / "demo-1.0.dist-info")
        assert dist is not None
        names = {path.name for path in dist.license_files()}
        self.assertEqual(names, {"LICENSE", "NOTICE"})


class LicensePlanTests(TempDirTestCase):
    def test_permissive_bundle(self):
        self.write(
            "site/demo-1.2.3.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: demo\nVersion: 1.2.3\n"
            "License-Expression: MIT\nSummary: demo package\n",
        )
        self.write("site/demo-1.2.3.dist-info/LICENSE", "MIT License\n")
        self.write("site/demo-1.2.3.dist-info/top_level.txt", "demo\n")
        dist = parse_dist_info(self.tmp / "site" / "demo-1.2.3.dist-info")
        assert dist is not None
        runtime, root = make_fake_runtime(self.tmp)
        (root / "LICENSE").write_text("Python license\n")
        payload = Payload()
        report = plan_licenses(payload, make_analysis(self.tmp, {"demo": dist}), runtime)
        self.assertEqual(report.warnings, [])
        dests = set(payload.items)
        self.assertIn("licenses/pybundler/LICENSE", dests)
        self.assertIn("licenses/python/LICENSE", dests)
        self.assertIn("licenses/python/demo-1.2.3/LICENSE", dests)
        self.assertIn("licenses/THIRD-PARTY.json", dests)
        manifest = json.loads(payload.items["licenses/THIRD-PARTY.json"].text or "[]")
        names = {entry["name"] for entry in manifest}
        self.assertIn("demo", names)
        self.assertIn("CPython", names)

    def test_copyleft_is_flagged(self):
        self.write(
            "site/copyleft-1.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: copyleft\nVersion: 1.0\nLicense: GPL-3.0-only\n",
        )
        dist = parse_dist_info(self.tmp / "site" / "copyleft-1.0.dist-info")
        assert dist is not None
        runtime, root = make_fake_runtime(self.tmp)
        payload = Payload()
        report = plan_licenses(payload, make_analysis(self.tmp, {"copyleft": dist}), runtime)
        self.assertEqual(len(report.non_permissive()), 1)
        self.assertTrue(report.warnings)

    def test_missing_license_file_is_documented(self):
        self.write(
            "site/mystery-2.0.dist-info/METADATA",
            "Metadata-Version: 2.1\nName: mystery\nVersion: 2.0\nLicense: MIT\n",
        )
        dist = parse_dist_info(self.tmp / "site" / "mystery-2.0.dist-info")
        assert dist is not None
        runtime, _root = make_fake_runtime(self.tmp)
        payload = Payload()
        plan_licenses(payload, make_analysis(self.tmp, {"mystery": dist}), runtime)
        self.assertIn("licenses/python/mystery-2.0/LICENSE-NOT-FOUND.txt", payload.items)


if __name__ == "__main__":
    unittest.main()
