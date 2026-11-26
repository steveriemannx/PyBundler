"""End-to-end builds.  These are slow (they provision a runtime and compile a
launcher) and are skipped unless ``PYBUNDLER_E2E=1`` is set::

    PYBUNDLER_E2E=1 PYTHONPATH=src python -m unittest discover -s tests -t tests
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import unittest
from pathlib import Path

from pybundler.build.bundle import build
from pybundler.build.onefile import read_footer
from pybundler.config import apply_overrides, load_spec
from pybundler.log import Logger

from support import EXAMPLE_HELLO, TempDirTestCase


@unittest.skipUnless(os.environ.get("PYBUNDLER_E2E"), "set PYBUNDLER_E2E=1 to run")
class EndToEndTests(TempDirTestCase):
    def test_onedir_and_onefile(self):
        spec = load_spec(EXAMPLE_HELLO / "pybundler.toml")
        dist = self.tmp / "dist-onedir"
        spec = apply_overrides(
            spec,
            output_dir=dist,
            work_dir=self.tmp / "work-onedir",
        )
        logger = Logger(level=os.environ.get("PYBUNDLER_E2E_LOG", "info"))
        onedir = build(spec, logger=logger)
        self.assertTrue(onedir.executable.is_file())
        payload = self.run_app(onedir.executable, ["--name", "world"])
        self.assertEqual(payload["greeting"], "hello, PyBundler!")
        self.assertTrue(payload["frozen"])
        self.assertEqual(payload["argv"], ["--name", "world"])
        self.assertEqual(Path(payload["argv0"]).resolve(), onedir.executable.resolve())
        self.assertEqual(payload["mode"], "onedir")
        self.assertIn("PyBundler example", payload["banner"])
        self.assertTrue(payload["stdlib"])
        self.assertEqual(payload["env"], "from-spec")

        onefile_spec = apply_overrides(
            spec,
            mode="onefile",
            output_dir=self.tmp / "dist-onefile",
            work_dir=self.tmp / "work-onefile",
        )
        onefile = build(onefile_spec, logger=logger)
        self.assertTrue(onefile.executable.is_file())
        size, offset, length = read_footer(onefile.executable)
        self.assertGreater(offset, 0)
        self.assertGreater(length, 0)
        cache = self.tmp / "cache"
        first = self.run_app(onefile.executable, ["--name", "world"], cache=cache)
        self.assertEqual(first["mode"], "onefile")
        self.assertTrue(first["frozen"])
        extraction_dirs = list((cache / "pybundler" / "hello").iterdir())
        self.assertEqual(len(extraction_dirs), 1)
        self.assertTrue((extraction_dirs[0] / "pybundler.json").is_file())
        # A second run must reuse the extracted payload.
        second = self.run_app(onefile.executable, ["--name", "again"], cache=cache)
        self.assertEqual(second["argv"], ["--name", "again"])
        self.assertEqual(len(list((cache / "pybundler" / "hello").iterdir())), 1)

    def test_multiprocessing_and_sqlite(self):
        project = self.tmp / "mpproj"
        (project / "pkg").mkdir(parents=True)
        (project / "pkg" / "__init__.py").write_text("")
        (project / "pkg" / "worker.py").write_text(
            "def square(value):\n    return value * value\n"
        )
        (project / "main.py").write_text(
            "import json, multiprocessing, sqlite3, sys\n"
            "from pkg.worker import square\n"
            "\n"
            "if __name__ == '__main__':\n"
            "    multiprocessing.freeze_support()\n"
            "    with multiprocessing.Pool(2) as pool:\n"
            "        squares = pool.map(square, [1, 2, 3, 4])\n"
            "    con = sqlite3.connect(':memory:')\n"
            "    con.execute('create table t (x integer)')\n"
            "    con.executemany('insert into t values (?)', [(1,), (2,)])\n"
            "    total = con.execute('select sum(x) from t').fetchone()[0]\n"
            "    print(json.dumps({'squares': squares, 'total': total,"
            " 'exe': sys.executable, 'start': multiprocessing.get_start_method()}))\n"
        )
        (project / "pybundler.toml").write_text(
            "[app]\nname = 'mpapp'\nversion = '0.1.0'\nentry = 'main.py'\n"
            "mode = 'onedir'\n\n[build]\noutput_dir = 'dist'\nwork_dir = 'work'\n"
        )
        spec = load_spec(project / "pybundler.toml")
        logger = Logger(level=os.environ.get("PYBUNDLER_E2E_LOG", "warn"))
        result = build(spec, logger=logger)
        proc = subprocess.run(
            [str(result.executable)],
            capture_output=True,
            text=True,
            timeout=120,
        )
        self.assertEqual(proc.returncode, 0, msg=proc.stderr[-3000:])
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual(len(lines), 1, msg=f"expected a single run, got:\n{proc.stdout}")
        payload = json.loads(lines[0])
        self.assertEqual(payload["squares"], [1, 4, 9, 16])
        self.assertEqual(payload["total"], 3)
        self.assertEqual(Path(payload["exe"]).resolve(), result.executable.resolve())

    @unittest.skipUnless(sys.platform == "darwin", "macOS .app test")
    def test_windowed_app_bundle(self):
        spec = load_spec(EXAMPLE_HELLO / "pybundler.toml")
        spec = apply_overrides(
            spec,
            console=False,
            output_dir=self.tmp / "dist-app",
            work_dir=self.tmp / "work-app",
        )
        logger = Logger(level=os.environ.get("PYBUNDLER_E2E_LOG", "warn"))
        result = build(spec, logger=logger)
        self.assertEqual(result.path.suffix, ".app")
        plist = result.path / "Contents" / "Info.plist"
        self.assertTrue(plist.is_file())
        import plistlib

        data = plistlib.loads(plist.read_bytes())
        self.assertEqual(data["CFBundleExecutable"], "hello")
        self.assertEqual(data["CFBundleIdentifier"], "org.pybundler.hello")
        self.assertTrue(result.executable.is_file())
        payload = self.run_app(result.executable, ["--name", "app"])
        self.assertEqual(payload["mode"], "onedir")

    def run_app(self, executable: Path, args: list[str], *, cache: Path | None = None) -> dict:
        env = dict(os.environ)
        env["PYTHONHOME"] = ""  # ensure the launcher, not the test, sets it
        if cache is not None:
            env["PYBUNDLER_CACHE_DIR"] = str(cache)
        proc = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            timeout=180,
            env=env,
            cwd=str(self.tmp),
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"stderr:\n{proc.stderr[-4000:]}\nstdout:\n{proc.stdout[-2000:]}",
        )
        lines = [line for line in proc.stdout.splitlines() if line.strip()]
        self.assertTrue(lines, msg=f"no output; stderr:\n{proc.stderr[-4000:]}")
        return json.loads(lines[-1])


if __name__ == "__main__":
    unittest.main()
