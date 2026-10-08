#!/usr/bin/env python3
"""Build an Apple Silicon Pakati app and drag-to-install disk image."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import platform
import plistlib
import shutil
import subprocess
import sys
import tempfile

HERE = Path(__file__).resolve().parent
OUTPUTS = HERE.parent if HERE.parent.name == "outputs" else HERE / "outputs"
WORK = (OUTPUTS.parent / "work" / "agent-relay-mac" if HERE.parent.name == "outputs"
        else HERE / "work")
ENGINE = WORK / "dist" / "relay-engine"
APP = OUTPUTS / "Pakati.app"
APP_ARCHIVE = OUTPUTS / "Pakati-mac-app.zip"
DMG = OUTPUTS / "Pakati-mac.dmg"
VERSION = "0.3.0"
PYINSTALLER_VERSION = "6.22.0"


def run(*arguments: object, env=None) -> None:
    print("Running: " + " ".join(str(x) for x in arguments), flush=True)
    subprocess.run([str(x) for x in arguments], check=True, env=env)


def copy_payload(source, destination):
    """Copy bytes and mode, never Finder or FileProvider extended attributes."""
    result = shutil.copyfile(source, destination)
    Path(destination).chmod(Path(source).stat().st_mode & 0o777)
    return result


def build_engine() -> Path:
    venv = WORK / "venv"
    python = venv / "bin" / "python"
    if not python.exists():
        run(sys.executable, "-m", "venv", venv)
    probe = subprocess.run([str(python), "-c", "import PyInstaller; print(PyInstaller.__version__)"],
                           capture_output=True, text=True)
    if probe.returncode or probe.stdout.strip() != PYINSTALLER_VERSION:
        env = os.environ.copy()
        env["PIP_CACHE_DIR"] = str(WORK / "pip-cache")
        run(python, "-m", "pip", "install", "--disable-pip-version-check",
            "pyinstaller==" + PYINSTALLER_VERSION, env=env)
    env = os.environ.copy()
    env["PYINSTALLER_CONFIG_DIR"] = str(WORK / "pyinstaller-cache")
    run(python, "-m", "PyInstaller", "--noconfirm", "--clean", "--onefile",
        "--name", "relay-engine", "--target-arch", "arm64",
        "--distpath", WORK / "dist", "--workpath", WORK / "pyinstaller",
        "--specpath", WORK, "--paths", HERE / "Engine", HERE / "Engine" / "engine.py", env=env)
    run("/usr/bin/codesign", "--force", "--sign", "-", ENGINE)
    run(ENGINE, "preflight")
    return python


def collect_licenses(resources: Path, python: Path | None) -> None:
    destination = resources / "Licenses"
    destination.mkdir()
    source = HERE / "LICENSE"
    if source.exists():
        copy_payload(source, destination / "Pakati-LICENSE.txt")
    if python and python.exists():
        # Fetch the licenses from the exact interpreter and PyInstaller used.
        script = "import pathlib, sys; print(pathlib.Path(sys.base_prefix) / 'lib' / ('python%d.%d' % sys.version_info[:2]) / 'LICENSE.txt')"
        path = Path(subprocess.check_output([str(python), "-c", script], text=True).strip())
        if path.exists():
            copy_payload(path, destination / "Python-LICENSE.txt")
        script = "import importlib.metadata as m; d=m.distribution('pyinstaller'); print(next(str(d.locate_file(f)) for f in d.files if str(f).lower().endswith(('copying.txt', 'license', 'license.txt'))))"
        result = subprocess.run([str(python), "-c", script], capture_output=True, text=True)
        if result.returncode == 0 and Path(result.stdout.strip()).exists():
            copy_payload(result.stdout.strip(), destination / "PyInstaller-COPYING.txt")


def build_app(python: Path | None, temporary_root: Path) -> Path:
    source = HERE / "Sources" / "AgentRelayApp.swift"
    if not source.exists():
        raise RuntimeError("Native app source is missing: " + str(source))
    if not ENGINE.exists():
        raise RuntimeError("Engine is missing; omit --skip-engine to build it.")
    staging = temporary_root / "Pakati.app"
    contents = staging / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    macos.mkdir(parents=True)
    resources.mkdir()
    env = os.environ.copy()
    env["MACOSX_DEPLOYMENT_TARGET"] = "13.0"
    run("xcrun", "swiftc", "-O", "-parse-as-library", "-target", "arm64-apple-macosx13.0",
        "-module-cache-path", WORK / "swift-module-cache", source,
        "-o", macos / "Pakati", env=env)
    copy_payload(ENGINE, resources / "relay-engine")
    icon = HERE / "Assets" / "AppIcon.icns"
    if icon.exists():
        copy_payload(icon, resources / "AppIcon.icns")
    for name in ("README.md", "VERIFICATION.md"):
        document = HERE / name
        if document.exists():
            copy_payload(document, resources / name)
    collect_licenses(resources, python)
    info = {
        "CFBundleDevelopmentRegion": "en", "CFBundleExecutable": "Pakati",
        "CFBundleIdentifier": "local.agentrelay.desktop", "CFBundleName": "Pakati",
        "CFBundleDisplayName": "Pakati", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION, "CFBundleVersion": "4",
        "LSMinimumSystemVersion": "13.0", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication",
        "NSHumanReadableCopyright": "Pakati contributors. MIT License.",
    }
    if icon.exists():
        info["CFBundleIconFile"] = "AppIcon"
    (contents / "Info.plist").write_bytes(plistlib.dumps(info))
    # Signing requires a filesystem without FileProvider-added Finder metadata.
    # Only app and disk-image staging use OS temp; build caches stay under work.
    run("/usr/bin/xattr", "-cr", staging)
    run("/usr/bin/codesign", "--force", "--sign", "-", resources / "relay-engine")
    run("/usr/bin/codesign", "--force", "--deep", "--sign", "-", staging)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", staging)
    print("Verified clean app bundle: " + str(staging), flush=True)
    return staging


def archive_app(app: Path) -> None:
    # Keep the signed bundle inside an archive when the output folder itself is
    # managed by FileProvider; that provider can modify loose .app metadata.
    APP_ARCHIVE.unlink(missing_ok=True)
    run("/usr/bin/ditto", "-c", "-k", "--keepParent", app, APP_ARCHIVE)
    if APP.exists():
        info = APP / "Contents" / "Info.plist"
        if info.exists() and plistlib.loads(info.read_bytes()).get("CFBundleIdentifier") == "local.agentrelay.desktop":
            shutil.rmtree(APP)
    print("Created " + str(APP_ARCHIVE), flush=True)


def build_dmg(app: Path, temporary_root: Path) -> None:
    staging = temporary_root / "dmg-stage"
    staging.mkdir()
    shutil.copytree(app, staging / app.name, symlinks=True, copy_function=copy_payload)
    run("/usr/bin/xattr", "-cr", staging / app.name)
    run("/usr/bin/codesign", "--verify", "--deep", "--strict", "--verbose=2", staging / app.name)
    (staging / "Applications").symlink_to("/Applications", target_is_directory=True)
    if (HERE / "README.md").exists():
        copy_payload(HERE / "README.md", staging / "READ ME.md")
    run("hdiutil", "create", "-volname", "Pakati", "-srcfolder", staging,
        "-fs", "HFS+", "-format", "UDZO", "-ov", DMG)
    run("hdiutil", "verify", DMG)
    print("Created " + str(DMG), flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine-only", action="store_true")
    parser.add_argument("--skip-engine", action="store_true")
    parser.add_argument("--skip-dmg", action="store_true")
    parser.add_argument("--keep-staging", action="store_true",
                        help="Keep the signed /private/tmp bundle for an isolated UI smoke test")
    args = parser.parse_args()
    if platform.system() != "Darwin" or platform.machine() != "arm64":
        raise RuntimeError("This build currently targets Apple Silicon Macs. Build on an arm64 Mac.")
    WORK.mkdir(parents=True, exist_ok=True)
    python = (WORK / "venv" / "bin" / "python") if args.skip_engine else build_engine()
    if not args.engine_only:
        OUTPUTS.mkdir(parents=True, exist_ok=True)
        # Documents may be managed by macOS FileProvider, which can reinstate
        # FinderInfo immediately after xattr cleanup and invalidate signing.
        temporary_root = Path(tempfile.mkdtemp(prefix="agent-relay-build-", dir="/private/tmp"))
        try:
            app = build_app(python, temporary_root)
            archive_app(app)
            if not args.skip_dmg:
                build_dmg(app, temporary_root)
        finally:
            if args.keep_staging:
                print("Retained signing staging: " + str(temporary_root), flush=True)
            else:
                shutil.rmtree(temporary_root)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.SubprocessError) as error:
        print("Build failed: " + str(error), file=sys.stderr)
        raise SystemExit(1)
