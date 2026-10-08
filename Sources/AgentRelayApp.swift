import SwiftUI
import AppKit
import Foundation
import CryptoKit
import Darwin

private let relayVersion = "0.3.0"
private let initialNotes = """
# Task handoff

## Goal and constraints

Describe what the user wants and any requirements.

## Completed work

Record meaningful changes and the files involved.

## Decisions and reasons

Explain choices the next assistant needs to understand.

## Checks and results

Record commands or tests and their actual results.

## Next steps

List the next concrete actions.

## Blockers

Record any unresolved questions or required input.
"""

enum Assistant: String, Codable, CaseIterable, Identifiable {
    case codex, claude
    var id: String { rawValue }
    var title: String { self == .codex ? "Codex" : "Claude Code" }
    var bundleIdentifiers: [String] {
        self == .codex ? ["com.openai.codex", "com.openai.Codex"] : ["com.anthropic.claudefordesktop", "com.anthropic.Claude"]
    }
    var appName: String { self == .codex ? "Codex" : "Claude" }
}

final class RelayTask: ObservableObject, Identifiable, Codable {
    let id: UUID
    @Published var taskID: String
    @Published var projectPath: String
    @Published var assistant: Assistant
    @Published var notes: String
    @Published var configured: Bool
    var notesBaseline: String?
    var folderName: String { URL(fileURLWithPath: projectPath).lastPathComponent }

    init(taskID: String, projectPath: String, assistant: Assistant = .codex, notes: String = initialNotes) {
        id = UUID(); self.taskID = taskID; self.projectPath = projectPath
        self.assistant = assistant; self.notes = notes; configured = false
    }
    enum CodingKeys: String, CodingKey { case id, taskID, projectPath, assistant, notes, configured, notesBaseline }
    required init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(UUID.self, forKey: .id)
        taskID = try c.decode(String.self, forKey: .taskID)
        projectPath = try c.decode(String.self, forKey: .projectPath)
        assistant = try c.decode(Assistant.self, forKey: .assistant)
        notes = try c.decode(String.self, forKey: .notes)
        configured = try c.decode(Bool.self, forKey: .configured)
        notesBaseline = try c.decodeIfPresent(String.self, forKey: .notesBaseline)
    }
    func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        try c.encode(id, forKey: .id); try c.encode(taskID, forKey: .taskID)
        try c.encode(projectPath, forKey: .projectPath); try c.encode(assistant, forKey: .assistant)
        try c.encode(notes, forKey: .notes); try c.encode(configured, forKey: .configured)
        try c.encodeIfPresent(notesBaseline, forKey: .notesBaseline)
    }
}

struct SavedSettings: Codable {
    var schemaVersion = 1
    var selectedID: UUID?
    var tasks: [RelayTask]
    var codexCLIPath: String? = nil
    var claudeCLIPath: String? = nil
}

struct Checkpoint: Identifiable {
    let id: String
    let timestamp: String
    let agent: String
    let branch: String
    let sourcePath: String
    let head: String
    let exclusions: [String]
    var dateLabel: String {
        let fractional = ISO8601DateFormatter(); fractional.formatOptions = [.withInternetDateTime, .withFractionalSeconds]
        let simple = ISO8601DateFormatter()
        guard let date = fractional.date(from: timestamp) ?? simple.date(from: timestamp) else { return timestamp }
        return date.formatted(date: .abbreviated, time: .shortened)
    }
    init?(_ data: [String: Any]) {
        guard let id = data["version"] as? String, let timestamp = data["snapshot_at"] as? String,
              let agent = data["agent"] as? String, let project = data["project"] as? [String: Any],
              let path = project["path"] as? String, let head = project["head"] as? String else { return nil }
        self.id = id; self.timestamp = timestamp; self.agent = agent; sourcePath = path; self.head = head
        branch = project["branch"] as? String ?? "Detached HEAD"
        exclusions = data["excluded_untracked"] as? [String] ?? []
    }
}

struct RelayStatus {
    let title: String
    let detail: String
    var isError = false
}

struct EngineOutput {
    let stdout: String
    let stderr: String
    var json: [String: Any]? { (try? JSONSerialization.jsonObject(with: Data(stdout.utf8))) as? [String: Any] }
}

struct ManagedRunState: Sendable {
    let status: String
    var projectPath: String?
    var agent: String?
    var runID: String?
    var detail: String
    var switchCount: Int
    var pid: Int?
    var writerActive = false

    init(status: String, detail: String = "", projectPath: String? = nil, agent: String? = nil) {
        self.status = status; self.detail = detail; self.projectPath = projectPath; self.agent = agent
        switchCount = 0
    }
    init?(_ json: [String: Any]) {
        let accepted: Set<String> = ["idle", "running", "checkpointing", "switching", "completed", "blocked", "stopped", "failed", "interrupted"]
        guard let status = json["status"] as? String, accepted.contains(status) else { return nil }
        self.status = status
        detail = String((json["detail"] as? String ?? "").prefix(2048))
        if let path = json["project_path"] as? String, path.hasPrefix("/"), path.utf8.count < 8192 { projectPath = path }
        agent = json["agent"] as? String
        runID = (json["run_id"] as? String).map { String($0.prefix(128)) }
        switchCount = max(0, json["switch_count"] as? Int ?? 0)
        pid = json["pid"] as? Int
        writerActive = json["writer_active"] as? Bool == true || json["cleanup_pending"] as? Bool == true
    }
    var isActive: Bool { ["starting", "running", "checkpointing", "switching", "stopping"].contains(status) }
    var blocksWrites: Bool { isActive || writerActive || status == "checking" || status == "unknown" }
    var title: String {
        if writerActive, !isActive { return "Waiting for previous agent to stop" }
        switch status {
        case "starting": return "Starting agent"
        case "running": return "Agent is working"
        case "checkpointing": return "Saving progress"
        case "switching": return "Continuing with the other agent"
        case "completed": return "Agent response finished"
        case "blocked": return "Run needs attention"
        case "stopping": return "Stopping agent"
        case "stopped": return "Run stopped"
        case "failed": return "Run failed"
        case "interrupted": return "Run interrupted"
        case "checking": return "Checking run status"
        case "unknown": return "Run status unavailable"
        default: return "Ready to start"
        }
    }
}

final class ManagedEngineProcess {
    private let process = Process()
    private let stdout = Pipe()
    private let stderr = Pipe()
    private let readers = DispatchGroup()
    private let onState: (ManagedRunState) -> Void
    private let onFinish: (Int32) -> Void

    init(onState: @escaping (ManagedRunState) -> Void, onFinish: @escaping (Int32) -> Void) {
        self.onState = onState; self.onFinish = onFinish
    }
    func start(dataDirectory: URL, project: String, arguments: [String]) throws {
        try launch(executable: EngineRunner.prepareRuntime(dataDirectory: dataDirectory), project: project, arguments: arguments)
    }
    private func launch(executable: URL, project: String, arguments: [String]) throws {
        process.executableURL = executable
        process.arguments = arguments; process.environment = EngineRunner.environment()
        process.currentDirectoryURL = URL(fileURLWithPath: project, isDirectory: true)
        process.standardInput = FileHandle.nullDevice
        process.standardOutput = stdout; process.standardError = stderr
        try process.run()
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { self.readers.leave() }
            var line = Data(), oversized = false
            while let chunk = self.readChunk(from: self.stdout.fileHandleForReading) {
                for byte in chunk {
                    if byte == 10 {
                        if !oversized { self.consume(line) }
                        line.removeAll(keepingCapacity: true); oversized = false
                    } else if !oversized {
                        if line.count < 32768 { line.append(byte) }
                        else { line.removeAll(keepingCapacity: true); oversized = true }
                    }
                }
            }
            if !oversized, !line.isEmpty { self.consume(line) }
        }
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            defer { self.readers.leave() }
            // Agent transcripts and stderr never enter the UI or an app log.
            while self.readChunk(from: self.stderr.fileHandleForReading) != nil {}
        }
        DispatchQueue.global(qos: .utility).async {
            self.process.waitUntilExit(); self.readers.wait()
            self.onFinish(self.process.terminationStatus)
        }
    }
    private func readChunk(from handle: FileHandle) -> Data? {
        var bytes = [UInt8](repeating: 0, count: 4096)
        while true {
            // One POSIX read returns a short flushed pipe write immediately. Foundation's
            // read(upToCount:) can wait for the requested size or EOF on this macOS.
            let count = bytes.withUnsafeMutableBytes { Darwin.read(handle.fileDescriptor, $0.baseAddress, $0.count) }
            if count > 0 { return Data(bytes.prefix(count)) }
            if count == 0 || errno != EINTR { return nil }
        }
    }
    private func consume(_ line: Data) {
        guard let json = (try? JSONSerialization.jsonObject(with: line)) as? [String: Any],
              json["type"] as? String == "state", let state = ManagedRunState(json) else { return }
        onState(state)
    }
}

enum EngineError: LocalizedError {
    case message(String)
    var errorDescription: String? { switch self { case .message(let text): return text } }
}

enum EngineRunner {
    static func environment() -> [String: String] {
        var environment = ProcessInfo.processInfo.environment
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin:/usr/local/bin:\(home)/.local/bin:\(home)/.npm-global/bin"
        return environment
    }
    static func dataDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["AGENT_RELAY_DATA_DIR"], !override.isEmpty {
            return canonicalDirectoryURL(URL(fileURLWithPath: override, isDirectory: true))
        }
        return canonicalDirectoryURL(FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Relay", isDirectory: true))
    }
    static func canonicalDirectoryURL(_ url: URL) -> URL {
        var ancestor = url.standardizedFileURL
        var missingComponents: [String] = []
        // Resolve an existing ancestor before appending missing folders. Foundation
        // normalizes even resolved /private/tmp paths back to /tmp; POSIX realpath
        // preserves the physical path required by the engine's symlink checks.
        while !FileManager.default.fileExists(atPath: ancestor.path) {
            let parent = ancestor.deletingLastPathComponent()
            guard parent.path != ancestor.path else { break }
            missingComponents.append(ancestor.lastPathComponent)
            ancestor = parent
        }
        var resolvedAncestor = ancestor.path
        if let physicalPath = realpath(ancestor.path, nil) {
            resolvedAncestor = String(cString: physicalPath)
            free(physicalPath)
        }
        let path = missingComponents.reversed().reduce(resolvedAncestor) {
            ($0 as NSString).appendingPathComponent($1)
        }
        return URL(fileURLWithPath: path, isDirectory: true)
    }
    static func prepareRuntime(dataDirectory: URL) throws -> URL {
        guard let bundled = Bundle.main.resourceURL?.appendingPathComponent("relay-engine"),
              FileManager.default.isExecutableFile(atPath: bundled.path) else {
            throw EngineError.message("The app's relay engine is missing. Reinstall the complete Pakati.app bundle.")
        }
        let folder = dataDirectory.appendingPathComponent("runtime/v\(relayVersion)", isDirectory: true)
        let stable = folder.appendingPathComponent("relay-engine")
        for path in [dataDirectory, dataDirectory.appendingPathComponent("runtime"), folder, stable] {
            if (try? FileManager.default.destinationOfSymbolicLink(atPath: path.path)) != nil {
                throw EngineError.message("The runtime path cannot be a symbolic link: \(path.path)")
            }
        }
        try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
        // A stable path keeps hooks working after the app is moved or a disk image is ejected.
        let bundledHash = SHA256.hash(data: try Data(contentsOf: bundled, options: .mappedIfSafe))
        let existingHash = (try? Data(contentsOf: stable, options: .mappedIfSafe)).map { SHA256.hash(data: $0) }
        if existingHash != bundledHash {
            let temporary = folder.appendingPathComponent(".relay-engine-\(UUID().uuidString)")
            defer { try? FileManager.default.removeItem(at: temporary) }
            try FileManager.default.copyItem(at: bundled, to: temporary)
            try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: temporary.path)
            // Atomic replacement permits a rebuilt app to refresh the helper without a gap for hooks.
            let result = temporary.path.withCString { source in stable.path.withCString { destination in rename(source, destination) } }
            guard result == 0 else { throw EngineError.message("Could not install the runtime helper: \(String(cString: strerror(errno)))") }
        }
        guard FileManager.default.isExecutableFile(atPath: stable.path) else {
            throw EngineError.message("The installed relay engine is not executable: \(stable.path)")
        }
        return stable
    }
    static func run(dataDirectory: URL, arguments: [String], allowFailure: Bool = false) throws -> EngineOutput {
        let executable = try prepareRuntime(dataDirectory: dataDirectory)
        let process = Process(); process.executableURL = executable; process.arguments = arguments
        let outputPipe = Pipe(), errorPipe = Pipe()
        process.standardOutput = outputPipe; process.standardError = errorPipe
        process.environment = environment()
        try process.run()
        // Read both streams concurrently so large task notes cannot fill one pipe and block the engine.
        let readers = DispatchGroup(), lock = NSLock()
        var stdout = Data(), stderr = Data()
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            let bytes = outputPipe.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); stdout = bytes; lock.unlock(); readers.leave()
        }
        readers.enter()
        DispatchQueue.global(qos: .userInitiated).async {
            let bytes = errorPipe.fileHandleForReading.readDataToEndOfFile()
            lock.lock(); stderr = bytes; lock.unlock(); readers.leave()
        }
        process.waitUntilExit(); readers.wait()
        let output = EngineOutput(stdout: String(decoding: stdout, as: UTF8.self), stderr: String(decoding: stderr, as: UTF8.self))
        guard process.terminationStatus == 0 || allowFailure else {
            let detail = output.stderr.trimmingCharacters(in: .whitespacesAndNewlines)
            throw EngineError.message(detail.isEmpty ? output.stdout : detail)
        }
        return output
    }
}

@MainActor final class RelayModel: ObservableObject {
    @Published var tasks: [RelayTask] = []
    @Published var selectedID: UUID?
    @Published var busy = false
    @Published var operation = ""
    @Published var status: RelayStatus?
    @Published var history: [Checkpoint] = []
    @Published var selectedVersion: String?
    @Published var savedNotes = ""
    @Published var showingAdd = false
    @Published var restoredPath: String?
    @Published var codexCLIPath = ""
    @Published var claudeCLIPath = ""
    @Published var managedStates: [UUID: ManagedRunState] = [:]
    @Published var stoppingTasks: Set<UUID> = []
    let dataDirectory: URL
    private var settingsRecoveryNeeded = false
    private var managedProcesses: [UUID: ManagedEngineProcess] = [:]
    private var startGenerations: [UUID: UUID] = [:]
    private var workerGenerations: [UUID: UUID] = [:]
    private var activityRevisions: [UUID: Int] = [:]
    private var statusChecks: Set<UUID> = []
    private var observedTasks: Set<UUID> = []
    private var quitting = false
    // Keep physical directory strings through CLI argument construction.
    var storePath: String { (dataDirectory.path as NSString).appendingPathComponent("checkpoints") }
    var handoffRootPath: String { (dataDirectory.path as NSString).appendingPathComponent("handoffs") }
    var storeURL: URL { URL(fileURLWithPath: storePath, isDirectory: true) }
    var selectedTask: RelayTask? { tasks.first { $0.id == selectedID } }
    var checkpoint: Checkpoint? { history.first { $0.id == selectedVersion } ?? history.first }

    init(dataDirectory: URL = EngineRunner.dataDirectory()) {
        self.dataDirectory = EngineRunner.canonicalDirectoryURL(dataDirectory)
        let settings = self.dataDirectory.appendingPathComponent("settings.json")
        if FileManager.default.fileExists(atPath: settings.path) {
            do {
                let saved = try JSONDecoder().decode(SavedSettings.self, from: Data(contentsOf: settings))
                guard saved.schemaVersion == 1 else { throw EngineError.message("This settings file uses an unsupported version.") }
                tasks = saved.tasks
                codexCLIPath = saved.codexCLIPath ?? ""
                claudeCLIPath = saved.claudeCLIPath ?? ""
                selectedID = tasks.contains { $0.id == saved.selectedID } ? saved.selectedID : tasks.first?.id
                for task in tasks {
                    task.configured = configurationMatches(task)
                    let diskNotes = readProjectNotes(task)
                    // Refresh an untouched editor, while preserving a user's unsaved draft.
                    if let baseline = task.notesBaseline, task.notes == baseline, let diskNotes = diskNotes {
                        task.notes = diskNotes; task.notesBaseline = diskNotes
                    } else if task.notesBaseline == nil, let diskNotes = diskNotes, task.notes == diskNotes {
                        task.notesBaseline = diskNotes
                    }
                }
            } catch {
                settingsRecoveryNeeded = true
                status = RelayStatus(title: "Settings could not be loaded", detail: error.localizedDescription + " The existing file will be backed up before new settings are saved.", isError: true)
            }
        }
        refreshHistory()
    }
    func persist() {
        do {
            try FileManager.default.createDirectory(at: dataDirectory, withIntermediateDirectories: true)
            if settingsRecoveryNeeded {
                let original = dataDirectory.appendingPathComponent("settings.json")
                if FileManager.default.fileExists(atPath: original.path) {
                    try FileManager.default.copyItem(at: original, to: dataDirectory.appendingPathComponent("settings-unreadable-\(UUID().uuidString).json"))
                }
                settingsRecoveryNeeded = false
            }
            let encoder = JSONEncoder(); encoder.outputFormatting = [.prettyPrinted, .sortedKeys]
            try encoder.encode(SavedSettings(selectedID: selectedID, tasks: tasks,
                    codexCLIPath: codexCLIPath.isEmpty ? nil : codexCLIPath,
                    claudeCLIPath: claudeCLIPath.isEmpty ? nil : claudeCLIPath))
                .write(to: dataDirectory.appendingPathComponent("settings.json"), options: .atomic)
        } catch { status = RelayStatus(title: "Settings could not be saved", detail: error.localizedDescription, isError: true) }
    }
    func configurationMatches(_ task: RelayTask) -> Bool {
        let path = URL(fileURLWithPath: task.projectPath).appendingPathComponent(".agent-relay.json")
        guard let data = try? Data(contentsOf: path),
              let value = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any],
              let storedStore = value["store"] as? String, storedStore.hasPrefix("/") else { return false }
        let configuredStore = canonicalStorePath(URL(fileURLWithPath: storedStore, isDirectory: true))
        let expectedStore = canonicalStorePath(storeURL)
        return value["task"] as? String == task.taskID && configuredStore == expectedStore && value["enabled"] as? Bool == true
    }
    private func canonicalStorePath(_ url: URL) -> String {
        EngineRunner.canonicalDirectoryURL(url).path
    }
    func selectTask() {
        status = nil; restoredPath = nil; selectedVersion = nil; savedNotes = ""
        if let task = selectedTask {
            task.configured = configurationMatches(task)
            if let baseline = task.notesBaseline, task.notes == baseline, let diskNotes = readProjectNotes(task) {
                task.notes = diskNotes; task.notesBaseline = diskNotes
            }
        }
        refreshHistory(); persist()
    }
    func addTask(taskID: String, path: String) throws {
        let name = taskID.trimmingCharacters(in: .whitespacesAndNewlines)
        guard name.range(of: "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", options: .regularExpression) != nil else {
            throw EngineError.message("Use 1–64 letters, numbers, underscores, or hyphens. Start with a letter or number.")
        }
        guard !tasks.contains(where: { $0.taskID.lowercased() == name.lowercased() }),
              !FileManager.default.fileExists(atPath: storeURL.appendingPathComponent("tasks/\(name)").path) else {
            throw EngineError.message("That task ID already has a handoff. Select the existing task or use a new ID.")
        }
        guard !path.isEmpty, FileManager.default.fileExists(atPath: path) else { throw EngineError.message("Choose the project's Git folder first.") }
        let task = RelayTask(taskID: name, projectPath: URL(fileURLWithPath: path).standardizedFileURL.path)
        task.notesBaseline = readProjectNotes(task)
        task.notes = task.notesBaseline ?? initialNotes
        task.configured = configurationMatches(task)
        tasks.append(task); selectedID = task.id; showingAdd = false; selectTask()
    }
    func readProjectNotes(_ task: RelayTask) -> String? {
        let url = URL(fileURLWithPath: task.projectPath).appendingPathComponent(".agent-relay/notes.md")
        guard let text = try? String(contentsOf: url, encoding: .utf8), !text.contains("agent-relay-template") else { return nil }
        return text
    }
    func chooseProject(for task: RelayTask) {
        guard !isManagedWriting(task) else { showManagedWriteConflict(); return }
        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
        panel.allowsMultipleSelection = false; panel.prompt = "Use folder"; panel.title = "Choose a Git project or worktree"
        panel.directoryURL = URL(fileURLWithPath: task.projectPath)
        if panel.runModal() == .OK, let url = panel.url {
            task.projectPath = url.standardizedFileURL.path
            task.configured = configurationMatches(task)
            task.notesBaseline = readProjectNotes(task)
            if let notes = task.notesBaseline { task.notes = notes }
            restoredPath = nil; persist()
        }
    }
    func reloadProjectNotes(_ task: RelayTask) {
        guard let notes = readProjectNotes(task) else {
            task.notesBaseline = nil; persist()
            status = RelayStatus(title: "No completed project notes yet", detail: "Edit the task notes here, then save a checkpoint.")
            return
        }
        task.notes = notes; task.notesBaseline = notes; persist()
        status = RelayStatus(title: "Project notes loaded", detail: "Loaded .agent-relay/notes.md from the active folder.")
    }
    func refreshHistory() {
        guard let task = selectedTask else { history = []; return }
        let versions = storeURL.appendingPathComponent("tasks/\(task.taskID)/versions", isDirectory: true)
        let urls = (try? FileManager.default.contentsOfDirectory(at: versions, includingPropertiesForKeys: nil)) ?? []
        history = urls.compactMap { directory in
            guard let data = try? Data(contentsOf: directory.appendingPathComponent("metadata.json")),
                  let json = (try? JSONSerialization.jsonObject(with: data)) as? [String: Any] else { return nil }
            return Checkpoint(json)
        }.sorted { $0.timestamp > $1.timestamp }
        if !history.contains(where: { $0.id == selectedVersion }) { selectedVersion = history.first?.id }
    }
    private func execute(_ name: String, arguments: [String], allowFailure: Bool = false, success: @escaping (EngineOutput) throws -> Void) {
        guard !busy else { return }
        busy = true; operation = name; status = nil
        let directory = dataDirectory
        Task {
            do {
                let output = try await Task.detached(priority: .userInitiated) {
                    try EngineRunner.run(dataDirectory: directory, arguments: arguments, allowFailure: allowFailure)
                }.value
                try success(output)
            } catch { status = RelayStatus(title: "\(name) failed", detail: error.localizedDescription, isError: true) }
            busy = false; operation = ""; persist()
        }
    }
    func managedState(for task: RelayTask) -> ManagedRunState {
        managedStates[task.id] ?? ManagedRunState(status: "idle")
    }
    func isManagedTaskActive(_ task: RelayTask) -> Bool {
        managedState(for: task).isActive || managedState(for: task).writerActive || managedProcesses[task.id] != nil || startGenerations[task.id] != nil
    }
    func isManagedWriting(_ task: RelayTask) -> Bool {
        if managedState(for: task).blocksWrites || isManagedTaskActive(task) { return true }
        let folder = canonicalStorePath(URL(fileURLWithPath: task.projectPath, isDirectory: true))
        return tasks.contains { other in
            guard other.id != task.id, managedState(for: other).blocksWrites || isManagedTaskActive(other) else { return false }
            let path = managedState(for: other).projectPath ?? other.projectPath
            return canonicalStorePath(URL(fileURLWithPath: path, isDirectory: true)) == folder
        }
    }
    var hasManagedRuns: Bool {
        !managedProcesses.isEmpty || !startGenerations.isEmpty || managedStates.values.contains { $0.isActive || $0.writerActive }
    }
    private func showManagedWriteConflict() {
        status = RelayStatus(title: "This folder is in use", detail: "Stop the managed agent and wait for it to finish before changing this folder or saving task notes.", isError: true)
    }
    private func cliArguments() throws -> [String] {
        var arguments: [String] = []
        for (flag, value) in [("--codex-cli", codexCLIPath), ("--claude-cli", claudeCLIPath)] {
            let text = value.trimmingCharacters(in: .whitespacesAndNewlines)
            if !text.isEmpty {
                let path = (text as NSString).expandingTildeInPath
                guard path.hasPrefix("/"), FileManager.default.isExecutableFile(atPath: path) else {
                    throw EngineError.message("Choose an executable file for \(flag == "--codex-cli" ? "Codex" : "Claude Code"), or leave its path empty for automatic discovery.")
                }
                arguments += [flag, path]
            }
        }
        return arguments
    }
    func chooseCLI(_ assistant: Assistant) {
        let panel = NSOpenPanel(); panel.canChooseDirectories = false; panel.canChooseFiles = true
        panel.allowsMultipleSelection = false; panel.title = "Choose the \(assistant.title) command-line executable"
        panel.prompt = "Use executable"; panel.showsHiddenFiles = true
        guard panel.runModal() == .OK, let url = panel.url else { return }
        guard FileManager.default.isExecutableFile(atPath: url.path) else {
            status = RelayStatus(title: "Choose an executable file", detail: "The selected file cannot be executed.", isError: true); return
        }
        if assistant == .codex { codexCLIPath = url.path } else { claudeCLIPath = url.path }
        persist()
    }
    func checkCLIs() {
        do {
            let arguments = try cliArguments()
            execute("Check command-line agents", arguments: ["auto", "preflight"] + arguments, allowFailure: true) { output in
                guard let json = output.json, json["ready"] as? Bool == true else {
                    throw EngineError.message(self.preflightDetail(output.json))
                }
                self.status = RelayStatus(title: "Command-line agents are available", detail: String((json["detail"] as? String ?? "Codex and Claude Code are ready for a managed run.").prefix(1024)))
            }
        } catch { status = RelayStatus(title: "Command-line agents need setup", detail: error.localizedDescription, isError: true) }
    }
    private func preflightDetail(_ json: [String: Any]?) -> String {
        guard let json = json else { return "The command-line agents could not be checked. Install and sign in to Codex and Claude Code, then check again." }
        var details: [String] = []
        if let agents = json["agents"] as? [String: Any] {
            for assistant in Assistant.allCases {
                if let agent = agents[assistant.rawValue] as? [String: Any], agent["available"] as? Bool != true {
                    details.append("\(assistant.title): \(String((agent["detail"] as? String ?? "executable not found or unsupported").prefix(256)))")
                }
            }
        }
        if details.isEmpty { details.append(String((json["detail"] as? String ?? "Both command-line agents must be available before starting.").prefix(512))) }
        return details.joined(separator: "\n")
    }
    func applyManagedState(_ state: ManagedRunState, to task: RelayTask) {
        let untouched = task.notesBaseline.map { task.notes == $0 } ?? (task.notes == initialNotes)
        managedStates[task.id] = state
        activityRevisions[task.id, default: 0] += 1
        if let path = state.projectPath { task.projectPath = path }
        if let agent = state.agent, let assistant = Assistant(rawValue: agent) { task.assistant = assistant }
        task.configured = configurationMatches(task)
        if untouched, let notes = readProjectNotes(task) { task.notes = notes; task.notesBaseline = notes }
        if selectedID == task.id { refreshHistory() }
        persist()
        observeExistingWorker(task)
    }
    private func observeExistingWorker(_ task: RelayTask) {
        let state = managedState(for: task)
        guard managedProcesses[task.id] == nil, startGenerations[task.id] == nil,
              state.isActive || state.writerActive, !observedTasks.contains(task.id) else { return }
        observedTasks.insert(task.id)
        Task {
            defer { self.observedTasks.remove(task.id) }
            while self.managedProcesses[task.id] == nil && self.startGenerations[task.id] == nil && self.isManagedTaskActive(task) {
                try? await Task.sleep(nanoseconds: 1_000_000_000)
                await self.reloadManagedStatus(task, showChecking: false)
            }
        }
    }
    private func receiveManagedState(_ state: ManagedRunState, task: RelayTask, generation: UUID) {
        guard workerGenerations[task.id] == generation else { return }
        applyManagedState(state, to: task)
    }
    private func receivePolledState(_ state: ManagedRunState, task: RelayTask, revision: Int) {
        guard activityRevisions[task.id, default: 0] == revision,
              managedProcesses[task.id] == nil, startGenerations[task.id] == nil else { return }
        applyManagedState(state, to: task)
    }
    private func managedWorkerFinished(_ task: RelayTask, generation: UUID, exitCode: Int32) async {
        guard workerGenerations[task.id] == generation else { return }
        managedProcesses.removeValue(forKey: task.id)
        workerGenerations.removeValue(forKey: task.id)
        startGenerations.removeValue(forKey: task.id)
        if exitCode != 0, managedState(for: task).isActive {
            managedStates[task.id] = ManagedRunState(status: "stopping", detail: "The worker exited unexpectedly. Checking that its agent stopped before this folder can be edited.", projectPath: task.projectPath, agent: task.assistant.rawValue)
        }
        await reloadManagedStatus(task, showChecking: false)
    }
    func reloadManagedStatus(_ task: RelayTask, showChecking: Bool = true) async {
        guard managedProcesses[task.id] == nil else { return }
        guard !statusChecks.contains(task.id) else { return }
        statusChecks.insert(task.id); defer { statusChecks.remove(task.id) }
        let revision = activityRevisions[task.id, default: 0]
        if showChecking, !isManagedTaskActive(task) { managedStates[task.id] = ManagedRunState(status: "checking") }
        let directory = dataDirectory
        let arguments = ["auto", "status", "--task", task.taskID, "--store", storePath]
        do {
            let output = try await Task.detached(priority: .utility) { try EngineRunner.run(dataDirectory: directory, arguments: arguments) }.value
            guard let json = output.json, let state = ManagedRunState(json) else { throw EngineError.message("The engine returned an invalid managed-run status.") }
            receivePolledState(state, task: task, revision: revision)
        } catch {
            if activityRevisions[task.id, default: 0] == revision, managedProcesses[task.id] == nil,
               startGenerations[task.id] == nil, !isManagedTaskActive(task) {
                managedStates[task.id] = ManagedRunState(status: "unknown", detail: "The managed-run status could not be read. Check the app installation and refresh the status before editing this folder.")
            }
        }
    }
    func restoreManagedStatuses() async {
        // Relaunch observes saved workers; it never starts or resumes an agent.
        for task in tasks { await reloadManagedStatus(task) }
    }
    private func requireSavedNotes(_ task: RelayTask) throws {
        guard let baseline = task.notesBaseline, let diskNotes = readProjectNotes(task),
              diskNotes == baseline, task.notes == baseline else {
            throw EngineError.message("Save your notes and checkpoint first. If the project notes changed in another agent, copy any unsaved draft you need, load the current project notes, and merge your changes before saving. Your draft and project notes have been preserved.")
        }
    }
    private func managedRunArguments(_ task: RelayTask, generation: UUID, optionalCLIArguments: [String]) -> [String] {
        // A restored checkout can live below an earlier run's handoff root. Give each
        // new run a sibling destination so the engine's containment guards remain valid.
        let runRoot = (handoffRootPath as NSString).appendingPathComponent("run-\(generation.uuidString.lowercased())")
        return ["auto", "run", "--task", task.taskID, "--store", storePath,
            "--project", task.projectPath, "--agent", task.assistant.rawValue,
            "--handoff-root", runRoot, "--max-switches", "1"] + optionalCLIArguments
    }
    func startManagedRun(_ task: RelayTask) {
        guard !busy, !isManagedWriting(task), !quitting else { return }
        guard configurationMatches(task) else {
            task.configured = false
            status = RelayStatus(title: "Configure this folder first", detail: "Automatic handoff requires a configured project with real task notes.", isError: true); return
        }
        let notes = task.notes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !notes.isEmpty, notes != initialNotes, !notes.contains("agent-relay-template") else {
            status = RelayStatus(title: "Add real task notes first", detail: "Record the goal and next steps before starting a managed agent.", isError: true); return
        }
        do { try requireSavedNotes(task) }
        catch {
            status = RelayStatus(title: "Save your notes and checkpoint first", detail: error.localizedDescription, isError: true); return
        }
        do {
            let optionalCLIArguments = try cliArguments()
            let generation = UUID(); startGenerations[task.id] = generation
            activityRevisions[task.id, default: 0] += 1
            managedStates[task.id] = ManagedRunState(status: "starting", detail: "Checking the installed command-line agents.", projectPath: task.projectPath, agent: task.assistant.rawValue)
            busy = true; operation = "Prepare automatic handoff"; status = nil
            let directory = dataDirectory
            let statusArguments = ["auto", "status", "--task", task.taskID, "--store", storePath]
            Task {
                do {
                    let ready = try await Task.detached(priority: .userInitiated) {
                        try EngineRunner.run(dataDirectory: directory, arguments: ["auto", "preflight"] + optionalCLIArguments, allowFailure: true)
                    }.value
                    guard ready.json?["ready"] as? Bool == true else { throw EngineError.message(self.preflightDetail(ready.json)) }
                    let previous = try await Task.detached(priority: .userInitiated) {
                        try EngineRunner.run(dataDirectory: directory, arguments: statusArguments)
                    }.value
                    guard let json = previous.json, let previousState = ManagedRunState(json) else { throw EngineError.message("The existing managed-run status could not be verified.") }
                    if previousState.isActive || previousState.writerActive {
                        self.applyManagedState(previousState, to: task)
                        throw EngineError.message("A managed agent is already working on this task. Stop it before starting another run.")
                    }
                    guard self.startGenerations[task.id] == generation, !self.quitting else {
                        self.busy = false; self.operation = ""; return
                    }
                    try self.requireSavedNotes(task)
                    self.persist()
                    let arguments = self.managedRunArguments(task, generation: generation, optionalCLIArguments: optionalCLIArguments)
                    let runner = ManagedEngineProcess(onState: { [weak self, weak task] state in
                        Task { @MainActor in
                            guard let self = self, let task = task else { return }
                            self.receiveManagedState(state, task: task, generation: generation)
                        }
                    }, onFinish: { [weak self, weak task] code in
                        Task { @MainActor in
                            guard let self = self, let task = task else { return }
                            await self.managedWorkerFinished(task, generation: generation, exitCode: code)
                        }
                    })
                    self.workerGenerations[task.id] = generation
                    self.managedProcesses[task.id] = runner
                    do { try runner.start(dataDirectory: directory, project: task.projectPath, arguments: arguments) }
                    catch { self.managedProcesses.removeValue(forKey: task.id); self.workerGenerations.removeValue(forKey: task.id); throw error }
                    self.startGenerations.removeValue(forKey: task.id)
                } catch {
                    self.startGenerations.removeValue(forKey: task.id)
                    if !self.managedState(for: task).isActive || self.managedState(for: task).status == "starting" {
                        self.managedStates[task.id] = ManagedRunState(status: "failed", detail: String(error.localizedDescription.prefix(1024)), projectPath: task.projectPath, agent: task.assistant.rawValue)
                    }
                    self.status = RelayStatus(title: "Automatic handoff could not start", detail: String(error.localizedDescription.prefix(1024)), isError: true)
                }
                self.busy = false; self.operation = ""; self.persist()
            }
        } catch { status = RelayStatus(title: "Command-line agents need setup", detail: error.localizedDescription, isError: true) }
    }
    func stopManagedRun(_ task: RelayTask) {
        guard !stoppingTasks.contains(task.id) else { return }
        let wasStarting = startGenerations.removeValue(forKey: task.id) != nil
        activityRevisions[task.id, default: 0] += 1
        if wasStarting, managedProcesses[task.id] == nil, managedState(for: task).status == "starting" {
            managedStates[task.id] = ManagedRunState(status: "stopped", detail: "The run was cancelled before an agent started."); return
        }
        guard isManagedTaskActive(task) || managedState(for: task).status == "unknown" else { return }
        stoppingTasks.insert(task.id)
        managedStates[task.id] = ManagedRunState(status: "stopping", detail: "Waiting for the worker to stop its agent and preserve the final checkpoint.", projectPath: task.projectPath, agent: task.assistant.rawValue)
        let directory = dataDirectory
        let arguments = ["auto", "stop", "--task", task.taskID, "--store", storePath]
        Task {
            defer { self.stoppingTasks.remove(task.id) }
            do {
                _ = try await Task.detached(priority: .userInitiated) { try EngineRunner.run(dataDirectory: directory, arguments: arguments) }.value
                for _ in 0..<30 {
                    await self.reloadManagedStatus(task, showChecking: false)
                    if !self.isManagedTaskActive(task) { return }
                    try await Task.sleep(nanoseconds: 1_000_000_000)
                }
                self.status = RelayStatus(title: "The agent is still stopping", detail: "Keep Pakati open while the worker finishes. You can retry Stop if this status persists.", isError: true)
            } catch {
                self.status = RelayStatus(title: "Stop request could not be completed", detail: "The worker has not confirmed that its agent stopped. Keep Pakati open and refresh the run status before retrying.", isError: true)
            }
        }
    }
    func prepareToQuit(_ completion: @escaping (Bool) -> Void) {
        guard !quitting else { return }
        quitting = true
        for task in tasks where isManagedTaskActive(task) { stopManagedRun(task) }
        Task {
            for _ in 0..<35 {
                if !self.hasManagedRuns { self.quitting = false; completion(true); return }
                try? await Task.sleep(nanoseconds: 1_000_000_000)
            }
            self.quitting = false
            self.status = RelayStatus(title: "Pakati is waiting for the agent to stop", detail: "The worker has not finished cleaning up. Keep the app open, refresh its status, and retry Stop before quitting.", isError: true)
            completion(false)
        }
    }
    func configure(_ task: RelayTask) {
        guard !isManagedWriting(task) else { showManagedWriteConflict(); return }
        execute("Configure project", arguments: ["setup", "--project", task.projectPath, "--store", storePath, "--task", task.taskID]) { output in
            task.configured = self.configurationMatches(task)
            self.status = RelayStatus(title: "Project configured", detail: output.stdout + output.stderr)
        }
    }
    func saveCheckpoint(_ task: RelayTask) {
        guard !isManagedWriting(task) else { showManagedWriteConflict(); return }
        guard configurationMatches(task) else {
            task.configured = false
            status = RelayStatus(title: "Configure this folder first", detail: "This folder's relay configuration does not match the selected task and shared store.", isError: true)
            return
        }
        let notes = task.notes.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !notes.isEmpty, notes != initialNotes, !notes.contains("agent-relay-template") else {
            status = RelayStatus(title: "Add real task notes first", detail: "Replace the prompts with the goal, completed work, checks, and next steps before saving a checkpoint.", isError: true)
            return
        }
        do {
            let folder = URL(fileURLWithPath: task.projectPath).appendingPathComponent(".agent-relay", isDirectory: true)
            let notesURL = folder.appendingPathComponent("notes.md")
            let values = try? notesURL.resourceValues(forKeys: [.isSymbolicLinkKey])
            let folderValues = try? folder.resourceValues(forKeys: [.isSymbolicLinkKey])
            guard values?.isSymbolicLink != true, folderValues?.isSymbolicLink != true else {
                throw EngineError.message("The task notes path is a symbolic link. Use a regular project notes file.")
            }
            guard readProjectNotes(task) == task.notesBaseline else {
                throw EngineError.message("Project notes changed since this editor loaded them. The assistant may have updated them. Copy any unsaved draft you need, choose Load project notes, then merge your changes before saving. The project notes have been preserved.")
            }
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: true)
            try (notes + "\n").write(to: notesURL, atomically: true, encoding: .utf8)
            task.notesBaseline = notes + "\n"
            task.notes = notes + "\n"
            execute("Save checkpoint", arguments: ["checkpoint", "--project", task.projectPath, "--task", task.taskID,
                    "--store", storePath, "--agent", task.assistant.rawValue, "--notes", notesURL.path]) { output in
                guard let value = output.json, let version = value["version"] as? String else { throw EngineError.message("The engine did not return a checkpoint version.") }
                self.selectedVersion = version; self.refreshHistory(); self.savedNotes = notes
                let excluded = value["excluded_untracked"] as? [String] ?? []
                self.status = RelayStatus(title: "Checkpoint saved", detail: excluded.isEmpty
                    ? "Code and task notes are ready for a handoff."
                    : "Code and task notes saved. \(excluded.count) untracked file(s) were excluded; see Checkpoints for the list.")
            }
        } catch { status = RelayStatus(title: "Task notes could not be saved", detail: error.localizedDescription, isError: true) }
    }
    func loadCheckpoint(_ task: RelayTask) {
        refreshHistory()
        guard let version = selectedVersion else {
            status = RelayStatus(title: "No checkpoints yet", detail: "Configure the project, add task notes, and save the first checkpoint.")
            return
        }
        execute("Load checkpoint", arguments: ["show", "--task", task.taskID, "--store", storePath, "--version", version]) { output in
            guard let value = output.json, let notes = value["notes"] as? String else { throw EngineError.message("The engine did not return checkpoint notes.") }
            self.savedNotes = notes
            self.status = RelayStatus(title: "Checkpoint verified", detail: "The saved code and notes passed the engine's integrity checks.")
        }
    }
    func restore(_ task: RelayTask) {
        guard !isManagedWriting(task) else { showManagedWriteConflict(); return }
        guard let version = selectedVersion ?? history.first?.id else { return }
        let panel = NSSavePanel(); panel.title = "Restore into a new folder"; panel.prompt = "Restore here"
        panel.message = "Choose a new folder name. Pakati recreates this checkpoint there and leaves the active folder intact."
        panel.nameFieldStringValue = "\(task.folderName)-handoff"
        panel.canCreateDirectories = true
        panel.directoryURL = URL(fileURLWithPath: task.projectPath).deletingLastPathComponent()
        guard panel.runModal() == .OK, let destination = panel.url else { return }
        guard !FileManager.default.fileExists(atPath: destination.path) else {
            status = RelayStatus(title: "Choose a new folder name", detail: "The destination already exists. Restore requires an absent folder.", isError: true)
            return
        }
        execute("Restore handoff", arguments: ["restore", "--task", task.taskID, "--store", storePath, "--version", version, "--into", destination.path]) { output in
            guard let value = output.json, let project = value["project"] as? String else { throw EngineError.message("The engine did not return a restored project folder.") }
            task.projectPath = project; task.configured = self.configurationMatches(task)
            task.notesBaseline = self.readProjectNotes(task)
            if let notes = task.notesBaseline { task.notes = notes }
            else if let notesPath = value["notes_path"] as? String,
                    let notes = try? String(contentsOfFile: notesPath, encoding: .utf8) { task.notes = notes }
            else if !self.savedNotes.isEmpty { task.notes = self.savedNotes }
            self.restoredPath = project
            self.status = RelayStatus(title: "Handoff restored", detail: "The restored folder is now active. Configure it, then save a checkpoint before the next commit to adopt this folder. Open your assistant and paste the continuation prompt.")
        }
    }
    func copyPrompt(_ task: RelayTask) {
        let engine = dataDirectory.appendingPathComponent("runtime/v\(relayVersion)/relay-engine").path
        func quoted(_ value: String) -> String { "'" + value.replacingOccurrences(of: "'", with: "'\\''") + "'" }
        let prompt = """
        Continue the saved Pakati task \(task.taskID).

        Active project folder: \(task.projectPath)
        Shared checkpoint store: \(storeURL.path)

        Read this folder's project instructions and .agent-relay/notes.md. Verify the latest checkpoint by running:
        \(quoted(engine)) show --task \(quoted(task.taskID)) --store \(quoted(storeURL.path))

        Compare the saved checkpoint's source folder and HEAD with this checkout. If the saved code is newer or belongs to another folder, restore it into a new folder before continuing. Follow the documented adoption step after a restore: configure that folder and explicitly checkpoint it before the first commit.

        Continue the recorded next steps within the original goal and constraints. Update .agent-relay/notes.md after meaningful milestones, record actual checks and results, and save a checkpoint before stopping or switching assistants. Do not include secrets or private reasoning in notes.
        """
        NSPasteboard.general.clearContents(); NSPasteboard.general.setString(prompt, forType: .string)
        status = RelayStatus(title: "Continuation prompt copied", detail: "Open the active folder in your assistant, then paste the prompt into its chat.")
    }
    func openAssistant(_ assistant: Assistant) {
        var appURL: URL?
        for identifier in assistant.bundleIdentifiers {
            if let url = NSWorkspace.shared.urlForApplication(withBundleIdentifier: identifier) { appURL = url; break }
        }
        if appURL == nil {
            let candidates = [URL(fileURLWithPath: "/Applications/\(assistant.appName).app"),
                              FileManager.default.homeDirectoryForCurrentUser.appendingPathComponent("Applications/\(assistant.appName).app")]
            appURL = candidates.first { FileManager.default.fileExists(atPath: $0.path) }
        }
        guard let url = appURL else {
            status = RelayStatus(title: "\(assistant.title) was not found", detail: "Open it manually, choose the active project folder, and paste the continuation prompt.", isError: true)
            return
        }
        NSWorkspace.shared.openApplication(at: url, configuration: NSWorkspace.OpenConfiguration()) { _, error in
            if let error = error {
                Task { @MainActor in self.status = RelayStatus(title: "Could not open \(assistant.title)", detail: error.localizedDescription, isError: true) }
            }
        }
    }
}

struct PakatiMark: View {
    var body: some View {
        Canvas { context, size in
            let edge = min(size.width, size.height)
            let center = CGPoint(x: size.width / 2, y: size.height / 2)
            let field = CGRect(x: center.x - edge / 2, y: center.y - edge / 2, width: edge, height: edge)
            context.fill(Path(ellipseIn: field), with: .color(.white))
            var rays = Path()
            for index in 0..<16 {
                let angle = Double(index) * .pi / 8 - .pi / 2
                rays.move(to: CGPoint(x: center.x + cos(angle) * edge * 0.235,
                                      y: center.y + sin(angle) * edge * 0.235))
                rays.addLine(to: CGPoint(x: center.x + cos(angle) * edge * 0.395,
                                        y: center.y + sin(angle) * edge * 0.395))
            }
            context.stroke(rays, with: .color(.black), style: StrokeStyle(lineWidth: max(0.85, edge * 0.025), lineCap: .round))
            let dot = CGRect(x: center.x - edge * 0.10, y: center.y - edge * 0.10, width: edge * 0.20, height: edge * 0.20)
            context.fill(Path(ellipseIn: dot), with: .color(.black))
        }
        .aspectRatio(1, contentMode: .fit)
        .accessibilityLabel("Pakati")
    }
}

struct TaskRow: View {
    @ObservedObject var task: RelayTask
    var body: some View {
        HStack(spacing: 10) {
            Image(systemName: "arrow.triangle.branch").font(.system(size: 17)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 3) {
                Text(task.taskID).font(.system(size: 13, weight: .semibold))
                Text(task.folderName).font(.system(size: 11)).foregroundStyle(.secondary).lineLimit(1)
            }
        }.padding(.vertical, 7)
    }
}

struct Card<Content: View>: View {
    let title: String
    let symbol: String
    @ViewBuilder var content: Content
    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(title, systemImage: symbol).font(.system(size: 13, weight: .semibold))
            content
        }
        .padding(18).frame(maxWidth: .infinity, alignment: .leading)
        .background(Color(nsColor: .controlBackgroundColor))
        .clipShape(RoundedRectangle(cornerRadius: 12))
        .overlay(RoundedRectangle(cornerRadius: 12).stroke(Color.primary.opacity(0.07), lineWidth: 1))
    }
}

struct TaskDetail: View {
    @ObservedObject var model: RelayModel
    @ObservedObject var task: RelayTask
    @State private var tab = 0
    var body: some View {
        ScrollView(.vertical) {
            VStack(alignment: .leading, spacing: 0) {
                HStack(alignment: .top) {
                    VStack(alignment: .leading, spacing: 5) {
                        Text(task.taskID).font(.system(size: 27, weight: .bold))
                        Text("Keep the next assistant up to speed.").font(.system(size: 13)).foregroundStyle(.secondary)
                    }
                    Spacer()
                    Label(task.configured ? "Configured" : "Needs setup", systemImage: task.configured ? "checkmark.circle.fill" : "circle.dashed")
                        .font(.system(size: 11, weight: .medium))
                        .foregroundStyle(task.configured ? Color.green : Color.secondary)
                        .padding(.horizontal, 10).padding(.vertical, 6)
                        .background((task.configured ? Color.green : Color.gray).opacity(0.08), in: Capsule())
                }.padding(.bottom, 22)
                if model.busy {
                    HStack(spacing: 10) { ProgressView().controlSize(.small); Text(model.operation + "…").font(.system(size: 12)) }
                        .padding(12).frame(maxWidth: .infinity, alignment: .leading)
                        .background(Color.accentColor.opacity(0.08), in: RoundedRectangle(cornerRadius: 8)).padding(.bottom, 16)
                }
                if let status = model.status {
                    StatusView(status: status, dismiss: { model.status = nil }).padding(.bottom, 16)
                }
                Picker("View", selection: $tab) { Text("Task notes").tag(0); Text("Checkpoints (\(model.history.count))").tag(1) }
                    .pickerStyle(.segmented).frame(width: 310).padding(.bottom, 18)
                HStack(alignment: .top, spacing: 20) {
                    VStack(alignment: .leading, spacing: 16) {
                        if tab == 0 { notesCard.disabled(model.busy || model.isManagedWriting(task)) }
                        else { historyCard.disabled(model.busy) }
                    }.frame(maxWidth: .infinity)
                    VStack(alignment: .leading, spacing: 16) {
                        managedCard
                        projectCard.disabled(model.busy)
                        handoffCard.disabled(model.busy || model.isManagedWriting(task))
                    }
                        .frame(width: 280)
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
        .onChange(of: task.notes) { _ in model.persist() }
        .onChange(of: task.assistant) { _ in model.persist() }
        .onChange(of: model.codexCLIPath) { _ in model.persist() }
        .onChange(of: model.claudeCLIPath) { _ in model.persist() }
    }
    private var managedCard: some View {
        let state = model.managedState(for: task)
        return Card(title: "Automatic handoff", symbol: "arrow.triangle.swap") {
            Text("Runs the installed command-line agents. Stop any agent already editing this folder before starting.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Picker("Start with", selection: $task.assistant) {
                ForEach(Assistant.allCases) { assistant in Text(assistant.title).tag(assistant) }
            }.pickerStyle(.segmented).disabled(model.busy || model.isManagedWriting(task))
            VStack(alignment: .leading, spacing: 5) {
                HStack(spacing: 7) {
                    if model.isManagedTaskActive(task) { ProgressView().controlSize(.small) }
                    Text(state.title).font(.system(size: 12, weight: .semibold))
                }
                if !state.detail.isEmpty {
                    Text(state.detail).font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if state.writerActive, !state.isActive {
                    Text("Waiting for the previous agent to stop before this folder can be edited.")
                        .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
                }
                if let folder = state.projectPath {
                    Text("Active folder: \(folder)").font(.system(size: 10)).foregroundStyle(.secondary)
                        .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                }
                if state.switchCount > 0 {
                    Text("Fallback used: \(state.switchCount) of 1").font(.system(size: 10)).foregroundStyle(.secondary)
                }
            }
            if model.isManagedTaskActive(task) {
                Button { model.stopManagedRun(task) } label: { Label("Stop", systemImage: "stop.fill") }
                    .disabled(model.stoppingTasks.contains(task.id)).frame(maxWidth: .infinity)
            } else {
                Button { model.startManagedRun(task) } label: { Label("Start with auto-handoff", systemImage: "play.fill") }
                    .buttonStyle(.borderedProminent)
                    .disabled(model.busy || !task.configured || model.isManagedWriting(task)).frame(maxWidth: .infinity)
            }
            Button("Refresh run status") { Task { await model.reloadManagedStatus(task) } }
                .disabled(model.isManagedTaskActive(task) && state.status != "stopping")
            DisclosureGroup("Command-line agent paths") {
                VStack(alignment: .leading, spacing: 9) {
                    Text("Leave empty to discover installed agents.").font(.system(size: 10)).foregroundStyle(.secondary)
                    cliPathField(.codex, path: $model.codexCLIPath)
                    cliPathField(.claude, path: $model.claudeCLIPath)
                    Button("Check agents") { model.checkCLIs() }
                }.padding(.top, 6).disabled(model.busy || model.hasManagedRuns)
            }.font(.system(size: 11))
            Text("Save your notes and checkpoint before starting. Keep Pakati open. A confirmed usage limit transfers progress to a fresh folder and starts the other agent once. Existing desktop chats run independently.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
    private func cliPathField(_ assistant: Assistant, path: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(assistant.title).font(.system(size: 10, weight: .medium))
            HStack(spacing: 5) {
                TextField("Auto-detect", text: path).textFieldStyle(.roundedBorder).font(.system(size: 11))
                Button { model.chooseCLI(assistant) } label: { Image(systemName: "folder") }.help("Choose executable")
            }
        }
    }
    private var notesCard: some View {
        Card(title: "Shared task notes", symbol: "doc.text") {
            Text("Record the goal, completed work, checks, and next steps. These notes travel with the code.")
                .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            TextEditor(text: $task.notes)
                .font(.system(size: 13, design: .monospaced))
                .scrollContentBackground(.hidden)
                .padding(10).frame(height: 350)
                .background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                .overlay(RoundedRectangle(cornerRadius: 8).stroke(Color.primary.opacity(0.09)))
                .accessibilityLabel("Shared task notes")
            HStack {
                Button("Load project notes") { model.reloadProjectNotes(task) }
                    .help("Replace this editor with the current .agent-relay/notes.md in the active folder.")
                Spacer()
                Button { model.saveCheckpoint(task) } label: { Label("Save checkpoint", systemImage: "tray.and.arrow.down") }
                    .buttonStyle(.borderedProminent).disabled(!task.configured)
            }
            Text("Checkpoint saves the notes and unfinished Git changes. Ignored files and sensitive untracked filenames are excluded.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
    private var projectCard: some View {
        Card(title: "Active project", symbol: "folder") {
            VStack(alignment: .leading, spacing: 4) {
                Text(task.folderName).font(.system(size: 14, weight: .medium))
                Text(task.projectPath).font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                Button("Change folder…") { model.chooseProject(for: task) }.disabled(model.isManagedWriting(task))
                Button { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: task.projectPath) } label: { Image(systemName: "arrow.up.forward.square") }
                    .help("Show active folder in Finder")
            }
            Divider()
            Button(task.configured ? "Reconfigure project" : "Configure project") { model.configure(task) }
                .disabled(model.isManagedWriting(task)).frame(maxWidth: .infinity)
            Text("Adds handoff instructions to AGENTS.md and CLAUDE.md, plus project hooks for both assistants. Existing rules and unrelated settings are preserved.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            Text("Reopen the assistant session and accept any hook trust prompt after configuration.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
        }
    }
    private var handoffCard: some View {
        Card(title: "Continue in another assistant", symbol: "arrow.left.arrow.right") {
            Picker("Assistant", selection: $task.assistant) {
                ForEach(Assistant.allCases) { assistant in Text(assistant.title).tag(assistant) }
            }.pickerStyle(.segmented).labelsHidden()
            if let checkpoint = model.history.first {
                HStack(spacing: 8) {
                    Image(systemName: "checkmark.circle").foregroundStyle(Color.green)
                    VStack(alignment: .leading, spacing: 2) {
                        Text("Latest saved checkpoint").font(.system(size: 11, weight: .medium))
                        Text(checkpoint.dateLabel).font(.system(size: 11)).foregroundStyle(.secondary)
                    }
                }
            } else {
                Text("Save a checkpoint to transfer unfinished code into a new folder.")
                    .font(.system(size: 12)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            }
            Button { model.restore(task) } label: { Label("Restore into new folder…", systemImage: "folder.badge.plus") }
                .disabled(model.history.isEmpty).frame(maxWidth: .infinity)
            Button { model.copyPrompt(task) } label: { Label("Copy continuation prompt", systemImage: "doc.on.doc") }
                .disabled(model.history.isEmpty).frame(maxWidth: .infinity)
            Button("Open \(task.assistant.title)") { model.openAssistant(task.assistant) }.frame(maxWidth: .infinity)
            Text("Open the active project in the assistant, then paste the prompt. Handoffs continue saved task state; live chat sessions stay in their original apps.")
                .font(.system(size: 11)).foregroundStyle(.secondary).fixedSize(horizontal: false, vertical: true)
            if let restored = model.restoredPath {
                Button("Show restored folder in Finder") { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: restored) }
            }
        }
    }
    private var historyCard: some View {
        Card(title: "Saved checkpoints", symbol: "clock.arrow.circlepath") {
            HStack {
                Text("Each checkpoint preserves a version of this task.").font(.system(size: 12)).foregroundStyle(.secondary)
                Spacer()
                Button { model.loadCheckpoint(task) } label: { Label("Reload", systemImage: "arrow.clockwise") }
            }
            if model.history.isEmpty {
                VStack(spacing: 12) {
                    Image(systemName: "tray").font(.system(size: 34)).foregroundStyle(.tertiary)
                    Text("Your first checkpoint will appear here.").font(.system(size: 13)).foregroundStyle(.secondary)
                }.frame(maxWidth: .infinity).padding(.vertical, 45)
            } else {
                ScrollView {
                    VStack(spacing: 8) {
                        ForEach(model.history) { checkpoint in
                            Button {
                                model.selectedVersion = checkpoint.id; model.savedNotes = ""; model.loadCheckpoint(task)
                            } label: {
                                HStack {
                                    VStack(alignment: .leading, spacing: 4) {
                                        Text(checkpoint.dateLabel).font(.system(size: 13, weight: .semibold))
                                        Text("\(checkpoint.agent.capitalized) · \(checkpoint.branch)").font(.system(size: 11)).foregroundStyle(.secondary)
                                    }
                                    Spacer()
                                    if checkpoint.id == model.selectedVersion { Image(systemName: "checkmark.circle.fill").foregroundStyle(Color.accentColor) }
                                }.padding(11).background((checkpoint.id == model.selectedVersion ? Color.accentColor : Color.gray).opacity(0.07), in: RoundedRectangle(cornerRadius: 7))
                            }.buttonStyle(.plain)
                        }
                    }
                }.frame(maxHeight: 185)
                if let checkpoint = model.checkpoint {
                    Divider()
                    Text("Source: \(checkpoint.sourcePath)").font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                    Text("Commit: \(checkpoint.head.prefix(12))").font(.system(size: 11, design: .monospaced)).foregroundStyle(.secondary)
                    if !checkpoint.exclusions.isEmpty {
                        Text("Excluded untracked files: \(checkpoint.exclusions.joined(separator: ", "))")
                            .font(.system(size: 11)).foregroundStyle(.orange).textSelection(.enabled)
                    }
                }
                if !model.savedNotes.isEmpty {
                    ScrollView { Text(model.savedNotes).font(.system(size: 12, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                        .padding(12).frame(minHeight: 160, maxHeight: 240).background(Color(nsColor: .textBackgroundColor), in: RoundedRectangle(cornerRadius: 8))
                    Button("Use these notes in editor") { task.notes = model.savedNotes; tab = 0 }.disabled(model.isManagedWriting(task))
                } else {
                    Button("Verify and read selected checkpoint") { model.loadCheckpoint(task) }
                }
            }
        }
    }
}

struct StatusView: View {
    let status: RelayStatus
    let dismiss: () -> Void
    var body: some View {
        HStack(alignment: .top, spacing: 10) {
            Image(systemName: status.isError ? "exclamationmark.triangle.fill" : "info.circle.fill")
                .foregroundStyle(status.isError ? Color.orange : Color.accentColor)
            VStack(alignment: .leading, spacing: 5) {
                Text(status.title).font(.system(size: 12, weight: .semibold))
                Text(status.detail.trimmingCharacters(in: .whitespacesAndNewlines))
                    .font(.system(size: 11)).foregroundStyle(.secondary).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true)
            }
            Spacer(minLength: 0)
            Button(action: dismiss) { Image(systemName: "xmark") }.buttonStyle(.plain).foregroundStyle(.secondary).help("Dismiss message")
        }.padding(13).frame(maxWidth: .infinity, alignment: .leading)
            .background((status.isError ? Color.orange : Color.accentColor).opacity(0.07), in: RoundedRectangle(cornerRadius: 8))
    }
}

struct AddTaskView: View {
    @ObservedObject var model: RelayModel
    @State private var taskID = ""
    @State private var projectPath = ""
    @State private var error = ""
    var body: some View {
        VStack(alignment: .leading, spacing: 20) {
            Label("Add a task", systemImage: "arrow.triangle.branch").font(.system(size: 21, weight: .semibold))
            Text("Choose a Git project or worktree, then give this task a unique ID. The ID connects its checkpoints across assistant apps and folders.")
                .font(.system(size: 13)).foregroundStyle(.secondary)
            VStack(alignment: .leading, spacing: 8) {
                Text("Task ID").font(.system(size: 12, weight: .medium))
                TextField("For example: fix-checkout", text: $taskID).textFieldStyle(.roundedBorder)
            }
            VStack(alignment: .leading, spacing: 8) {
                Text("Project folder").font(.system(size: 12, weight: .medium))
                HStack {
                    Text(projectPath.isEmpty ? "No folder selected" : projectPath)
                        .font(.system(size: 12)).foregroundStyle(.secondary).lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                    Button("Choose…") {
                        let panel = NSOpenPanel(); panel.canChooseDirectories = true; panel.canChooseFiles = false
                        panel.allowsMultipleSelection = false; panel.prompt = "Use folder"; panel.title = "Choose a Git project or worktree"
                        if panel.runModal() == .OK, let url = panel.url { projectPath = url.path }
                    }
                }
            }
            if !error.isEmpty { Text(error).font(.system(size: 12)).foregroundStyle(.red) }
            Text("Adding a task saves this app's list. Project files change only when you choose Configure project or Save checkpoint.")
                .font(.system(size: 11)).foregroundStyle(.secondary)
            HStack {
                Spacer()
                Button("Cancel") { model.showingAdd = false }.keyboardShortcut(.cancelAction)
                Button("Add task") {
                    do { try model.addTask(taskID: taskID, path: projectPath) } catch { self.error = error.localizedDescription }
                }.buttonStyle(.borderedProminent).keyboardShortcut(.defaultAction).disabled(taskID.isEmpty || projectPath.isEmpty)
            }
        }.padding(28).frame(width: 490)
    }
}

struct WelcomeView: View {
    @ObservedObject var model: RelayModel
    var body: some View {
        VStack(spacing: 0) {
            if let status = model.status { StatusView(status: status, dismiss: { model.status = nil }).padding(24) }
            Spacer()
            VStack(spacing: 18) {
                PakatiMark().frame(width: 74, height: 74)
                Text("Your work, ready to continue.").font(.system(size: 29, weight: .bold))
                Text("Carry task notes and unfinished code between\nCodex and Claude Code, even in separate folders.")
                    .font(.system(size: 15)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(4)
                Button { model.showingAdd = true } label: { Label("Add your first task", systemImage: "plus") }
                    .buttonStyle(.borderedProminent).controlSize(.large).padding(.top, 8)
                HStack(alignment: .top, spacing: 30) {
                    welcomeStep("1", "Configure", "Connect the project\nto shared task notes.")
                    welcomeStep("2", "Checkpoint", "Save progress and\nunfinished Git changes.")
                    welcomeStep("3", "Continue", "Restore a folder and\nopen the next assistant.")
                }.padding(.top, 25)
            }
            Spacer()
            Text("Checkpoints stay on your Mac. Managed agents use your existing accounts.")
                .font(.system(size: 11)).foregroundStyle(.secondary).padding(.bottom, 30)
        }.frame(maxWidth: .infinity, maxHeight: .infinity).background(Color(nsColor: .windowBackgroundColor))
    }
    private func welcomeStep(_ number: String, _ title: String, _ text: String) -> some View {
        VStack(spacing: 8) {
            Text(number).font(.system(size: 11, weight: .bold)).frame(width: 25, height: 25).background(Color.accentColor.opacity(0.1), in: Circle()).foregroundStyle(Color.accentColor)
            Text(title).font(.system(size: 12, weight: .semibold))
            Text(text).font(.system(size: 11)).foregroundStyle(.secondary).multilineTextAlignment(.center).lineSpacing(2)
        }.frame(width: 135)
    }
}

struct RelayWindow: View {
    @ObservedObject var model: RelayModel
    var body: some View {
        NavigationSplitView {
            VStack(spacing: 0) {
                HStack(spacing: 9) {
                    PakatiMark().frame(width: 26, height: 26)
                    Text("Pakati").font(.system(size: 16, weight: .semibold))
                    Spacer()
                }.padding(.horizontal, 18).padding(.top, 22).padding(.bottom, 23)
                HStack { Text("TASKS").font(.system(size: 10, weight: .semibold)).foregroundStyle(.secondary); Spacer() }.padding(.horizontal, 19).padding(.bottom, 7)
                List(selection: $model.selectedID) {
                    ForEach(model.tasks) { task in TaskRow(task: task).tag(task.id) }
                }.listStyle(.sidebar).disabled(model.busy)
                Divider()
                Button { model.showingAdd = true } label: {
                    Label("Add task", systemImage: "plus").frame(maxWidth: .infinity, alignment: .leading)
                }.buttonStyle(.plain).padding(18).disabled(model.busy)
                HStack(spacing: 6) {
                    Image(systemName: "internaldrive")
                    Text("Local checkpoints")
                    Spacer()
                    Button { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: model.dataDirectory.path) } label: { Image(systemName: "arrow.up.forward.square") }
                        .buttonStyle(.plain).help("Show app data in Finder")
                }.font(.system(size: 10)).foregroundStyle(.secondary).padding(.horizontal, 18).padding(.bottom, 16)
            }.navigationSplitViewColumnWidth(min: 215, ideal: 235, max: 290)
        } detail: {
            if let task = model.selectedTask { TaskDetail(model: model, task: task).id(task.id) }
            else { WelcomeView(model: model) }
        }
        .frame(minWidth: 1070, minHeight: 640)
        .onChange(of: model.selectedID) { _ in model.selectTask() }
        .sheet(isPresented: $model.showingAdd) { AddTaskView(model: model) }
        .task { await model.restoreManagedStatuses() }
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button { model.showingAdd = true } label: { Image(systemName: "plus") }.help("Add task").disabled(model.busy)
            }
            ToolbarItem(placement: .automatic) {
                if model.hasManagedRuns {
                    Button {
                        for task in model.tasks where model.isManagedTaskActive(task) { model.stopManagedRun(task) }
                    } label: { Label("Stop agents", systemImage: "stop.fill") }
                    .help("Cooperatively stop all agents supervised by Pakati")
                }
            }
        }
    }
}

@MainActor final class RelayApplicationDelegate: NSObject, NSApplicationDelegate {
    weak var model: RelayModel?
    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        guard let model = model, model.hasManagedRuns else { return .terminateNow }
        model.prepareToQuit { allowed in sender.reply(toApplicationShouldTerminate: allowed) }
        return .terminateLater
    }
}

@MainActor private func verifyModel(dataDirectory: URL) throws -> [String: Bool] {
    let testRoot = dataDirectory.appendingPathComponent(".model-smoke-\(UUID().uuidString)", isDirectory: true)
    defer { try? FileManager.default.removeItem(at: testRoot) }
    let project = testRoot.appendingPathComponent("project", isDirectory: true)
    let notesFolder = project.appendingPathComponent(".agent-relay", isDirectory: true)
    try FileManager.default.createDirectory(at: notesFolder, withIntermediateDirectories: true)
    let notesURL = notesFolder.appendingPathComponent("notes.md")
    try "Original project notes\n".write(to: notesURL, atomically: true, encoding: .utf8)
    let modelDirectory = testRoot.appendingPathComponent("app-data", isDirectory: true)
    let model = RelayModel(dataDirectory: modelDirectory)
    let config: [String: Any] = ["task": "model-smoke", "store": model.storeURL.path, "enabled": true]
    try JSONSerialization.data(withJSONObject: config).write(to: project.appendingPathComponent(".agent-relay.json"))
    try model.addTask(taskID: "model-smoke", path: project.path)
    guard let task = model.selectedTask else { throw EngineError.message("Model smoke test could not add a task.") }
    task.notes = "Unsaved editor draft\n"
    try "Assistant updated project notes\n".write(to: notesURL, atomically: true, encoding: .utf8)
    model.saveCheckpoint(task)
    let preservedDiskNotes = try String(contentsOf: notesURL, encoding: .utf8)
    let conflictSafe = model.status?.isError == true && !model.busy && preservedDiskNotes == "Assistant updated project notes\n"
    model.persist()
    let reloaded = RelayModel(dataDirectory: modelDirectory)
    guard let reloadedTask = reloaded.selectedTask else { throw EngineError.message("Model smoke test could not reload its task.") }
    let draftPreserved = reloadedTask.notes == "Unsaved editor draft\n"
    reloaded.saveCheckpoint(reloadedTask)
    let relaunchConflictSafe = reloaded.status?.isError == true && !reloaded.busy
    reloaded.reloadProjectNotes(reloadedTask)
    let reloadSafe = reloadedTask.notes == "Assistant updated project notes\n" && reloadedTask.notesBaseline == reloadedTask.notes
    var duplicateRefused = false
    do { try reloaded.addTask(taskID: "model-smoke", path: project.path) } catch { duplicateRefused = true }
    let results = ["note_conflict_preserves_disk": conflictSafe, "draft_survives_relaunch": draftPreserved,
                   "relaunch_preserves_conflict_guard": relaunchConflictSafe, "reload_resolves_note_conflict": reloadSafe,
                   "duplicate_task_id_refused": duplicateRefused]
    guard results.values.allSatisfy({ $0 }) else { throw EngineError.message("UI model smoke tests failed: \(results)") }
    return results
}

private func verifyManagedStatus(dataDirectory: URL) throws -> Bool {
    let rootPath = (dataDirectory.path as NSString).appendingPathComponent(".managed-smoke-\(UUID().uuidString)")
    let storePath = (rootPath as NSString).appendingPathComponent("checkpoints")
    defer { try? FileManager.default.removeItem(atPath: rootPath) }
    guard !FileManager.default.fileExists(atPath: storePath) else {
        throw EngineError.message("The managed-status smoke store must be fresh.")
    }
    let output = try EngineRunner.run(dataDirectory: dataDirectory,
            arguments: ["auto", "status", "--task", "native-smoke", "--store", storePath])
    guard let json = output.json, let state = ManagedRunState(json),
          state.status == "idle", !state.blocksWrites else {
        throw EngineError.message("The native app and bundled helper did not return an idle managed status for a fresh store.")
    }
    return true
}

@main struct AgentRelayApp: App {
    @StateObject private var model = RelayModel()
    @NSApplicationDelegateAdaptor(RelayApplicationDelegate.self) private var appDelegate
    init() {
        if CommandLine.arguments.contains("--smoke-test") {
            guard let value = ProcessInfo.processInfo.environment["AGENT_RELAY_DATA_DIR"], !value.isEmpty else {
                fputs("Smoke tests require AGENT_RELAY_DATA_DIR to keep app data isolated.\n", stderr); exit(2)
            }
            do {
                let directory = EngineRunner.dataDirectory()
                let engine = try EngineRunner.prepareRuntime(dataDirectory: directory)
                let output = try EngineRunner.run(dataDirectory: directory, arguments: ["--help"])
                let modelChecks = try verifyModel(dataDirectory: directory)
                let managedStatusReady = try verifyManagedStatus(dataDirectory: directory)
                let result: [String: Any] = ["status": "ok", "version": relayVersion, "data_directory": directory.path,
                                          "engine": engine.path, "engine_help": output.stdout, "model_checks": modelChecks,
                                          "fresh_managed_status_idle": managedStatusReady]
                let data = try JSONSerialization.data(withJSONObject: result, options: [.prettyPrinted, .sortedKeys])
                FileHandle.standardOutput.write(data); FileHandle.standardOutput.write(Data("\n".utf8)); exit(0)
            } catch { fputs("\(error.localizedDescription)\n", stderr); exit(2) }
        }
    }
    var body: some Scene {
        WindowGroup("Pakati") {
            RelayWindow(model: model)
                .tint(Color(nsColor: .labelColor))
                .accentColor(Color(nsColor: .labelColor))
                .onAppear { appDelegate.model = model }
        }
            .defaultSize(width: 1160, height: 760)
            .commands {
                CommandGroup(replacing: .newItem) {
                    Button("Add task…") { model.showingAdd = true }.keyboardShortcut("n").disabled(model.busy)
                }
            }
    }
}
