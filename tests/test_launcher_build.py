from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

from pybundler.build.launcher import (
    LauncherConfig,
    build_compile_invocation,
    compiler_family,
    find_compiler,
)
from pybundler.errors import BuildError
from pybundler.interp import check_compatible

from support import TempDirTestCase, make_env_info


def config(**overrides) -> LauncherConfig:
    values = dict(
        name="demo",
        build_id="0123456789ab",
        onefile=True,
        home_rel="runtime/python",
        exe_rel="runtime/python/python.exe",
        site_rel="site",
        lib_rel="lib",
        windowed=False,
        console=True,
        compress=True,
    )
    values.update(overrides)
    return LauncherConfig(**values)


class CompilerFamilyTests(unittest.TestCase):
    def test_families(self):
        self.assertEqual(compiler_family("cl"), "msvc")
        self.assertEqual(compiler_family("C:\\VS\\cl.exe"), "msvc")
        self.assertEqual(compiler_family("clang-cl"), "msvc")
        self.assertEqual(compiler_family("cc"), "gcc")
        self.assertEqual(compiler_family("/usr/bin/clang"), "gcc")
        self.assertEqual(compiler_family("gcc"), "gcc")

    def test_windows_prefers_msvc_over_gcc(self):
        with mock.patch.object(sys, "platform", "win32"), mock.patch.dict(
            "os.environ", {"CC": ""}, clear=False
        ), mock.patch(
            "shutil.which", side_effect=lambda name: "C:\\msvc\\" + name if name in ("cl", "gcc") else None
        ):
            self.assertEqual(find_compiler(), "C:\\msvc\\cl")


class CompileInvocationTests(TempDirTestCase):
    def test_msvc_command_uses_static_crt_and_config_header(self):
        output = self.tmp / "launcher.exe"
        invocation = build_compile_invocation(config(), output, "cl", compress=False)
        command = invocation.command
        self.assertIn("/MT", command)
        self.assertIn("/FIpybundler_launcher_config.h", command)
        self.assertIn("/Fopybundler_launcher.obj", command)
        self.assertIn("/Felauncher.exe", command)
        self.assertIn("shell32.lib", command)
        self.assertIn("user32.lib", command)
        self.assertIn("/link", command)
        self.assertNotIn("zlib.lib", command)
        self.assertEqual(invocation.cwd, output.parent)
        header = invocation.header.read_text("utf-8")
        self.assertIn('#define PYB_NAME "demo"', header)
        self.assertIn("#define PYB_MODE_ONEFILE 1", header)
        self.assertIn("#define PYB_USE_ZLIB 0", header)
        self.assertIn("#define PYB_CONSOLE 1", header)

    def test_msvc_compression_links_zlib(self):
        invocation = build_compile_invocation(
            config(compress=True), self.tmp / "launcher.exe", "clang-cl", compress=True
        )
        self.assertIn("zlib.lib", invocation.command)
        self.assertIn("#define PYB_USE_ZLIB 1", invocation.header.read_text("utf-8"))

    def test_gcc_invocation(self):
        output = self.tmp / "launcher"
        invocation = build_compile_invocation(config(), output, "cc", compress=True)
        command = invocation.command
        self.assertEqual(command[0], "cc")
        self.assertIn("-std=c99", command)
        self.assertIn("-include", command)
        self.assertIn("pybundler_launcher_config.h", command)
        self.assertIn("-lz", command)
        self.assertEqual(invocation.object_file, None)

    def test_posix_32bit_adds_m32(self):
        from pybundler.targets import make_target

        invocation = build_compile_invocation(
            config(),
            self.tmp / "launcher",
            "cc",
            compress=False,
            target=make_target("linux", "x86_64", "6.8", bits=32),
        )
        self.assertIn("-m32", invocation.command)


class BitnessCompatibilityTests(TempDirTestCase):
    def test_mismatched_bitness_is_rejected(self):
        env = make_env_info(self.tmp, version="3.12.8")
        from dataclasses import replace

        runtime = replace(env, bits=32)
        with self.assertRaises(BuildError):
            check_compatible(env, runtime)


if __name__ == "__main__":
    unittest.main()
