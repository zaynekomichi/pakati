#!/usr/bin/env python3
"""Test the native helper cache and simultaneous large output draining.

The fake test helper is a local shell script in a disposable workspace folder.
No coding assistants, network access, project settings, or user home are used.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

HARNESS = r'''
@main struct RuntimeRunnerChecks {
    static func main() throws {
        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["RUNTIME_TEST_ROOT"]!, isDirectory: true)
        let directory = root.appendingPathComponent("isolated-runtime", isDirectory: true)
        guard let bundled = Bundle.main.resourceURL?.appendingPathComponent("relay-engine") else {
            throw EngineError.message("Test helper resource is unavailable.")
        }
        let script = "#!/bin/sh\n/usr/bin/head -c 262144 /dev/zero\n/usr/bin/head -c 262144 /dev/zero >&2\n"
        try script.write(to: bundled, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: bundled.path)
        let stable = try EngineRunner.prepareRuntime(dataDirectory: directory)
        let originalInfo = try FileManager.default.attributesOfItem(atPath: stable.path)
        let originalInode = originalInfo[.systemFileNumber] as? NSNumber
        _ = try EngineRunner.prepareRuntime(dataDirectory: directory)
        let reusedInfo = try FileManager.default.attributesOfItem(atPath: stable.path)
        guard originalInode == reusedInfo[.systemFileNumber] as? NSNumber else {
            throw EngineError.message("Unchanged helper was replaced instead of reused.")
        }
        // A rebuilt app with the same version must repair/update an old cached helper.
        try "#!/bin/sh\necho stale\n".write(to: stable, atomically: true, encoding: .utf8)
        _ = try EngineRunner.prepareRuntime(dataDirectory: directory)
        guard try Data(contentsOf: stable) == Data(contentsOf: bundled) else {
            throw EngineError.message("Helper cache did not refresh changed bytes.")
        }
        let output = try EngineRunner.run(dataDirectory: directory, arguments: [])
        guard output.stdout.utf8.count == 262144, output.stderr.utf8.count == 262144 else {
            throw EngineError.message("Runner did not fully drain both large output streams.")
        }
        try FileManager.default.removeItem(at: stable)
        let external = root.appendingPathComponent("preserved-external-helper")
        try "do not overwrite\n".write(to: external, atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(at: stable, withDestinationURL: external)
        var rejected = false
        do { _ = try EngineRunner.prepareRuntime(dataDirectory: directory) }
        catch { rejected = true }
        guard rejected, try String(contentsOf: external, encoding: .utf8) == "do not overwrite\n" else {
            throw EngineError.message("Runtime symlink refusal did not preserve external target.")
        }
        print("Native runner checks passed: helper reuse, same-version hash refresh, 256 KiB stdout plus 256 KiB stderr, runtime symlink refusal.")
    }
}
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True, type=Path)
    args = parser.parse_args()
    scratch = args.scratch.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / 'Sources/AgentRelayApp.swift'
    original = source.read_text()
    marker = '@main struct AgentRelayApp: App {'
    if original.count(marker) != 1:
        raise RuntimeError('Native app entry point marker changed; update the test driver.')
    with tempfile.TemporaryDirectory(prefix='relay-runner-', dir=scratch) as temporary:
        root = Path(temporary)
        swift = root / 'RuntimeRunnerChecks.swift'
        swift.write_text(original.split(marker)[0] + HARNESS)
        executable = root / 'RuntimeRunnerChecks'
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-target', 'arm64-apple-macosx13.0',
                        '-module-cache-path', str(scratch / 'swift-module-cache'),
                        str(swift), '-o', str(executable)], check=True)
        env = os.environ.copy()
        env['AGENT_RELAY_DATA_DIR'] = str(root / 'isolated-app-data')
        env['RUNTIME_TEST_ROOT'] = str(root)
        subprocess.run([str(executable)], env=env, check=True, timeout=40)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
