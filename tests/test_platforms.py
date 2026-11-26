from __future__ import annotations

import io
import unittest

from pybundler.analysis.finder import FoundModule
from pybundler.analysis.graph import Analysis, EntryResolution
from pybundler.build.plan import Payload
from pybundler.collect.packages import plan_site
from pybundler.collect.runtime import plan_runtime
from pybundler.errors import RuntimeProvisionError
from pybundler.log import Logger
from pybundler.runtimes import Runtime, _select_asset, ensure_runtime, pbs_available
from pybundler.targets import make_target

from support import TempDirTestCase, make_env_info


class TargetTests(unittest.TestCase):
    def test_macos(self):
        target = make_target("darwin", "arm64", "25.0.0")
        self.assertTrue(target.is_macos)
        self.assertEqual(target.arch, "aarch64")
        self.assertEqual(target.triple, "aarch64-apple-darwin")
        self.assertEqual(target.exe_suffix, "")

    def test_windows(self):
        target = make_target("win32", "AMD64", "11")
        self.assertTrue(target.is_windows)
        self.assertEqual(target.arch, "x86_64")
        self.assertEqual(target.bits, 64)
        self.assertEqual(target.exe_suffix, ".exe")
        self.assertEqual(target.pathsep, ";")
        self.assertTrue(pbs_available(target))

    def test_windows_x86(self):
        target = make_target("win32", "AMD64", "11", bits=32)
        self.assertEqual(target.arch, "x86")
        self.assertEqual(target.bits, 32)
        self.assertTrue(target.is_32bit)
        self.assertEqual(target.triple, "i686-pc-windows-msvc")
        self.assertTrue(pbs_available(target))

    def test_linux_x86_local(self):
        target = make_target("linux", "x86_64", "6.8", bits=32)
        self.assertEqual(target.arch, "i686")
        self.assertEqual(target.triple, "i686-unknown-linux-gnu")
        self.assertFalse(pbs_available(target))

    def test_freebsd_x86(self):
        target = make_target("freebsd14", "amd64", "14.1", bits=32)
        self.assertEqual(target.arch, "i686")
        self.assertEqual(target.triple, "i386-unknown-freebsd14")
        self.assertFalse(pbs_available(target))

    def test_32bit_macos_is_rejected(self):
        with self.assertRaises(Exception):
            make_target("darwin", "x86_64", "11", bits=32)

    def test_linux(self):
        target = make_target("linux", "x86_64", "6.8.0")
        self.assertTrue(target.is_linux)
        self.assertTrue(target.is_elf)
        self.assertEqual(target.triple, "x86_64-unknown-linux-gnu")

    def test_freebsd(self):
        target = make_target("freebsd14", "amd64", "14.1-RELEASE")
        self.assertTrue(target.is_freebsd)
        self.assertTrue(target.is_elf)
        self.assertFalse(target.is_linux)
        self.assertEqual(target.triple, "x86_64-unknown-freebsd14")
        self.assertFalse(pbs_available(target))

    def test_unknown_arch(self):
        with self.assertRaises(Exception):
            make_target("linux", "sparc64", "6")

    def test_unknown_platform(self):
        with self.assertRaises(Exception):
            make_target("aix", "x86_64", "7")


class FreeBSDRuntimeTests(TempDirTestCase):
    def test_download_is_rejected(self):
        target = make_target("freebsd14", "amd64", "14.1")
        with self.assertRaises(RuntimeProvisionError) as caught:
            ensure_runtime(
                version="3.11",
                target=target,
                source="download",
                logger=Logger("quiet", stream=io.StringIO()),
            )
        self.assertIn("FreeBSD", str(caught.exception.hint or caught.exception))

    def test_i686_windows_asset_is_selected(self):
        target = make_target("win32", "AMD64", "11", bits=32)
        assets = [
            {"name": "cpython-3.12.16+tag-i686-pc-windows-msvc-install_only_stripped.tar.gz"},
            {"name": "cpython-3.12.16+tag-x86_64-pc-windows-msvc-install_only_stripped.tar.gz"},
        ]
        asset = _select_asset(assets, "3.12", target.triple)
        self.assertIn("i686", str(asset["name"]))

    def test_linux_i686_download_is_rejected(self):
        target = make_target("linux", "x86_64", "6.8", bits=32)
        with self.assertRaises(RuntimeProvisionError):
            ensure_runtime(
                version="3.12",
                target=target,
                source="download",
                logger=Logger("quiet", stream=io.StringIO()),
            )

    def test_local_prefix_is_trimmed(self):
        root = self.tmp / "usr-local"
        stdlib = root / "lib" / "python3.11"
        files = {
            root / "bin" / "python3.11": "#!/bin/sh\n",
            root / "bin" / "gcc": "#!/bin/sh\n",
            root / "lib" / "libpython3.11.so.1.0": "elf",
            root / "lib" / "libssl.so.3": "elf",
            stdlib / "os.py": "",
            stdlib / "lib-dynload" / "_ssl.so": "elf",
            stdlib / "site-packages" / "pip" / "__init__.py": "",
            root / "lib" / "tcl8.6" / "init.tcl": "",
            root / "share" / "man" / "man1" / "python.1": "",
            root / "etc" / "pkg.conf": "",
            root / "include" / "python3.11" / "Python.h": "",
            root / "libdata" / "pkgconfig" / "python.pc": "",
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (root / "bin" / "python3.11").chmod(0o755)
        env = make_env_info(root, version="3.11.9")
        runtime = Runtime(root=root, python=env.python, env=env, source="local")
        payload = Payload()
        layout, warnings = plan_runtime(
            runtime, target=make_target("freebsd14", "amd64", "14.1"), features=set(), payload=payload
        )
        dests = set(payload.items)
        self.assertIn("runtime/python/bin/python3.11", dests)
        self.assertIn("runtime/python/lib/libpython3.11.so.1.0", dests)
        self.assertIn("runtime/python/lib/python3.11/os.py", dests)
        self.assertIn("runtime/python/lib/python3.11/lib-dynload/_ssl.so", dests)
        self.assertNotIn("runtime/python/bin/gcc", dests)
        self.assertNotIn("runtime/python/lib/libssl.so.3", dests)
        self.assertNotIn("runtime/python/lib/python3.11/site-packages/pip/__init__.py", dests)
        self.assertNotIn("runtime/python/lib/tcl8.6/init.tcl", dests)
        self.assertNotIn("runtime/python/share/man/man1/python.1", dests)
        self.assertNotIn("runtime/python/etc/pkg.conf", dests)
        self.assertNotIn("runtime/python/include/python3.11/Python.h", dests)
        self.assertNotIn("runtime/python/libdata/pkgconfig/python.pc", dests)
        self.assertEqual(layout.exe_rel, "runtime/python/bin/python3.11")
        self.assertEqual(layout.stdlib_rel, "runtime/python/lib/python3.11")
        self.assertEqual(warnings, [])

    def test_tcl_tk_in_local_prefix(self):
        root = self.tmp / "usr-local"
        stdlib = root / "lib" / "python3.11"
        (root / "bin").mkdir(parents=True)
        (root / "bin" / "python3.11").write_text("")
        (root / "bin" / "python3.11").chmod(0o755)
        (root / "lib" / "libpython3.11.so").parent.mkdir(parents=True, exist_ok=True)
        (root / "lib" / "libpython3.11.so").write_text("elf")
        (stdlib / "tkinter").mkdir(parents=True)
        (stdlib / "tkinter" / "__init__.py").write_text("")
        (root / "lib" / "tcl8.6").mkdir(parents=True)
        (root / "lib" / "tcl8.6" / "init.tcl").write_text("")
        env = make_env_info(root, version="3.11.9")
        runtime = Runtime(root=root, python=env.python, env=env, source="local")
        payload = Payload()
        plan_runtime(
            runtime,
            target=make_target("freebsd14", "amd64", "14.1"),
            features={"tcl-tk"},
            payload=payload,
        )
        self.assertIn("runtime/python/lib/python3.11/tkinter/__init__.py", payload.items)
        self.assertIn("runtime/python/lib/tcl8.6/init.tcl", payload.items)


class WindowsLocalPrefixTests(TempDirTestCase):
    def test_local_windows_install_is_trimmed(self):
        root = self.tmp / "Python313"
        files = {
            root / "python.exe": "pe",
            root / "pythonw.exe": "pe",
            root / "python313.dll": "pe",
            root / "vcruntime140.dll": "pe",
            root / "Lib" / "os.py": "",
            root / "Lib" / "site-packages" / "pip" / "__init__.py": "",
            root / "DLLs" / "_ssl.pyd": "pe",
            root / "tcl" / "tcl8.6" / "init.tcl": "",
            root / "Scripts" / "pip.exe": "pe",
            root / "include" / "Python.h": "",
            root / "Doc" / "readme.txt": "",
            root / "libs" / "python313.lib": "",
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        from pybundler.interp import EnvInfo

        env = EnvInfo(
            python=root / "python.exe",
            version="3.13.1",
            hexversion=(3 << 24) | (13 << 16),
            implementation="cpython",
            prefix=root,
            base_prefix=root,
            sys_path=(root / "Lib",),
            sysname="win32",
            machine="x86",
            bits=32,
            ext_suffix=".pyd",
            soabi="cp313",
            stdlib=root / "Lib",
            platstdlib=root / "Lib",
            dynload=root / "DLLs",
            purelib=root / "Lib" / "site-packages",
            platlib=root / "Lib" / "site-packages",
            builtin_modules=frozenset(),
            is_venv=False,
        )
        runtime = Runtime(root=root, python=env.python, env=env, source="local")
        payload = Payload()
        plan_runtime(
            runtime,
            target=make_target("win32", "AMD64", "11"),
            features={"tcl-tk"},
            payload=payload,
        )
        dests = set(payload.items)
        self.assertIn("runtime/python/python.exe", dests)
        self.assertIn("runtime/python/pythonw.exe", dests)
        self.assertIn("runtime/python/python313.dll", dests)
        self.assertIn("runtime/python/vcruntime140.dll", dests)
        self.assertIn("runtime/python/Lib/os.py", dests)
        self.assertIn("runtime/python/DLLs/_ssl.pyd", dests)
        self.assertIn("runtime/python/tcl/tcl8.6/init.tcl", dests)
        self.assertNotIn("runtime/python/Lib/site-packages/pip/__init__.py", dests)
        self.assertNotIn("runtime/python/Scripts/pip.exe", dests)
        self.assertNotIn("runtime/python/include/Python.h", dests)
        self.assertNotIn("runtime/python/Doc/readme.txt", dests)
        self.assertNotIn("runtime/python/libs/python313.lib", dests)


class MultiarchLocalPrefixTests(TempDirTestCase):
    def test_only_libpython_is_taken_from_multiarch(self):
        root = self.tmp / "usr"
        stdlib = root / "lib" / "python3.11"
        multiarch = root / "lib" / "x86_64-linux-gnu"
        files = {
            root / "bin" / "python3.11": "#!/bin/sh\n",
            multiarch / "libpython3.11.so.1.0": "elf",
            multiarch / "gio" / "modules" / "libgiognutls.so": "elf",
            multiarch / "libssl.so.3": "elf",
            stdlib / "os.py": "",
        }
        for path, content in files.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        (root / "bin" / "python3.11").chmod(0o755)
        env = make_env_info(root, version="3.11.9")
        runtime = Runtime(root=root, python=env.python, env=env, source="local")
        payload = Payload()
        plan_runtime(
            runtime,
            target=make_target("linux", "x86_64", "6.8"),
            features=set(),
            payload=payload,
        )
        dests = set(payload.items)
        self.assertIn("runtime/python/lib/x86_64-linux-gnu/libpython3.11.so.1.0", dests)
        self.assertNotIn("runtime/python/lib/x86_64-linux-gnu/gio/modules/libgiognutls.so", dests)
        self.assertNotIn("runtime/python/lib/x86_64-linux-gnu/libssl.so.3", dests)


class WheelLibraryDirectoryTests(TempDirTestCase):
    def test_dot_libs_directory_is_copied_with_its_package(self):
        site = self.tmp / "site"
        package = site / "mypkg"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("")
        libs = site / "mypkg.libs"
        libs.mkdir()
        (libs / "libnative.dylib").write_text("dylib")
        unrelated = site / "other.libs"
        unrelated.mkdir()
        (unrelated / "libother.dylib").write_text("dylib")
        module = FoundModule(
            name="mypkg",
            kind="package",
            path=package / "__init__.py",
            is_package=True,
            root=site,
            rel=package.relative_to(site) / "__init__.py",
            category="thirdparty",
        )
        analysis = Analysis(
            entry=EntryResolution(
                mode="script",
                module="main",
                function=None,
                script_source=None,
                script_dest=None,
                search_root=self.tmp,
            ),
            modules={"mypkg": module},
            distributions={},
            data=[],
            binaries=[],
            missing=[],
            warnings=[],
            features=frozenset(),
            project_root=self.tmp,
            site_dirs=(site,),
            stdlib_dirs=(),
        )
        payload = Payload()
        plan_site(analysis, payload)
        self.assertIn("site/mypkg.libs/libnative.dylib", payload.items)
        self.assertNotIn("site/other.libs/libother.dylib", payload.items)


if __name__ == "__main__":
    unittest.main()
