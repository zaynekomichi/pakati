import SwiftUI
import AppKit
import Foundation
import CryptoKit
import Darwin

private let relayVersion = "0.2.0"
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

enum EngineError: LocalizedError {
    case message(String)
    var errorDescription: String? { switch self { case .message(let text): return text } }
}

enum EngineRunner {
    static func dataDirectory() -> URL {
        if let override = ProcessInfo.processInfo.environment["AGENT_RELAY_DATA_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true).standardizedFileURL
        }
        return FileManager.default.homeDirectoryForCurrentUser
            .appendingPathComponent("Library/Application Support/Agent Relay", isDirectory: true)
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
    static func run(dataDirectory: URL, arguments: [String]) throws -> EngineOutput {
        let executable = try prepareRuntime(dataDirectory: dataDirectory)
        let process = Process(); process.executableURL = executable; process.arguments = arguments
        let outputPipe = Pipe(), errorPipe = Pipe()
        process.standardOutput = outputPipe; process.standardError = errorPipe
        var environment = ProcessInfo.processInfo.environment
        environment["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin:/usr/local/bin:/opt/homebrew/bin"
        process.environment = environment
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
        guard process.terminationStatus == 0 else {
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
    let dataDirectory: URL
    private var settingsRecoveryNeeded = false
    var storeURL: URL { dataDirectory.appendingPathComponent("checkpoints", isDirectory: true) }
    var selectedTask: RelayTask? { tasks.first { $0.id == selectedID } }
    var checkpoint: Checkpoint? { history.first { $0.id == selectedVersion } ?? history.first }

    init(dataDirectory: URL = EngineRunner.dataDirectory()) {
        self.dataDirectory = dataDirectory
        let settings = dataDirectory.appendingPathComponent("settings.json")
        if FileManager.default.fileExists(atPath: settings.path) {
            do {
                let saved = try JSONDecoder().decode(SavedSettings.self, from: Data(contentsOf: settings))
                guard saved.schemaVersion == 1 else { throw EngineError.message("This settings file uses an unsupported version.") }
                tasks = saved.tasks
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
            try encoder.encode(SavedSettings(selectedID: selectedID, tasks: tasks))
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
        var ancestor = url.standardizedFileURL
        var missingComponents: [String] = []
        // Foundation leaves an absent path unresolved. Resolve its existing ancestor first;
        // the checkpoint store does not exist yet when a project is initially configured.
        while !FileManager.default.fileExists(atPath: ancestor.path) {
            let parent = ancestor.deletingLastPathComponent()
            guard parent.path != ancestor.path else { break }
            missingComponents.append(ancestor.lastPathComponent)
            ancestor = parent
        }
        return missingComponents.reversed().reduce(ancestor.resolvingSymlinksInPath().path) {
            ($0 as NSString).appendingPathComponent($1)
        }
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
    private func execute(_ name: String, arguments: [String], success: @escaping (EngineOutput) throws -> Void) {
        guard !busy else { return }
        busy = true; operation = name; status = nil
        let directory = dataDirectory
        Task {
            do {
                let output = try await Task.detached(priority: .userInitiated) {
                    try EngineRunner.run(dataDirectory: directory, arguments: arguments)
                }.value
                try success(output)
            } catch { status = RelayStatus(title: "\(name) failed", detail: error.localizedDescription, isError: true) }
            busy = false; operation = ""; persist()
        }
    }
    func configure(_ task: RelayTask) {
        execute("Configure project", arguments: ["setup", "--project", task.projectPath, "--store", storeURL.path, "--task", task.taskID]) { output in
            task.configured = self.configurationMatches(task)
            self.status = RelayStatus(title: "Project configured", detail: output.stdout + output.stderr)
        }
    }
    func saveCheckpoint(_ task: RelayTask) {
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
                    "--store", storeURL.path, "--agent", task.assistant.rawValue, "--notes", notesURL.path]) { output in
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
        execute("Load checkpoint", arguments: ["show", "--task", task.taskID, "--store", storeURL.path, "--version", version]) { output in
            guard let value = output.json, let notes = value["notes"] as? String else { throw EngineError.message("The engine did not return checkpoint notes.") }
            self.savedNotes = notes
            self.status = RelayStatus(title: "Checkpoint verified", detail: "The saved code and notes passed the engine's integrity checks.")
        }
    }
    func restore(_ task: RelayTask) {
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
        execute("Restore handoff", arguments: ["restore", "--task", task.taskID, "--store", storeURL.path, "--version", version, "--into", destination.path]) { output in
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
                        if tab == 0 { notesCard } else { historyCard }
                    }.frame(maxWidth: .infinity)
                    VStack(alignment: .leading, spacing: 16) { projectCard; handoffCard }
                        .frame(width: 280)
                }
            }
            .padding(28)
            .frame(maxWidth: .infinity, alignment: .topLeading)
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        .background(Color(nsColor: .windowBackgroundColor))
        .disabled(model.busy)
        .onChange(of: task.notes) { _ in model.persist() }
        .onChange(of: task.assistant) { _ in model.persist() }
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
                Button("Change folder…") { model.chooseProject(for: task) }
                Button { NSWorkspace.shared.selectFile(nil, inFileViewerRootedAtPath: task.projectPath) } label: { Image(systemName: "arrow.up.forward.square") }
                    .help("Show active folder in Finder")
            }
            Divider()
            Button(task.configured ? "Reconfigure project" : "Configure project") { model.configure(task) }.frame(maxWidth: .infinity)
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
                    Button("Use these notes in editor") { task.notes = model.savedNotes; tab = 0 }
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
            Text("Stored locally on your Mac. No account or cloud service required.")
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
        .toolbar {
            ToolbarItem(placement: .automatic) {
                Button { model.showingAdd = true } label: { Image(systemName: "plus") }.help("Add task").disabled(model.busy)
            }
        }
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

@main struct AgentRelayApp: App {
    @StateObject private var model = RelayModel()
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
                let result: [String: Any] = ["status": "ok", "version": relayVersion, "data_directory": directory.path,
                                          "engine": engine.path, "engine_help": output.stdout, "model_checks": modelChecks]
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
        }
            .defaultSize(width: 1160, height: 760)
            .commands {
                CommandGroup(replacing: .newItem) {
                    Button("Add task…") { model.showingAdd = true }.keyboardShortcut("n").disabled(model.busy)
                }
            }
    }
}
