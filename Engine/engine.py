#!/usr/bin/env python3
"""Pakati's bundled CLI; desktop UI and project hooks share this engine."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys

VERSION = "0.3.0"


def ensure_path() -> None:
    # Finder launches do not inherit the user's shell PATH.
    existing = [part for part in os.environ.get("PATH", "").split(os.pathsep) if part]
    os.environ["PATH"] = os.pathsep.join(dict.fromkeys([
        *existing, "/usr/bin", "/bin", "/usr/sbin", "/sbin",
        "/opt/homebrew/bin", "/usr/local/bin",
    ]))


def preflight() -> int:
    executable = shutil.which("git")
    if not executable:
        print("Pakati requires Git. Install Apple's Command Line Tools with xcode-select --install.", file=sys.stderr)
        return 2
    try:
        result = subprocess.run([executable, "--version"], capture_output=True,
                                text=True, timeout=15)
    except (OSError, subprocess.SubprocessError) as error:
        print(f"Git is unavailable: {error}", file=sys.stderr)
        return 2
    if result.returncode:
        print("Git is unavailable. Install Apple's Command Line Tools with xcode-select --install.\n" +
              (result.stderr.strip() or result.stdout.strip()), file=sys.stderr)
        return 2
    print(json.dumps({"version": VERSION, "git": executable,
                      "git_version": result.stdout.strip(),
                      "bundled_python": bool(getattr(sys, "frozen", False))}))
    return 0


def main() -> int:
    ensure_path()
    if len(sys.argv) < 2 or sys.argv[1] in ("--help", "-h"):
        print("Pakati " + VERSION + "\n\nCommands: setup, checkpoint, show, restore, hook, preflight, auto\n"
              "Use COMMAND --help for options. Project hooks use the installed stable engine path.")
        return 0
    command = sys.argv.pop(1)
    if command in ("--version", "version"):
        print(VERSION)
        return 0
    if command == "preflight":
        return preflight()
    if command == "setup":
        import setup_relay
        return setup_relay.main()
    if command == "hook":
        import hook
        return hook.main()
    if command == "auto":
        import autopilot
        return autopilot.main()
    if command in ("checkpoint", "show", "restore"):
        import relay
        sys.argv.insert(1, command)
        return relay.main()
    print(f"Unknown command: {command}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
