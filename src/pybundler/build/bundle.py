"""End-to-end build orchestration.

The public entry point is :func:`build`.  It follows the same phases as the
CLI output, which makes failures easy to explain:

1. resolve the build environment and the target runtime,
2. analyse imports,
3. plan the payload,
4. materialise, precompile and fix native libraries,
5. emit the launcher (native or script),
6. install the bundle and write a build report.
"""

from __future__ import annotations

import glob as globlib
import hashlib
import json
import os
import shutil
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import __version__
from . import onefile as onefile_mod
from .launcher import (
    LauncherConfig,
    compile_launcher,
    find_compiler,
    precompile,
    write_script_launcher,
    zlib_available,
)
from .macos import assemble_app
from .plan import Payload
from ..analysis.graph import Analysis, DataFile, BinaryFile, analyze, build_finder, resolve_entry
from ..analysis.hooks import HookRegistry
from ..collect.binaries import fixup_shared_libraries
from ..collect.licenses import LicenseReport, plan_licenses
from ..collect.packages import plan_site
from ..collect.runtime import RuntimeLayout, plan_runtime
from ..config import ConfigError, Spec
from ..errors import BuildError, ToolchainError
from ..fsutil import (
    ensure_dir,
    human_size,
    make_executable,
    remove_tree,
    tree_size,
)
from ..interp import check_compatible, analyze_environment
from ..log import Logger
from ..runtimes import Runtime, ensure_runtime
from ..targets import Target, host_target

SCHEMA_VERSION = 1


@dataclass
class BuildContext:
    spec: Spec
    target: Target
    runtime: Runtime
    layout: RuntimeLayout
    analysis: Analysis
    payload: Payload
    licenses: LicenseReport
    entry_payload: dict[str, object]
    build_id: str
    warnings: list[str] = field(default_factory=list)


@dataclass
class BuildResult:
    name: str
    version: str
    mode: str
    path: Path
    executable: Path
    launcher: str
    compressed: bool
    build_id: str
    size: int
    duration: float
    payload: dict[str, object]
    analysis: Analysis
    warnings: list[str]
    report_path: Path | None = None
    verification: dict[str, object] | None = None

    @property
    def size_human(self) -> str:
        return human_size(self.size)


# ---------------------------------------------------------------------------
# Extra files from the spec
# ---------------------------------------------------------------------------


def _split_pair(raw: str) -> tuple[str, str | None]:
    """Split ``"src:dest"``; also accepts ``os.pathsep`` and keeps drive letters intact."""
    source, separator, dest = raw.partition(os.pathsep)
    if separator:
        return source.strip(), dest.strip()
    if len(raw) > 2 and raw[1] == ":" and raw[2] in "\\/":
        index = raw.find(":", 2)  # skip the "C:" drive prefix
        if index > 0:
            return raw[:index].strip(), raw[index + 1 :].strip()
        return raw.strip(), None
    source, separator, dest = raw.partition(":")
    return source.strip(), (dest.strip() if separator else None)


def _entry_dest(source: Path, explicit: str | None, project_root: Path) -> str:
    if explicit:
        dest = explicit.replace("\\", "/")
        if dest.endswith("/") or (source.is_file() and not Path(dest).suffix):
            dest = f"{dest.rstrip('/')}/{source.name}"
        return dest.lstrip("/")
    try:
        return source.relative_to(project_root).as_posix()
    except ValueError:
        return source.name


def resolve_extra_entries(
    patterns: list[str],
    *,
    project_root: Path,
    kind: str,
    logger: Logger | None = None,
) -> list[tuple[Path, str]]:
    """Expand ``data``/``binaries`` patterns into ``(source, bundle path)``."""
    entries: list[tuple[Path, str]] = []
    for raw in patterns:
        if not raw or not raw.strip():
            continue
        source_pattern, explicit = _split_pair(raw.strip())
        candidate = Path(source_pattern).expanduser()
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if globlib.has_magic(source_pattern):
            matches = [Path(match) for match in sorted(globlib.glob(str(candidate), recursive=True))]
            if not matches:
                if logger is not None:
                    logger.warn(f"{kind} pattern matched nothing: {raw!r}")
                continue
        else:
            if not candidate.exists():
                raise ConfigError(
                    f"{kind} entry not found: {candidate}",
                    hint=f"check the `{kind}` list in your spec file",
                )
            matches = [candidate]
        for match in matches:
            entries.append((match, _entry_dest(match, explicit, project_root)))
    return entries


# ---------------------------------------------------------------------------
# Payload identity
# ---------------------------------------------------------------------------


def payload_build_id(payload: Payload, *, spec: Spec, runtime: Runtime | None) -> str:
    """Deterministic content id, used for the onefile cache key."""
    digest = hashlib.sha256()
    for dest in sorted(payload.items):
        if dest == "pybundler.json":
            continue
        item = payload.items[dest]
        digest.update(dest.encode("utf-8"))
        digest.update(b"\x00")
        digest.update(str(item.size).encode("ascii"))
        digest.update(b"\x00")
    digest.update(
        f"{spec.app.name}|{spec.app.version}|{spec.app.mode}|"
        f"{runtime.version if runtime else ''}|{int(spec.build.strip_sources)}".encode("utf-8")
    )
    return digest.hexdigest()[:12]


# ---------------------------------------------------------------------------
# Preparation (shared by build and inspect)
# ---------------------------------------------------------------------------


def prepare_build(spec: Spec, logger: Logger) -> BuildContext:
    target = host_target()
    logger.step("Resolving build environment")
    env, search_paths = analyze_environment(
        project_root=spec.project_root,
        interpreter=spec.python.executable,
    )
    if env.implementation != "cpython":
        raise BuildError(
            f"unsupported interpreter implementation {env.implementation!r}",
            hint="PyBundler bundles CPython and cannot freeze PyPy or GraalPy applications",
        )
    resolution = resolve_entry(spec.app.entry, spec.project_root)
    logger.detail(f"build interpreter: {env.python} (Python {env.version})")
    logger.detail(f"entry: {resolution.mode} {resolution.module or resolution.script_source}")

    requested = spec.python.version
    if requested is not None:
        requested_minor = tuple(int(part) for part in requested.split(".")[:2] if part.isdigit())
        if len(requested_minor) == 2 and requested_minor != env.major_minor:
            raise BuildError(
                f"requested Python {requested} does not match the build environment "
                f"({env.version})",
                hint=(
                    "compiled extensions are only compatible within one minor version; "
                    "create a virtualenv with the requested Python and build inside it"
                ),
            )

    logger.step("Preparing Python runtime")
    runtime_source = spec.python.runtime
    if spec.python.runtime_path is not None and runtime_source != "download":
        runtime_source = "local"
    runtime = ensure_runtime(
        version=spec.python.version or env.major_minor_str,
        target=target,
        source=runtime_source,
        local_python=env.python,
        local_root=spec.python.runtime_path,
        tag=spec.python.pbs_tag,
        logger=logger,
    )
    warnings = check_compatible(env, runtime.env)
    logger.detail(f"runtime: Python {runtime.version} ({runtime.source}) from {runtime.root}")

    logger.step("Analysing imports")
    finder, site_dirs, stdlib_dirs = build_finder(
        env=env,
        runtime_env=runtime.env,
        project_root=spec.project_root,
        entry_root=resolution.search_root,
        extra_paths=search_paths,
    )
    hooks = HookRegistry(spec.collect.hooks, logger=logger)
    extra_data = [
        DataFile(source=source, dest=dest, origin="spec")
        for source, dest in resolve_extra_entries(
            spec.collect.data, project_root=spec.project_root, kind="data", logger=logger
        )
    ]
    extra_binaries = [
        BinaryFile(source=source, dest=dest, origin="spec")
        for source, dest in resolve_extra_entries(
            spec.collect.binaries, project_root=spec.project_root, kind="binaries", logger=logger
        )
    ]
    analysis = analyze(
        entry=spec.app.entry,
        project_root=spec.project_root,
        finder=finder,
        hooks=hooks,
        logger=logger,
        hidden_imports=spec.collect.hidden_imports,
        packages=spec.collect.packages,
        exclusions=spec.collect.exclude,
        include_tests=spec.collect.include_tests,
        extra_data=extra_data,
        extra_binaries=extra_binaries,
    )
    for warning in analysis.warnings:
        logger.warn(warning)
    hard_missing = [item for item in analysis.missing if not item.soft]
    if hard_missing:
        logger.warn(
            f"{len(hard_missing)} import(s) could not be resolved; "
            "they may be provided by the OS or optional at runtime"
        )
        for item in hard_missing[:15]:
            logger.detail(f"missing {item.name!r} imported by {item.importer}")
        if len(hard_missing) > 15:
            logger.detail(f"... and {len(hard_missing) - 15} more")
    logger.result(
        f"{analysis.stats()['modules']} modules, "
        f"{analysis.stats()['distributions']} distributions"
    )

    logger.step("Planning payload")
    payload = Payload()
    layout, runtime_warnings = plan_runtime(
        runtime,
        target=target,
        features=set(analysis.features),
        payload=payload,
        logger=logger,
    )
    plan_site(
        analysis,
        payload,
        include_tests=spec.collect.include_tests,
        exclusions=spec.collect.exclude,
        logger=logger,
    )
    licenses = plan_licenses(
        payload, analysis, runtime, project_root=spec.project_root, logger=logger
    )
    warnings.extend(runtime_warnings)
    warnings.extend(licenses.warnings)
    warnings.extend(payload.warnings)

    entry_payload: dict[str, object]
    if resolution.mode == "script":
        assert resolution.script_dest
        entry_payload = {"type": "script", "path": f"site/{resolution.script_dest}"}
    elif resolution.mode == "module":
        entry_payload = {"type": "module", "module": resolution.module}
    else:
        entry_payload = {
            "type": "callable",
            "module": resolution.module,
            "function": resolution.function,
        }

    payload.add_text(
        (Path(__file__).resolve().parent.parent / "resources" / "boot.py").read_text("utf-8"),
        "site/_pybundler_boot.py",
        origin="bootstrap",
    )
    build_id = payload_build_id(payload, spec=spec, runtime=runtime)

    logger.result(
        f"{len(payload.items)} files, {human_size(payload.total_size())} planned"
    )
    return BuildContext(
        spec=spec,
        target=target,
        runtime=runtime,
        layout=layout,
        analysis=analysis,
        payload=payload,
        licenses=licenses,
        entry_payload=entry_payload,
        build_id=build_id,
        warnings=warnings,
    )


# ---------------------------------------------------------------------------
# Installation helpers
# ---------------------------------------------------------------------------


def _swap_into_place(target: Path, producer) -> None:
    tmp = target.with_name(f".{target.name}.tmp")
    backup = target.with_name(f".{target.name}.old")
    remove_tree(tmp)
    remove_tree(backup)
    ensure_dir(target.parent)
    try:
        producer(tmp)
        if target.exists() or target.is_symlink():
            os.replace(target, backup)
        os.replace(tmp, target)
    except BaseException:
        remove_tree(tmp)
        raise
    remove_tree(backup)


def _app_executable(path: Path, name: str, *, onefile: bool, target: Target) -> Path:
    if path.suffix == ".app":
        return path / "Contents" / "MacOS" / name
    if onefile:
        return path
    return path / (name + target.exe_suffix)


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def build(
    spec: Spec,
    *,
    logger: Logger,
    launcher_kind: str = "auto",
    clean: bool = False,
    dry_run: bool = False,
) -> BuildResult:
    started = time.monotonic()
    target = host_target()
    context = prepare_build(spec, logger)
    spec = context.spec
    dist_dir = ensure_dir(spec.build.output_dir)
    app_name = spec.app.name
    onefile = spec.app.mode == "onefile"
    windowed = not spec.app.console
    wants_app_bundle = windowed and target.is_macos
    work_root = spec.build.work_dir / "pybundler" / app_name
    payload_dir = work_root / "payload"
    if clean:
        remove_tree(work_root)
    ensure_dir(work_root)

    final_name = app_name + (target.exe_suffix if onefile else "")
    if wants_app_bundle:
        final_path = dist_dir / f"{app_name}.app"
    else:
        final_path = dist_dir / final_name

    if dry_run:
        logger.step("Dry run: nothing written")
        return BuildResult(
            name=app_name,
            version=spec.app.version,
            mode=spec.app.mode,
            path=final_path,
            executable=_app_executable(final_path, app_name, onefile=onefile, target=target),
            launcher="none",
            compressed=False,
            build_id=context.build_id,
            size=0,
            duration=time.monotonic() - started,
            payload=context.payload.summary(),
            analysis=context.analysis,
            warnings=context.warnings,
        )

    compiler = find_compiler()
    if launcher_kind == "auto":
        launcher_kind = "native" if compiler else "script"
    if launcher_kind not in ("native", "script"):
        raise ToolchainError(f"unknown launcher kind {launcher_kind!r}")
    if launcher_kind == "script" and onefile:
        raise ToolchainError(
            "onefile bundles need a native launcher (it extracts the payload on first run)",
            hint="install a C compiler, or build with --mode onedir",
        )
    compress = bool(spec.build.compress and onefile)
    if compress and not zlib_available(compiler, bits=target.bits):
        logger.warn("zlib is unavailable; storing onefile payload without compression")
        compress = False

    logger.step("Materialising bundle")
    remove_tree(payload_dir)
    written = context.payload.materialize(payload_dir)
    logger.result(f"{written} files, {human_size(tree_size(payload_dir))} on disk")

    logger.step("Precompiling bytecode")
    protect: set[Path] = set()
    if context.analysis.entry.mode == "script" and context.analysis.entry.script_dest:
        protect.add(payload_dir / "site" / context.analysis.entry.script_dest)
    compile_failures = precompile(
        context.runtime.python,
        home=context.runtime.root,
        site_dir=payload_dir / "site",
        stdlib_dir=payload_dir / context.layout.stdlib_rel,
        strip_sources=spec.build.strip_sources,
        protect=protect,
        logger=logger,
    )
    if compile_failures:
        logger.warn(f"{len(compile_failures)} file(s) could not be precompiled")
    else:
        logger.result("bytecode up to date")

    logger.step("Fixing native libraries")
    fixup = fixup_shared_libraries(
        payload_dir,
        target=target,
        python_home=payload_dir / context.layout.home_rel,
        features=set(context.analysis.features),
        logger=logger,
    )
    for warning in fixup.warnings:
        logger.warn(warning)
    context.warnings.extend(fixup.warnings)
    if fixup.added:
        native_manifest = [
            {"path": dest, "source": fixup.sources.get(dest)}
            for dest in sorted(fixup.added)
        ]
        native_path = payload_dir / "licenses" / "NATIVE-LIBRARIES.json"
        native_path.parent.mkdir(parents=True, exist_ok=True)
        native_path.write_text(
            json.dumps(native_manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        note = (
            f"{len(fixup.added)} native library/libraries were copied from the build "
            "machine; their licences are not part of Python package metadata "
            "(see licenses/NATIVE-LIBRARIES.json)"
        )
        context.warnings.append(note)
        logger.warn(note)
    logger.result(
        f"{len(fixup.added)} copied, {len(fixup.rewritten)} load command(s) rewritten"
    )

    config_json = {
        "schema": SCHEMA_VERSION,
        "name": app_name,
        "version": spec.app.version,
        "mode": spec.app.mode,
        "python_version": context.runtime.version,
        "build_id": context.build_id,
        "entry": context.entry_payload,
        "runtime": context.layout.as_dict(),
        "site": "site",
        "sys_path": ["site"],
        "features": sorted(context.analysis.features),
        "env": dict(spec.launch.env),
        "pybundler": __version__,
    }
    (payload_dir / "pybundler.json").write_text(
        json.dumps(config_json, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    launcher_config = LauncherConfig(
        name=app_name,
        build_id=context.build_id,
        onefile=onefile,
        home_rel=context.layout.home_rel,
        exe_rel=context.layout.exe_rel,
        site_rel="site",
        lib_rel="lib",
        windowed=windowed,
        console=spec.app.console,
        compress=compress,
        icon=spec.app.icon,
    )

    logger.step("Building launcher")
    launcher_binary: Path | None = None
    if launcher_kind == "native":
        launcher_binary = work_root / f"launcher{target.exe_suffix}"
        compile_launcher(
            launcher_config, launcher_binary, target=target, compiler=compiler, logger=logger
        )
        logger.result(f"launcher compiled with {compiler}")
    else:
        logger.result("using script launcher")

    report: dict[str, object] = {
        "schema": SCHEMA_VERSION,
        "name": app_name,
        "version": spec.app.version,
        "mode": spec.app.mode,
        "launcher": launcher_kind,
        "compressed": compress,
        "build_id": context.build_id,
        "pybundler_version": __version__,
        "python": {
            "version": context.runtime.version,
            "root": str(context.runtime.root),
            "source": context.runtime.source,
            "tag": context.runtime.tag,
            "asset": context.runtime.asset,
        },
        "target": {
            "sysname": target.sysname,
            "arch": target.arch,
            "triple": target.triple,
        },
        "entry": context.entry_payload,
        "analysis": context.analysis.stats(),
        "payload": context.payload.summary(),
        "native_libraries": {
            "added": fixup.added,
            "rewritten": fixup.rewritten,
            "sources": fixup.sources,
        },
        "licenses": [
            {
                "name": entry.name,
                "version": entry.version,
                "license": entry.license,
                "classification": entry.classification,
            }
            for entry in context.licenses.entries
        ],
        "warnings": context.warnings,
        "bytecode_failures": compile_failures,
        "built": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
    }

    onefile_info: dict[str, object] | None = None
    if onefile:
        logger.step("Packing onefile payload")
        container_path = work_root / f"{app_name}.pbf"
        container = onefile_mod.write_container(
            payload_dir, container_path, compress=compress
        )
        onefile_info = {
            "container_size": container.size,
            "files": container.count,
            "sha256": container.sha256,
        }
        logger.result(
            f"{container.count} files, {human_size(container.size)} "
            f"({'zlib' if compress else 'stored'})"
        )
        assert launcher_binary is not None

        def produce_executable(tmp_path: Path) -> None:
            onefile_mod.append_container(launcher_binary, container_path, tmp_path)
            make_executable(tmp_path)
            # Do not re-sign here: appending the payload invalidates a code
            # signature as far as `codesign` is concerned, and the kernel
            # accepts the linker-signed pages (PyInstaller does the same).
            # Notarized distribution needs a signed .app wrapper instead.

        if wants_app_bundle:
            def producer(tmp_path: Path) -> None:
                executable_tmp = work_root / f"{app_name}-onefile{target.exe_suffix}"
                produce_executable(executable_tmp)
                assemble_app(
                    tmp_path,
                    payload_root=payload_dir,
                    executable_source=executable_tmp,
                    name=app_name,
                    version=spec.app.version,
                    bundle_id=spec.app.bundle_id,
                    icon=spec.app.icon,
                    onefile=True,
                )
                executable_tmp.unlink(missing_ok=True)
        else:
            def producer(tmp_path: Path) -> None:
                produce_executable(tmp_path)
        _swap_into_place(final_path, producer)
        remove_tree(payload_dir)
    else:
        logger.step("Installing bundle")
        launcher_name = app_name + target.exe_suffix
        launcher_path = payload_dir / launcher_name
        if launcher_kind == "native":
            assert launcher_binary is not None
            shutil.move(str(launcher_binary), launcher_path)
            make_executable(launcher_path)
        else:
            write_script_launcher(launcher_config, payload_dir, target=target)

        if wants_app_bundle:
            def producer(tmp_path: Path) -> None:
                assemble_app(
                    tmp_path,
                    payload_root=payload_dir,
                    executable_source=None,
                    name=app_name,
                    version=spec.app.version,
                    bundle_id=spec.app.bundle_id,
                    icon=spec.app.icon,
                    onefile=False,
                )
        else:
            def producer(tmp_path: Path) -> None:
                shutil.move(str(payload_dir), tmp_path)
        _swap_into_place(final_path, producer)
        if payload_dir.exists():
            remove_tree(payload_dir)

    executable = _app_executable(final_path, app_name, onefile=onefile, target=target)
    if not executable.exists():
        raise BuildError(f"internal error: expected executable at {executable}")

    report["onefile"] = onefile_info
    report["path"] = str(final_path)
    report["executable"] = str(executable)
    report["size"] = final_path.stat().st_size if final_path.is_file() else tree_size(final_path)
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report_path = dist_dir / f"{final_path.name}.build.json"
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    verification = None
    if spec.build.verify:
        logger.step("Verifying bundle")
        verification = _verify(executable, spec.launch.args, logger=logger)
        report["verification"] = verification
        if verification["ok"]:
            logger.result(f"exit code {verification['returncode']}")
        else:
            logger.warn(f"verification failed: {verification.get('error') or verification['returncode']}")
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")

    if not spec.build.keep_work:
        remove_tree(work_root)
    size = final_path.stat().st_size if final_path.is_file() else tree_size(final_path)
    return BuildResult(
        name=app_name,
        version=spec.app.version,
        mode=spec.app.mode,
        path=final_path,
        executable=executable,
        launcher=launcher_kind,
        compressed=compress,
        build_id=context.build_id,
        size=size,
        duration=time.monotonic() - started,
        payload=context.payload.summary(),
        analysis=context.analysis,
        warnings=context.warnings,
        report_path=report_path,
        verification=verification,
    )


def _verify(executable: Path, args: list[str], *, logger: Logger, timeout: float = 120.0) -> dict[str, object]:
    started = time.monotonic()
    try:
        proc = subprocess.run(
            [str(executable), *args],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            cwd=str(executable.parent),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"timed out after {timeout:.0f}s",
            "duration": round(time.monotonic() - started, 3),
            "stdout": (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else "",
        }
    except OSError as exc:
        return {"ok": False, "error": str(exc), "duration": 0.0}
    if logger.verbosity >= 4 and proc.stdout:
        for line in proc.stdout.splitlines()[:40]:
            logger.detail(f"stdout: {line}")
    if logger.verbosity >= 4 and proc.stderr:
        for line in proc.stderr.splitlines()[:40]:
            logger.detail(f"stderr: {line}")
    return {
        "ok": proc.returncode == 0,
        "returncode": proc.returncode,
        "duration": round(time.monotonic() - started, 3),
        "stdout": proc.stdout[-4000:],
        "stderr": proc.stderr[-4000:],
    }


def summarize(result: BuildResult) -> str:
    lines = [
        f"bundle:  {result.path}",
        f"launch:  {result.executable}",
        f"mode:    {result.mode} ({result.launcher} launcher"
        + (", compressed" if result.compressed else "")
        + ")",
        f"size:    {result.size_human}",
        f"python:  {result.analysis.stats()['modules']} modules analysed, "
        f"build id {result.build_id}",
    ]
    if result.report_path is not None:
        lines.append(f"report:  {result.report_path}")
    return "\n".join(lines)
