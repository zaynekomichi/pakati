#!/usr/bin/env python3
"""Compile and exercise Swift model note-conflict guards without opening an app.

Pass --scratch to keep generated Swift, binary, and disposable project state in
workspace work/. The test never starts another assistant or installs hooks.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import subprocess
import tempfile

HARNESS = r'''
@main struct NotesModelChecks {
    @MainActor static func main() throws {
        let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["NOTES_TEST_ROOT"]!, isDirectory: true)
        let project = root.appendingPathComponent("fixture-project", isDirectory: true)
        let notesFolder = project.appendingPathComponent(".agent-relay", isDirectory: true)
        try FileManager.default.createDirectory(at: notesFolder, withIntermediateDirectories: true)
        let notesURL = notesFolder.appendingPathComponent("notes.md")
        let original = "# Goal\nInitial agent progress.\n"
        try original.write(to: notesURL, atomically: true, encoding: .utf8)
        let firstModel = RelayModel()
        try FileManager.default.createDirectory(at: firstModel.dataDirectory, withIntermediateDirectories: true)
        let canonicalStore = ProcessInfo.processInfo.environment["CANONICAL_TEST_STORE"]!
        func writeConfiguration(store: String, task: String = "fixture", enabled: Bool = true) throws {
            let config: [String: Any] = ["task": task, "store": store, "enabled": enabled]
            try JSONSerialization.data(withJSONObject: config).write(to: project.appendingPathComponent(".agent-relay.json"))
        }
        // The packaged Python setup resolves /tmp and every symlink before emitting store.
        try writeConfiguration(store: canonicalStore)
        guard !FileManager.default.fileExists(atPath: firstModel.storeURL.path) else {
            throw EngineError.message("The initial configuration regression must run before the checkpoint store exists.")
        }
        try firstModel.addTask(taskID: "fixture", path: project.path)
        let firstTask = firstModel.selectedTask!
        guard firstTask.configured, firstModel.configurationMatches(firstTask) else {
            throw EngineError.message("The engine's canonical store path was not recognized as configured.")
        }
        let logicalRoot = root.path.replacingOccurrences(of: "/private/tmp/", with: "/tmp/")
        let nestedData = URL(fileURLWithPath: logicalRoot, isDirectory: true)
            .appendingPathComponent("missing-parent/app-data", isDirectory: true)
        let nestedModel = RelayModel(dataDirectory: nestedData)
        let nestedTask = RelayTask(taskID: "fixture", projectPath: project.path)
        let nestedCanonicalStore = root.resolvingSymlinksInPath()
            .appendingPathComponent("missing-parent/app-data/checkpoints", isDirectory: true).path
        try writeConfiguration(store: nestedCanonicalStore)
        guard !FileManager.default.fileExists(atPath: nestedData.path), nestedModel.configurationMatches(nestedTask) else {
            throw EngineError.message("Nested missing store parents were not canonicalized without creating them.")
        }
        try FileManager.default.createDirectory(at: firstModel.storeURL, withIntermediateDirectories: true)
        let storeAlias = root.appendingPathComponent("store-alias", isDirectory: true)
        try FileManager.default.createSymbolicLink(at: storeAlias, withDestinationURL: firstModel.storeURL)
        try writeConfiguration(store: storeAlias.path)
        firstModel.selectTask()
        guard firstTask.configured, firstModel.configurationMatches(firstTask) else {
            throw EngineError.message("A symlink alias of the same store was incorrectly rejected.")
        }
        for invalidStore in ["isolated-app-data/checkpoints", root.appendingPathComponent("other-store").path] {
            try writeConfiguration(store: invalidStore)
            guard !firstModel.configurationMatches(firstTask) else {
                throw EngineError.message("A relative or unrelated store was accepted as configured.")
            }
        }
        try writeConfiguration(store: canonicalStore, task: "other-task")
        guard !firstModel.configurationMatches(firstTask) else {
            throw EngineError.message("Canonical store comparison bypassed the task ID guard.")
        }
        try writeConfiguration(store: canonicalStore, enabled: false)
        guard !firstModel.configurationMatches(firstTask) else {
            throw EngineError.message("Canonical store comparison bypassed the enabled guard.")
        }
        try writeConfiguration(store: canonicalStore)
        firstModel.selectTask()
        guard firstTask.notes == original, firstTask.notesBaseline == original else {
            throw EngineError.message("Adding task did not load the project note baseline.")
        }
        firstTask.notes = "# Goal\nUnsaved editor draft.\n"
        let external = "# Goal\nNewer coding assistant progress.\n"
        try external.write(to: notesURL, atomically: true, encoding: .utf8)
        firstModel.saveCheckpoint(firstTask)
        guard firstModel.status?.isError == true, !firstModel.busy,
              try String(contentsOf: notesURL, encoding: .utf8) == external,
              firstTask.notes.contains("Unsaved editor draft") else {
            throw EngineError.message("External-edit conflict failed to preserve both draft and project notes.")
        }
        firstModel.persist()
        let resumedModel = RelayModel()
        let resumedTask = resumedModel.selectedTask!
        guard resumedTask.notes.contains("Unsaved editor draft"), resumedTask.notesBaseline == original else {
            throw EngineError.message("Relaunch lost the draft or changed its disk baseline.")
        }
        resumedModel.saveCheckpoint(resumedTask)
        guard resumedModel.status?.isError == true, !resumedModel.busy,
              try String(contentsOf: notesURL, encoding: .utf8) == external else {
            throw EngineError.message("Relaunch allowed stale draft to overwrite newer project notes.")
        }
        resumedModel.reloadProjectNotes(resumedTask)
        guard resumedTask.notes == external, resumedTask.notesBaseline == external else {
            throw EngineError.message("Explicit reload did not reset draft and baseline.")
        }
        resumedModel.persist()
        let newest = "# Goal\nMore work while the relay app was closed.\n"
        try newest.write(to: notesURL, atomically: true, encoding: .utf8)
        let refreshedModel = RelayModel()
        let refreshedTask = refreshedModel.selectedTask!
        guard refreshedTask.notes == newest, refreshedTask.notesBaseline == newest else {
            throw EngineError.message("Relaunch did not refresh an editor with no unsaved draft.")
        }
        try FileManager.default.removeItem(at: notesURL)
        let externalTarget = root.appendingPathComponent("external-preserve.md")
        try "outside target\n".write(to: externalTarget, atomically: true, encoding: .utf8)
        try FileManager.default.createSymbolicLink(at: notesURL, withDestinationURL: externalTarget)
        refreshedTask.notes = "# Goal\nAttempted editor change.\n"
        refreshedModel.saveCheckpoint(refreshedTask)
        guard refreshedModel.status?.isError == true, !refreshedModel.busy,
              try String(contentsOf: externalTarget, encoding: .utf8) == "outside target\n" else {
            throw EngineError.message("Symbolic-link notes guard failed to preserve external target.")
        }
        try FileManager.default.removeItem(at: notesURL)
        let privateNote = "# Goal\nPrivate local task note.\n"
        try privateNote.write(to: notesURL, atomically: true, encoding: .utf8)
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: notesURL.path)
        refreshedTask.notesBaseline = privateNote
        refreshedTask.notes = "# Goal\nExplicit user editor revision.\n"
        refreshedModel.saveCheckpoint(refreshedTask)
        let attributes = try FileManager.default.attributesOfItem(atPath: notesURL.path)
        guard refreshedModel.busy, attributes[.posixPermissions] as? NSNumber == NSNumber(value: 0o600),
              try String(contentsOf: notesURL, encoding: .utf8) == refreshedTask.notes else {
            throw EngineError.message("Explicit note save did not preserve existing private file permissions.")
        }
        print("Swift model checks passed: canonical store identity and configuration guards, external-edit conflict, draft relaunch, explicit reload, untouched-editor refresh, symlink refusal, note permission preservation.")
        // execute() queues the engine job on this actor. Exit before yielding;
        // only its synchronous explicit note-save phase is exercised here.
        exit(0)
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
        raise RuntimeError('Native app entry point marker changed; update the model test driver.')
    with tempfile.TemporaryDirectory(prefix='relay-model-', dir=scratch) as temporary:
        root = Path(temporary)
        swift = root / 'NotesModelChecks.swift'
        swift.write_text(original.split(marker)[0] + HARNESS)
        executable = root / 'NotesModelChecks'
        subprocess.run(['xcrun', 'swiftc', '-parse-as-library', '-target', 'arm64-apple-macosx13.0',
                        '-module-cache-path', str(scratch / 'swift-module-cache'),
                        str(swift), '-o', str(executable)], check=True)
        env = os.environ.copy()
        env['AGENT_RELAY_DATA_DIR'] = str(root / 'isolated-app-data')
        env['CANONICAL_TEST_STORE'] = str((root / 'isolated-app-data' / 'checkpoints').resolve())
        env['NOTES_TEST_ROOT'] = str(root)
        subprocess.run([str(executable)], env=env, check=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
