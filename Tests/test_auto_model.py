#!/usr/bin/env python3
"""Exercise native managed-run invariants through Swift's interpreter/JIT.

Creates only disposable fixtures under --scratch. It does not build an app,
helper, executable, or installer, start an agent, or install project hooks.
Use --typecheck-only if the local Swift interpreter cannot run this harness.
"""
from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import tempfile


HARNESS = r'''
extension ManagedEngineProcess {
    func feedForChecks(_ json: [String: Any]) throws {
        consume(try JSONSerialization.data(withJSONObject: json))
    }
}

extension RelayModel {
    func ownForChecks(_ task: RelayTask, _ generation: UUID) {
        workerGenerations[task.id] = generation
        managedProcesses[task.id] = ManagedEngineProcess(onState: { _ in }, onFinish: { _ in })
    }
    func dropOwnershipForChecks(_ task: RelayTask) {
        managedProcesses.removeValue(forKey: task.id)
        workerGenerations.removeValue(forKey: task.id)
    }
    func receiveForChecks(_ state: ManagedRunState, _ task: RelayTask, _ generation: UUID) {
        receiveManagedState(state, task: task, generation: generation)
    }
    func finishForChecks(_ task: RelayTask, _ generation: UUID) async {
        await managedWorkerFinished(task, generation: generation, exitCode: 0)
    }
    func revisionForChecks(_ task: RelayTask) -> Int { activityRevisions[task.id, default: 0] }
    func pollForChecks(_ state: ManagedRunState, _ task: RelayTask, _ revision: Int) {
        receivePolledState(state, task: task, revision: revision)
    }
    func pendingStartForChecks(_ task: RelayTask) {
        startGenerations[task.id] = UUID()
        managedStates[task.id] = ManagedRunState(status: "starting")
    }
}

@MainActor private func check(_ condition: Bool, _ message: String) throws {
    if !condition { throw EngineError.message(message) }
}

@MainActor private func fixture(_ root: URL, _ name: String) throws -> (RelayModel, RelayTask, URL) {
    let area = root.appendingPathComponent(name, isDirectory: true)
    let project = area.appendingPathComponent("project", isDirectory: true)
    let noteDirectory = project.appendingPathComponent(".agent-relay", isDirectory: true)
    try FileManager.default.createDirectory(at: noteDirectory, withIntermediateDirectories: true)
    let notes = noteDirectory.appendingPathComponent("notes.md")
    try "# Goal\nSaved project progress.\n".write(to: notes, atomically: true, encoding: .utf8)
    let model = RelayModel(dataDirectory: area.appendingPathComponent("app-data"))
    let config: [String: Any] = ["task": name, "store": model.storeURL.path, "enabled": true]
    try JSONSerialization.data(withJSONObject: config).write(to: project.appendingPathComponent(".agent-relay.json"))
    try model.addTask(taskID: name, path: project.path)
    return (model, model.selectedTask!, notes)
}

@MainActor private func runChecks() async throws {
    let root = URL(fileURLWithPath: ProcessInfo.processInfo.environment["AUTO_MODEL_ROOT"]!, isDirectory: true)
    let fm = FileManager.default
    var count = 0

    let oldDirectory = root.appendingPathComponent("old-settings", isDirectory: true)
    try fm.createDirectory(at: oldDirectory, withIntermediateDirectories: true)
    try Data(#"{"schemaVersion":1,"tasks":[]}"#.utf8).write(to: oldDirectory.appendingPathComponent("settings.json"))
    let oldModel = RelayModel(dataDirectory: oldDirectory)
    try check(oldModel.codexCLIPath.isEmpty && oldModel.claudeCLIPath.isEmpty, "Old settings did not decode absent CLI preferences.")
    oldModel.codexCLIPath = "/fixture/codex"; oldModel.claudeCLIPath = "/fixture/claude"; oldModel.persist()
    let reopened = RelayModel(dataDirectory: oldDirectory)
    try check(reopened.codexCLIPath == "/fixture/codex" && reopened.claudeCLIPath == "/fixture/claude", "CLI path preferences did not survive persistence.")
    count += 1

    var seen: [ManagedRunState] = []
    let parser = ManagedEngineProcess(onState: { seen.append($0) }, onFinish: { _ in })
    try parser.feedForChecks(["type": "event", "status": "completed", "detail": "untrusted transcript"])
    try parser.feedForChecks(["type": "user", "status": "running", "detail": "untrusted transcript"])
    try parser.feedForChecks(["type": "state", "status": "not-a-state"])
    try parser.feedForChecks(["type": "state", "status": "running", "detail": String(repeating: "x", count: 3000), "project_path": "relative-folder"])
    try check(seen.count == 1 && seen[0].detail.count == 2048 && seen[0].projectPath == nil, "NDJSON parser accepted non-state data or unbounded details.")
    count += 1

    let (draftModel, draftTask, sourceNotes) = try fixture(root, "draft-transfer")
    let destination = sourceNotes.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent().appendingPathComponent("restored-project", isDirectory: true)
    try fm.createDirectory(at: destination.appendingPathComponent(".agent-relay"), withIntermediateDirectories: true)
    let destinationNotes = destination.appendingPathComponent(".agent-relay/notes.md")
    let newer = "# Goal\nNewer transferred agent progress.\n"
    try newer.write(to: destinationNotes, atomically: true, encoding: .utf8)
    let destinationConfig: [String: Any] = ["task": draftTask.taskID, "store": draftModel.storeURL.path, "enabled": true]
    try JSONSerialization.data(withJSONObject: destinationConfig).write(to: destination.appendingPathComponent(".agent-relay.json"))
    let baseline = draftTask.notesBaseline
    draftTask.notes = "# Goal\nUnsaved local editor draft.\n"
    draftModel.applyManagedState(ManagedRunState(status: "completed", projectPath: destination.path, agent: "claude"), to: draftTask)
    try check(draftTask.notes.contains("Unsaved local editor draft") && draftTask.notesBaseline == baseline && draftTask.assistant == .claude, "Transfer lost the local draft, baseline, or assistant update.")
    draftModel.startManagedRun(draftTask)
    try check(!draftModel.busy && draftModel.status?.title == "Save your notes and checkpoint first", "Start accepted an unsaved or stale draft.")
    try check(try String(contentsOf: destinationNotes, encoding: .utf8) == newer, "Start changed project notes before acquiring the managed writer lock.")
    count += 1

    let (freshModel, freshTask, freshNotes) = try fixture(root, "untouched-notes")
    try newer.write(to: freshNotes, atomically: true, encoding: .utf8)
    freshModel.applyManagedState(ManagedRunState(status: "completed"), to: freshTask)
    try check(freshTask.notes == newer && freshTask.notesBaseline == newer && freshModel.managedState(for: freshTask).title == "Agent response finished", "Untouched editor failed to refresh or claimed the task was complete.")
    count += 1

    let (guardModel, guardTask, guardNotes) = try fixture(root, "writer-guard")
    let alias = root.appendingPathComponent("writer-project-alias")
    try fm.createSymbolicLink(at: alias, withDestinationURL: URL(fileURLWithPath: guardTask.projectPath))
    let other = RelayTask(taskID: "other-record", projectPath: alias.path)
    guardModel.tasks.append(other)
    let owned = UUID(); guardModel.ownForChecks(guardTask, owned)
    guardModel.receiveForChecks(ManagedRunState(status: "running"), guardTask, owned)
    let before = try String(contentsOf: guardNotes, encoding: .utf8)
    guardModel.configure(guardTask); guardModel.saveCheckpoint(guardTask); guardModel.restore(guardTask); guardModel.chooseProject(for: guardTask)
    try check(guardModel.isManagedWriting(other) && !guardModel.busy, "A record referring to the same folder bypassed the writer guard.")
    try check(try String(contentsOf: guardNotes, encoding: .utf8) == before, "Manual actions changed notes while a managed writer owned the folder.")
    guardModel.receiveForChecks(ManagedRunState(status: "completed"), guardTask, owned)
    try check(guardModel.isManagedWriting(guardTask), "Terminal stream state unlocked the folder before its helper finished.")
    guardModel.dropOwnershipForChecks(guardTask)
    try check(!guardModel.isManagedWriting(guardTask), "Settled terminal state did not unlock its folder.")
    count += 1

    let (generationModel, generationTask, _) = try fixture(root, "worker-generations")
    let current = UUID(), stale = UUID()
    generationModel.ownForChecks(generationTask, current)
    generationModel.receiveForChecks(ManagedRunState(status: "running"), generationTask, current)
    let revision = generationModel.revisionForChecks(generationTask)
    generationModel.receiveForChecks(ManagedRunState(status: "completed", projectPath: "/wrong-old-folder"), generationTask, stale)
    await generationModel.finishForChecks(generationTask, stale)
    try check(generationModel.isManagedTaskActive(generationTask) && generationModel.managedState(for: generationTask).status == "running" && generationTask.projectPath != "/wrong-old-folder", "An old worker callback removed or overwrote its replacement.")
    generationModel.receiveForChecks(ManagedRunState(status: "checkpointing"), generationTask, current)
    generationModel.dropOwnershipForChecks(generationTask)
    generationModel.pollForChecks(ManagedRunState(status: "idle"), generationTask, revision)
    try check(generationModel.managedState(for: generationTask).status == "checkpointing", "A stale status poll overwrote a newer streamed state.")
    generationModel.applyManagedState(ManagedRunState(status: "stopped"), to: generationTask)
    count += 1

    let (cleanupModel, cleanupTask, _) = try fixture(root, "cleanup-guard")
    let pending = ManagedRunState(["status": "interrupted", "writer_active": true, "detail": "Supervisor exited."])!
    cleanupModel.applyManagedState(pending, to: cleanupTask)
    try check(cleanupModel.isManagedWriting(cleanupTask) && cleanupModel.hasManagedRuns && pending.title == "Waiting for previous agent to stop", "Interrupted status released a still-active writer.")
    cleanupModel.applyManagedState(ManagedRunState(["status": "interrupted", "writer_active": false])!, to: cleanupTask)
    try check(!cleanupModel.isManagedWriting(cleanupTask) && !cleanupModel.hasManagedRuns && !cleanupModel.busy, "Confirmed cleanup failed to unlock the folder or restarted a worker.")
    count += 1

    let (cancelModel, cancelTask, _) = try fixture(root, "cancel-start")
    cancelModel.pendingStartForChecks(cancelTask)
    cancelModel.stopManagedRun(cancelTask)
    try check(!cancelModel.hasManagedRuns && cancelModel.managedState(for: cancelTask).status == "stopped", "Stop failed to cancel a start before an agent was launched.")
    count += 1
    print("Managed UI model checks passed: \(count) groups (executed through Swift interpreter/JIT).")
}

Task { @MainActor in
    do { try await runChecks(); exit(0) }
    catch { fputs("Managed UI model check failed: \(error.localizedDescription)\n", stderr); exit(1) }
}
RunLoop.main.run()
'''


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--scratch', required=True, type=Path)
    parser.add_argument('--typecheck-only', action='store_true')
    args = parser.parse_args()
    scratch = args.scratch.expanduser().resolve()
    scratch.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().parents[1] / 'Sources/AgentRelayApp.swift'
    original = source.read_text()
    # Keep the actual model and process runner, excluding UI view definitions.
    # Loading the entire SwiftUI view graph through the JIT can require symbols
    # unavailable to the interpreter even though native source typechecks.
    marker = 'struct PakatiMark: View {'
    if original.count(marker) != 1:
        raise RuntimeError('Native model/view boundary changed; update this driver.')
    with tempfile.TemporaryDirectory(prefix='managed-model-', dir=scratch) as temporary:
        root = Path(temporary)
        script = root / 'ManagedModelChecks.swift'
        script.write_text(original.split(marker)[0] + HARNESS)
        cache = scratch / 'swift-module-cache'
        if args.typecheck_only:
            command = ['xcrun', 'swiftc', '-typecheck', '-module-cache-path', str(cache), str(script)]
        else:
            command = ['xcrun', 'swift', '-module-cache-path', str(cache), str(script)]
        import os
        env = os.environ.copy()
        env['AUTO_MODEL_ROOT'] = str(root / 'fixtures')
        result = subprocess.run(command, env=env, capture_output=True, text=True, timeout=120)
        if result.returncode:
            log = scratch / 'managed-model-diagnostic.log'
            log.write_text(result.stdout + result.stderr)
            print((result.stdout + result.stderr)[:4000])
            raise RuntimeError(f'Swift checks exited {result.returncode}; diagnostics: {log}')
        print(result.stdout, end='')
        if result.stderr:
            print(result.stderr[:4000])
        if args.typecheck_only:
            print('Managed UI model harness typechecked; runtime checks were not executed.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
