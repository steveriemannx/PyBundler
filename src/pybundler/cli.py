"""Command line interface for PyBundler."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import textwrap
from pathlib import Path
from typing import Any, Sequence

from . import __version__
from .build.bundle import build, prepare_build, summarize
from .build.launcher import find_compiler, zlib_available
from .build.onefile import describe as describe_onefile
from .build.onefile import read_entry
from .collect.licenses import (
    LicenseEntry,
    LicenseReport,
    classify_license,
    write_license_report,
)
from .config import apply_overrides, load_spec
from .errors import PybundlerError, UsageError
from .fsutil import default_cache_dir, human_size, remove_tree, tree_size
from .log import Logger
from .runtimes import list_cached_runtimes, prune_runtimes
from .targets import host_target

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_POLICY = 3


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _level_from_args(args: argparse.Namespace) -> str:
    if getattr(args, "log_level", None):
        return str(args.log_level)
    if getattr(args, "quiet", 0):
        return "error" if args.quiet > 1 else "warn"
    verbose = getattr(args, "verbose", 0)
    if verbose >= 2:
        return "debug"
    if verbose == 1:
        return "verbose"
    return "info"


def _logger(args: argparse.Namespace) -> Logger:
    color = getattr(args, "color", "auto")
    forced = None if color == "auto" else color == "always"
    return Logger(level=_level_from_args(args), color=forced)

def _json_print(payload: Any) -> None:
    json.dump(payload, sys.stdout, indent=2, sort_keys=True)
    sys.stdout.write("\n")


def _analysis_payload(context) -> dict[str, Any]:
    analysis = context.analysis
    return {
        "entry": {
            "mode": analysis.entry.mode,
            "module": analysis.entry.module,
            "function": analysis.entry.function,
            "script": str(analysis.entry.script_source) if analysis.entry.script_source else None,
        },
        "stats": analysis.stats(),
        "features": sorted(analysis.features),
        "modules": {name: module.describe() for name, module in analysis.modules.items()},
        "distributions": {
            name: {
                "version": dist.version,
                "license": dist.license_summary,
                "classification": classify_license(dist.license_summary),
                "metadata": str(dist.dist_info),
            }
            for name, dist in analysis.distributions.items()
        },
        "missing": [
            {"name": item.name, "importer": item.importer, "soft": item.soft}
            for item in analysis.missing
        ],
        "warnings": analysis.warnings,
        "payload": context.payload.summary(),
        "runtime": {
            "version": context.runtime.version,
            "source": context.runtime.source,
            "root": str(context.runtime.root),
            "tag": context.runtime.tag,
            "layout": context.layout.as_dict(),
        },
        "build_id": context.build_id,
    }


def _license_entries_from_bundle(path: Path) -> list[dict[str, Any]]:
    manifest = path / "licenses" / "THIRD-PARTY.json"
    if manifest.is_file():
        data = json.loads(manifest.read_text("utf-8"))
        if isinstance(data, dict):
            return list(data.get("entries") or data.get("licenses") or [])
        return list(data)
    # onefile bundles keep their licences inside the payload; the build report
    # next to the executable has the same information.
    report_candidates = [
        path.with_name(path.name + ".build.json"),
        path.parent / f"{path.name}.build.json",
    ]
    for report_path in report_candidates:
        if report_path.is_file():
            report = json.loads(report_path.read_text("utf-8"))
            entries = report.get("licenses")
            if isinstance(entries, list):
                return entries
    raise UsageError(
        f"no licence manifest found for {path}",
        hint="pass an onedir bundle directory containing licenses/, or build with `pybundler build --verify`",
    )


def _print_license_table(entries: list[dict[str, Any]], *, json_output: bool) -> int:
    if json_output:
        _json_print(entries)
    else:
        width = max((len(str(entry.get("name", ""))) for entry in entries), default=10)
        for entry in entries:
            classification = entry.get("classification") or classify_license(
                str(entry.get("license") or "")
            )
            marker = "ok " if classification == "permissive" else "!! "
            print(
                f"{marker}{str(entry.get('name','')):<{width}}  "
                f"{str(entry.get('version','')):<10}  "
                f"{entry.get('license') or 'unknown'} [{classification}]"
            )
    offenders = [
        entry
        for entry in entries
        if (entry.get("classification") or classify_license(str(entry.get("license") or "")))
        != "permissive"
    ]
    return EXIT_POLICY if offenders else EXIT_OK


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


def cmd_build(args: argparse.Namespace) -> int:
    logger = _logger(args)
    spec = load_spec(args.spec)
    spec = apply_overrides(
        spec,
        mode="onefile" if args.onefile else ("onedir" if args.onedir else args.mode),
        name=args.name,
        console=False if args.windowed else (True if args.console else None),
        python_executable=Path(args.python).expanduser() if args.python else None,
        python_version=args.python_version,
        runtime=args.runtime,
        runtime_path=Path(args.runtime_path).expanduser() if args.runtime_path else None,
        output_dir=Path(args.output_dir).expanduser() if args.output_dir else None,
        work_dir=Path(args.work_dir).expanduser() if args.work_dir else None,
        strip_sources=False if args.keep_sources else (True if args.strip_sources else None),
        compress=False if args.no_compress else None,
        verify=True if args.verify else None,
        keep_work=True if args.keep_work else None,
        hidden_imports=args.hidden_import,
        exclude=args.exclude_module,
        add_data=args.add_data,
        add_binaries=args.add_binary,
        hook_dir=Path(args.hook_dir).expanduser() if args.hook_dir else None,
        include_tests=True if args.include_tests else None,
        pbs_tag=args.pbs_tag,
    )
    result = build(
        spec,
        logger=logger,
        launcher_kind=args.launcher,
        clean=args.clean,
        dry_run=args.dry_run,
    )
    if args.json:
        _json_print(
            {
                "name": result.name,
                "version": result.version,
                "mode": result.mode,
                "path": str(result.path),
                "executable": str(result.executable),
                "launcher": result.launcher,
                "compressed": result.compressed,
                "build_id": result.build_id,
                "size": result.size,
                "size_human": result.size_human,
                "duration_seconds": round(result.duration, 3),
                "warnings": result.warnings,
                "report": str(result.report_path) if result.report_path else None,
                "verification": result.verification,
                "analysis": result.analysis.stats(),
            }
        )
    else:
        print()
        print(summarize(result))
        if result.verification and not result.verification.get("ok"):
            logger.warn("the bundled application exited with an error during verification")
    if result.verification and not result.verification.get("ok"):
        return EXIT_ERROR
    return EXIT_OK


def cmd_inspect(args: argparse.Namespace) -> int:
    logger = _logger(args)
    spec = load_spec(args.spec)
    spec = apply_overrides(
        spec,
        mode="onefile" if args.onefile else None,
        runtime=args.runtime,
        runtime_path=Path(args.runtime_path).expanduser() if args.runtime_path else None,
        hidden_imports=args.hidden_import,
        exclude=args.exclude_module,
        include_tests=True if args.include_tests else None,
    )
    context = prepare_build(spec, logger)
    if args.json:
        _json_print(_analysis_payload(context))
        return EXIT_OK
    analysis = context.analysis
    print(f"entry:        {analysis.entry.mode} {analysis.entry.module or analysis.entry.script_source}")
    print(f"runtime:      Python {context.runtime.version} ({context.runtime.source})")
    print(f"build id:     {context.build_id}")
    print(f"modules:      {len(analysis.modules)} ({len(analysis.third_party)} third-party)")
    print(f"distributions: {len(analysis.distributions)}")
    print(f"features:     {', '.join(sorted(analysis.features)) or 'none'}")
    print(f"payload:      {context.payload.summary()['files']} files, "
          f"{context.payload.summary()['size_human']}")
    if args.distributions:
        print("\ndistributions:")
        for name, dist in analysis.distributions.items():
            print(f"  {name} {dist.version}  [{dist.license_summary}]")
    if args.missing:
        print("\nmissing imports:")
        for item in analysis.missing:
            print(f"  {item.name}  <- {item.importer}" + (" (soft)" if item.soft else ""))
    if args.files:
        print("\npayload files:")
        for dest in sorted(context.payload.items):
            item = context.payload.items[dest]
            print(f"  {item.size:>10}  {dest}")
    return EXIT_OK


def cmd_licenses(args: argparse.Namespace) -> int:
    logger = _logger(args)
    target = Path(args.path).expanduser() if args.path else Path.cwd()
    bundle_dir = target if (target / "licenses" / "THIRD-PARTY.json").is_file() else None
    if (target / "licenses" / "THIRD-PARTY.json").is_file():
        entries = _license_entries_from_bundle(target)
        code = _print_license_table(entries, json_output=args.json)
    elif target.is_file() or (target / "pybundler.json").is_file():
        entries = _license_entries_from_bundle(target)
        code = _print_license_table(entries, json_output=args.json)
    else:
        spec = load_spec(args.path)
        context = prepare_build(spec, logger)
        entries = [
            {
                "name": entry.name,
                "version": entry.version,
                "license": entry.license,
                "classification": entry.classification,
                "files": entry.files,
            }
            for entry in context.licenses.entries
        ]
        code = _print_license_table(entries, json_output=args.json)
    if bundle_dir is not None and not args.json:
        report = LicenseReport(
            entries=[
                LicenseEntry(
                    name=str(entry.get("name", "")),
                    version=str(entry.get("version", "")),
                    license=str(entry.get("license") or ""),
                    classification=str(
                        entry.get("classification")
                        or classify_license(str(entry.get("license") or ""))
                    ),
                    files=[str(path) for path in entry.get("files") or []],
                )
                for entry in entries
            ],
            warnings=[],
        )
        try:
            write_license_report(bundle_dir, report)
        except OSError as exc:
            logger.warn(f"could not write licenses/AUDIT.json: {exc}")
    if code == EXIT_POLICY and not args.check:
        logger.warn(
            "non-permissive licences were found; review them before distributing"
        )
        return EXIT_OK
    return code


def cmd_run(args: argparse.Namespace) -> int:
    bundle = Path(args.bundle).expanduser()
    if not bundle.exists():
        raise UsageError(f"bundle not found: {bundle}")
    executable = _find_executable(bundle)
    if executable is None:
        raise UsageError(
            f"{bundle} does not look like a PyBundler bundle",
            hint="pass the .app directory, the onedir directory or the onefile executable",
        )
    extra = list(args.args or [])
    if os.name != "nt":
        try:
            os.execv(str(executable), [str(executable), *extra])
        except OSError as exc:
            raise UsageError(
                f"cannot execute {executable}: {exc}",
                hint="the bundle may be incomplete or lack execute permissions",
            ) from exc
        return EXIT_OK  # pragma: no cover
    import subprocess

    return subprocess.call([str(executable), *extra])


def _find_executable(bundle: Path) -> Path | None:
    if bundle.is_file():
        return bundle
    if bundle.suffix == ".app":
        macos = bundle / "Contents" / "MacOS"
        plist = bundle / "Contents" / "Info.plist"
        if plist.is_file():
            import plistlib

            try:
                data = plistlib.loads(plist.read_bytes())
            except Exception:  # pragma: no cover - corrupt plist
                data = {}
            executable_name = data.get("CFBundleExecutable")
            if isinstance(executable_name, str):
                candidate = macos / executable_name
                if candidate.is_file():
                    return candidate
        if macos.is_dir():
            candidates = [
                path
                for path in sorted(macos.iterdir())
                if path.is_file() and os.access(path, os.X_OK)
            ]
            if candidates:
                return candidates[0]
        return None
    config = bundle / "pybundler.json"
    if config.is_file():
        data = json.loads(config.read_text("utf-8"))
        name = str(data.get("name") or bundle.name)
        for candidate in (bundle / name, bundle / f"{name}.exe"):
            if candidate.is_file():
                return candidate
    for candidate in sorted(bundle.iterdir()):
        if candidate.is_file() and os.access(candidate, os.X_OK) and candidate.suffix != ".json":
            return candidate
    return None


def cmd_info(args: argparse.Namespace) -> int:
    bundle = Path(args.path).expanduser()
    if not bundle.exists():
        raise UsageError(f"path not found: {bundle}")
    payload: dict[str, Any] = {"path": str(bundle)}
    config = bundle / "pybundler.json"
    if config.is_file():
        payload["config"] = json.loads(config.read_text("utf-8"))
        payload["size"] = tree_size(bundle)
        payload["size_human"] = human_size(payload["size"])
    elif bundle.is_file():
        payload["onefile"] = describe_onefile(bundle)
        embedded = read_entry(bundle, "pybundler.json")
        if embedded is not None:
            try:
                payload["config"] = json.loads(embedded.decode("utf-8"))
            except ValueError:  # pragma: no cover - corrupted payload
                pass
    else:
        raise UsageError(f"{bundle} does not look like a PyBundler bundle")
    if args.json:
        _json_print(payload)
        return EXIT_OK
    for key, value in payload.items():
        if isinstance(value, dict):
            print(f"{key}:")
            for inner_key, inner_value in value.items():
                print(f"  {inner_key}: {inner_value}")
        else:
            print(f"{key}: {value}")
    return EXIT_OK


def cmd_doctor(args: argparse.Namespace) -> int:
    logger = _logger(args)
    target = host_target()
    checks: list[tuple[bool, str, str]] = []

    version_ok = sys.version_info[:2] >= (3, 11)
    checks.append((version_ok, "Python >= 3.11", sys.version.split()[0]))
    checks.append((True, "target", f"{target.sysname} {target.arch} ({target.triple})"))

    compiler = find_compiler()
    checks.append(
        (
            compiler is not None,
            "C compiler",
            compiler or "not found (only --launcher script / onedir will work)",
        )
    )
    if compiler:
        zlib = zlib_available(compiler, bits=target.bits)
        checks.append(
            (
                zlib,
                "zlib",
                "available" if zlib else "missing (onefile bundles will be stored uncompressed)",
            )
        )
    cache = default_cache_dir()
    try:
        cache.mkdir(parents=True, exist_ok=True)
        probe = cache / ".write-test"
        probe.write_text("ok", "utf-8")
        probe.unlink()
        checks.append((True, "cache", str(cache)))
    except OSError as exc:
        checks.append((False, "cache", f"not writable: {exc}"))
    runtimes = list_cached_runtimes()
    checks.append(
        (
            True,
            "cached runtimes",
            ", ".join(str(entry["version"] or "?") for entry in runtimes) or "none",
        )
    )
    checks.append((True, "licence", "PyBundler is BSD-2-Clause; zero runtime dependencies"))

    if args.json:
        _json_print(
            [{"ok": ok, "check": name, "detail": detail} for ok, name, detail in checks]
        )
    else:
        for ok, name, detail in checks:
            print(f"{'ok  ' if ok else 'FAIL'} {name:<18} {detail}")
    failed = [item for item in checks if not item[0]]
    if failed:
        logger.warn(f"{len(failed)} check(s) failed")
        return EXIT_ERROR
    return EXIT_OK


def cmd_init(args: argparse.Namespace) -> int:
    directory = Path(args.directory or ".").expanduser().resolve()
    directory.mkdir(parents=True, exist_ok=True)
    spec_path = directory / "pybundler.toml"
    if spec_path.exists() and not args.force:
        raise UsageError(f"{spec_path} already exists", hint="pass --force to overwrite it")
    raw_name = args.name or directory.name or "app"
    name = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-_.") or "app"
    candidates = ("__main__.py", "main.py", "app.py", "src/main.py", "src/__main__.py")
    entry = next((candidate for candidate in candidates if (directory / candidate).is_file()), None)
    if args.entry:
        entry = args.entry
    lines = [
        "# PyBundler build spec -- https://github.com/pybundler/pybundler",
        "",
        "[app]",
        f'name = "{name}"',
        'version = "0.1.0"',
        f'entry = "{entry or "main.py"}"',
        f'mode = "{args.mode}"',
        "console = true",
        "",
        "[python]",
        f'version = "{sys.version_info.major}.{sys.version_info.minor}"',
        'runtime = "auto"',
        "",
        "[build]",
        'output_dir = "dist"',
        'work_dir = "build"',
        "strip_sources = true  # .pyc only; use --keep-sources for readable tracebacks",
        "compress = true",
        "",
        "[collect]",
        "hidden_imports = []",
        "exclude = []",
        "data = []",
        "binaries = []",
        "",
        "[launch]",
        "args = []",
        "",
        "[launch.env]",
    ]
    spec_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    if entry is None and not args.entry:
        main = directory / "main.py"
        if not main.exists():
            main.write_text(
                'def main() -> int:\n'
                '    print("hello from PyBundler")\n'
                "    return 0\n"
                "\n"
                '\nif __name__ == "__main__":\n'
                "    raise SystemExit(main())\n",
                encoding="utf-8",
            )
    print(f"created {spec_path}")
    print("next: pybundler build")
    return EXIT_OK


def cmd_cache(args: argparse.Namespace) -> int:
    if args.action == "clear":
        remove_tree(default_cache_dir())
        print(f"removed {default_cache_dir()}")
        return EXIT_OK
    if args.action == "prune":
        removed = prune_runtimes(keep=args.keep, logger=_logger(args))
        print(f"removed {len(removed)} runtime(s)")
        return EXIT_OK
    entries = list_cached_runtimes()
    if args.json:
        _json_print(entries)
        return EXIT_OK
    if not entries:
        print(f"no cached runtimes in {default_cache_dir()}")
        return EXIT_OK
    for entry in entries:
        print(
            f"{entry['version'] or '?':<12} {entry['tag'] or '':<12} "
            f"{human_size(entry['size'])}  {entry['path']}"
        )
    return EXIT_OK


def cmd_clean(args: argparse.Namespace) -> int:
    spec = load_spec(args.spec)
    removed: list[Path] = []
    target = host_target()
    candidates = [
        spec.build.work_dir / "pybundler",
        spec.build.output_dir / spec.app.name,
        spec.build.output_dir / f"{spec.app.name}.app",
        spec.build.output_dir / f"{spec.app.name}{target.exe_suffix}",
        spec.build.output_dir / f"{spec.app.name}.build.json",
        spec.build.output_dir / f"{spec.app.name}.app.build.json",
    ]
    paths = [spec.build.work_dir, spec.build.output_dir] if args.all else candidates
    for path in paths:
        if path.exists():
            remove_tree(path)
            removed.append(path)
    if not removed:
        print("nothing to clean")
    for path in removed:
        print(f"removed {path}")
    return EXIT_OK


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------


def _add_logging_flags(parser: argparse.ArgumentParser, *, suppress: bool) -> None:
    """Add -v/-q/--color; subcommands use SUPPRESS so they do not clobber globals."""
    default = argparse.SUPPRESS if suppress else None
    parser.add_argument(
        "-v",
        "--verbose",
        action="count",
        default=default if suppress else 0,
        help="increase verbosity",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="count",
        default=default if suppress else 0,
        help="decrease verbosity",
    )
    parser.add_argument(
        "--log-level",
        choices=["quiet", "error", "warn", "info", "verbose", "debug"],
        default=default,
    )
    parser.add_argument(
        "--color",
        choices=["auto", "always", "never"],
        default=default if suppress else "auto",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="pybundler",
        description="Bundle Python applications into relocatable executables "
        "(BSD-2-Clause, zero dependencies).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(
            """\
            examples:
              pybundler init                      create a pybundler.toml
              pybundler build                     build using ./pybundler.toml
              pybundler build app.toml --onefile  build a single-file executable
              pybundler inspect --missing         explain unresolved imports
              pybundler licenses --check          fail on non-permissive licences
              pybundler run dist/MyApp.app        launch a built bundle
            """
        ),
    )
    parser.add_argument("--version", action="version", version=f"pybundler {__version__}")
    _add_logging_flags(parser, suppress=False)

    subparsers = parser.add_subparsers(dest="command", required=True)

    build_parser_ = subparsers.add_parser("build", help="build a bundle")
    _add_logging_flags(build_parser_, suppress=True)
    build_parser_.add_argument("spec", nargs="?", help="path to pybundler.toml or pyproject.toml")
    mode = build_parser_.add_mutually_exclusive_group()
    mode.add_argument("--onefile", action="store_true", help="produce a single executable")
    mode.add_argument("--onedir", action="store_true", help="produce a directory bundle")
    build_parser_.add_argument("--mode", choices=["onedir", "onefile"], help="bundle mode")
    build_parser_.add_argument("--name", help="override the application name")
    console = build_parser_.add_mutually_exclusive_group()
    console.add_argument("--console", action="store_true", help="console application")
    console.add_argument("--windowed", action="store_true", help="no console window / .app bundle")
    build_parser_.add_argument("--python", help="build environment interpreter")
    build_parser_.add_argument("--python-version", help="target Python version (X.Y)")
    build_parser_.add_argument(
        "--runtime", choices=["auto", "download", "local"], help="where to get the interpreter"
    )
    build_parser_.add_argument("--runtime-path", help="a relocatable runtime directory")
    build_parser_.add_argument("--pbs-tag", help="python-build-standalone release tag")
    build_parser_.add_argument("--output-dir", help="dist directory")
    build_parser_.add_argument("--work-dir", help="build work directory")
    build_parser_.add_argument(
        "--strip-sources",
        action="store_true",
        default=None,
        help="ship only .pyc files (default)",
    )
    build_parser_.add_argument(
        "--keep-sources",
        action="store_true",
        help="ship .py sources for readable tracebacks",
    )
    build_parser_.add_argument("--no-compress", action="store_true", help="store onefile payload uncompressed")
    build_parser_.add_argument("--launcher", choices=["auto", "native", "script"], default="auto")
    build_parser_.add_argument("--hidden-import", action="append", default=[], metavar="MODULE")
    build_parser_.add_argument("--exclude-module", action="append", default=[], metavar="MODULE")
    build_parser_.add_argument("--add-data", action="append", default=[], metavar="SRC:DEST")
    build_parser_.add_argument("--add-binary", action="append", default=[], metavar="SRC:DEST")
    build_parser_.add_argument("--hook-dir", help="directory containing user hook files")
    build_parser_.add_argument("--include-tests", action="store_true", help="ship test directories")
    build_parser_.add_argument("--verify", action="store_true", help="run the bundle after building")
    build_parser_.add_argument("--keep-work", action="store_true", help="keep intermediate files")
    build_parser_.add_argument("--clean", action="store_true", help="remove previous build output")
    build_parser_.add_argument("--dry-run", action="store_true", help="plan without writing a bundle")
    build_parser_.add_argument("--json", action="store_true", help="machine readable output")
    build_parser_.set_defaults(func=cmd_build)

    inspect_parser = subparsers.add_parser("inspect", help="analyse imports and the payload plan")
    _add_logging_flags(inspect_parser, suppress=True)
    inspect_parser.add_argument("spec", nargs="?")
    inspect_parser.add_argument("--onefile", action="store_true")
    inspect_parser.add_argument("--runtime", choices=["auto", "download", "local"])
    inspect_parser.add_argument("--runtime-path")
    inspect_parser.add_argument("--hidden-import", action="append", default=[])
    inspect_parser.add_argument("--exclude-module", action="append", default=[])
    inspect_parser.add_argument("--include-tests", action="store_true")
    inspect_parser.add_argument("--files", action="store_true", help="list every planned file")
    inspect_parser.add_argument("--missing", action="store_true", help="list unresolved imports")
    inspect_parser.add_argument("--distributions", action="store_true", help="list distributions")
    inspect_parser.add_argument("--json", action="store_true")
    inspect_parser.set_defaults(func=cmd_inspect)

    licenses_parser = subparsers.add_parser("licenses", help="audit licences of a bundle or project")
    _add_logging_flags(licenses_parser, suppress=True)
    licenses_parser.add_argument("path", nargs="?", help="bundle directory, .app or spec file")
    licenses_parser.add_argument("--check", action="store_true", help="exit 3 on non-permissive licences")
    licenses_parser.add_argument("--json", action="store_true")
    licenses_parser.set_defaults(func=cmd_licenses)

    run_parser = subparsers.add_parser("run", help="launch a built bundle")
    _add_logging_flags(run_parser, suppress=True)
    run_parser.add_argument("bundle")
    run_parser.add_argument("args", nargs=argparse.REMAINDER)
    run_parser.set_defaults(func=cmd_run)

    info_parser = subparsers.add_parser("info", help="show information about a built bundle")
    _add_logging_flags(info_parser, suppress=True)
    info_parser.add_argument("path")
    info_parser.add_argument("--json", action="store_true")
    info_parser.set_defaults(func=cmd_info)

    doctor_parser = subparsers.add_parser("doctor", help="check the build environment")
    _add_logging_flags(doctor_parser, suppress=True)
    doctor_parser.add_argument("--json", action="store_true")
    doctor_parser.set_defaults(func=cmd_doctor)

    init_parser = subparsers.add_parser("init", help="create a pybundler.toml")
    _add_logging_flags(init_parser, suppress=True)
    init_parser.add_argument("directory", nargs="?")
    init_parser.add_argument("--name")
    init_parser.add_argument("--entry")
    init_parser.add_argument("--mode", choices=["onedir", "onefile"], default="onedir")
    init_parser.add_argument("--force", action="store_true")
    init_parser.set_defaults(func=cmd_init)

    cache_parser = subparsers.add_parser("cache", help="manage cached runtimes and downloads")
    _add_logging_flags(cache_parser, suppress=True)
    cache_parser.add_argument("--json", action="store_true", help="machine readable output")
    cache_sub = cache_parser.add_subparsers(dest="action", required=True)
    cache_json = argparse.ArgumentParser(add_help=False)
    cache_json.add_argument(
        "--json", action="store_true", default=argparse.SUPPRESS, help=argparse.SUPPRESS
    )
    cache_sub.add_parser("list", parents=[cache_json], help="list cached runtimes")
    prune = cache_sub.add_parser("prune", parents=[cache_json], help="remove old cached runtimes")
    prune.add_argument("--keep", type=int, default=2)
    cache_sub.add_parser("clear", parents=[cache_json], help="delete the whole cache")
    cache_parser.set_defaults(func=cmd_cache, json=False)

    clean_parser = subparsers.add_parser("clean", help="remove this project's build output")
    _add_logging_flags(clean_parser, suppress=True)
    clean_parser.add_argument("spec", nargs="?")
    clean_parser.add_argument("--all", action="store_true", help="remove the whole work and dist directories")
    clean_parser.set_defaults(func=cmd_clean)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logger = _logger(args)
    try:
        return int(args.func(args) or 0)
    except PybundlerError as exc:
        logger.error(exc.message)
        if exc.hint:
            for line in str(exc.hint).splitlines():
                logger.info(f"       {line}")
        return exc.exit_code
    except KeyboardInterrupt:  # pragma: no cover - interactive
        logger.error("interrupted")
        return 130
    except BrokenPipeError:  # pragma: no cover - piping into head
        return EXIT_OK


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
