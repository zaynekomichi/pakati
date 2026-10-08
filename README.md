# Pakati for Mac

<img src="Assets/AppIcon.png" alt="Pakati: a black center dot surrounded by radial lines on white" width="128" />

Pakati is a native macOS app that keeps a coding task ready to continue between Codex and Claude Code. Its name means “center” in Shona: one place for your agents’ shared task notes and unfinished work.

The app pairs a shared task note with a Git checkpoint. It saves the goal, decisions, progress, and next action alongside committed history and unfinished code, then restores that work into a new folder when you switch assistants.

Pakati is open source under the [MIT license](LICENSE). Contributions are welcome; see [CONTRIBUTING.md](CONTRIBUTING.md).

## Install

Version 0.3.0 includes the automatic handoff mode described below.

Open `Pakati-mac.dmg` and drag **Pakati** into **Applications**. Open the app from Applications. `Pakati-mac-app.zip` is an alternative copy of the same app; `Pakati-mac-source.zip` contains the editable source and build instructions.

This build targets Apple Silicon Macs running macOS 13 or later. It bundles its Python runtime; you do not need to install Python or use Terminal for ordinary app actions. Git must be available, for example through Apple's Command Line Tools or Xcode. Both coding assistants keep their existing accounts and usage limits.

The build has an ad-hoc code signature for local testing. It has not been signed with an Apple Developer ID or notarized. On another Mac, Gatekeeper may require approval through System Settings. Public distribution should use Developer ID signing and notarization. [Apple's distribution guidance](https://developer.apple.com/developer-id/)

## Start a task

1. Add a task and choose the active Git project or worktree where the work is happening.
2. Give the task a unique ID, choose the assistant doing the work, and write the goal and current next steps.
3. Configure the project. This explicit action adds project instructions and hooks for both assistants while preserving existing settings.
4. Save a checkpoint, then start a fresh assistant session so it loads the instructions. Review and trust the Codex project hooks when prompted.

The project hooks capture code during work and load the saved context when a session starts. Ask the agent to keep `.agent-relay/notes.md` current after meaningful progress and before long operations. Git cannot supply the explanation of why a decision was made.

## Automatic handoff

Configure a task, write its goal, progress, constraints, and next steps, then **Save checkpoint**. Choose the starting assistant and select **Start with auto-handoff**. Save any editor draft and resolve newer notes from the project before starting. This mode runs the installed Codex and Claude Code command-line tools in the background. Stop any independently running agent that is editing the same folder before starting.

Both command-line tools must be installed and signed in. Pakati checks their availability and required options before starting; optional executable paths in the app support installations outside the usual locations. It uses the tools' normal account configuration. [Codex programmatic execution](https://learn.chatgpt.com/docs/non-interactive-mode), [Claude programmatic execution](https://code.claude.com/docs/en/headless).

During a managed run, Pakati saves periodic checkpoints and reads the latest task notes without replacing agent edits. If the agent reports confirmed account usage exhaustion, Pakati waits for its managed process group to stop, saves a final checkpoint, restores into a new folder, configures that folder, and records its adoption before starting the other agent. The active folder and assistant update in the task view. The original folder remains available.

The default is one automatic switch per run. If both agents are unavailable, a tool needs approval, authentication fails, or a checkpoint cannot be saved, the run stops with a status explaining the next action. Ordinary replies, temporary HTTP 429 errors, and quota wording inside project files or tool output do not trigger a switch. **Agent response finished** means the run ended; review its work and notes to determine whether the original goal is complete.

Use **Stop** to cancel the active run. Keep Pakati open while it supervises the agents. Quitting requests a stop first; reopening reads the recorded status and does not silently start a new run. Agents should update the shared notes throughout the task, because an exhausted agent may be unable to write a final explanation.

This mode controls agents started by Pakati. It does not automatically take over a conversation already running in another desktop app. Existing desktop sessions can still use the manual handoff below.

Cleanup covers the agent and ordinary tools that remain in its process group. A tool that deliberately detaches into a separate session and drops inherited locks is outside that supervision. Running servers and other external processes are not transferred in a checkpoint.

### Run the controller from source

No app build is needed to exercise the controller. Use a configured Git project, meaningful `.agent-relay/notes.md`, and absolute paths for your checkpoint store and handoff folder:

```sh
python3 Engine/engine.py auto preflight
python3 Engine/engine.py auto run --task my-task --store /absolute/path/shared-store --project /absolute/path/project --agent codex --handoff-root /absolute/path/handoffs
```

From a second terminal, inspect or stop the run:

```sh
python3 Engine/engine.py auto status --task my-task --store /absolute/path/shared-store
python3 Engine/engine.py auto stop --task my-task --store /absolute/path/shared-store
```

Use `--codex-cli /absolute/path/codex` and `--claude-cli /absolute/path/claude` on `auto preflight` or `auto run` to choose specific executables. Keep the store and handoff folder outside the project. Stop the source controller before moving its source files.

## Manual continuation in the other assistant

Stop the current agent from making further edits. Save a checkpoint, or inspect the latest automatic checkpoint if the usage limit has already been reached.

Choose a checkpoint and restore it into a new folder. The app refuses to overwrite an existing folder. Open the restored folder as the other assistant's local project, copy the continuation prompt, and ask it to continue. The app can reveal the folder and open the installed assistant, but you still start the conversation yourself.

Opening an old main branch does not bring unfinished work across. If the assistant creates its own worktree, ensure it carries the restored uncommitted changes too.

## Local storage

The app keeps its task list, versioned checkpoints, and a stable helper runtime under:

```text
~/Library/Application Support/Agent Relay/
```

Pakati reuses the earlier Agent Relay storage directory, bundle identity, and project filenames so existing tasks and installed hooks continue to work through the rename.

Project hooks point to that stable helper, so moving or closing the app does not break them. Pakati does not upload checkpoints, read private chat transcripts, or modify global Codex or Claude settings. Managed command-line agents communicate with their services and use their normal accounts and usage limits. The store may include repository history and source code; keep it private.

Checkpoint versions are retained; there is no automatic storage pruning in this build.

## Scope

- The source must be an ordinary Git checkout with at least one commit.
- Checkpoints preserve staged and unstaged changes independently, binary files, regular nonignored new files, and executable permissions.
- Ignored files, dependencies, environment setup, running processes, browser state, and connector access are not captured.
- Common secret-like untracked filenames are excluded. This does not scrub tracked files, repository history, or user-authored notes.
- Submodules, Git LFS, sparse checkouts, hidden index entries, unmerged indexes, intent-to-add entries, and untracked symlinks produce unsupported-state errors.
- Restores start on a detached Git HEAD. Original branch names are recorded, but original remotes are not copied. Create the appropriate branch and configure the verified remote before publishing.
- Automatic captures can lag behind edits because tool snapshots are throttled. A crash or undocumented usage-limit screen may prevent a final capture. Save notes and checkpoints throughout the task.

The manual flow prepares a handoff; the managed mode starts a new command-line continuation. Neither flow transfers native chats or automatically submits a new task in another desktop app. Uncommitted source files and task notes supply the context for a cross-agent continuation.

## Build from source

The source package includes native SwiftUI code, backend code, app assets, the build script, and local integration tests.

```sh
git clone https://github.com/zaynekomichi/pakati.git
cd pakati
./build-mac.sh
```

Building requires an Apple Silicon Mac, Xcode's command-line toolchain, Python 3.9 or newer with a shared Python runtime suitable for PyInstaller, and network access for the build dependency if it is not already present. Build work stays in `work/` and the app ZIP and DMG are written to `outputs/` inside the cloned repository. The resulting app bundles the runtime and does not require the build tools on a machine with Git already available.

Set `RELAY_BUILD_PYTHON` to the path of a suitable Python interpreter if `python3` is not the one you want to bundle. For the delivered source ZIP, extract it and run `./build-mac.sh` inside its source folder; cloning is optional.

Run the source backend checks without building the app:

```sh
python3 Engine/test_relay.py
python3 Engine/test_autopilot.py
python3 Tests/test_auto_regressions.py --scratch "$PWD/work/auto-regressions"
```

On an Apple Silicon Mac, run the native checks using disposable scratch folders:

```sh
python3 Tests/test_notes_model.py --scratch "$PWD/work/model-tests"
python3 Tests/test_runtime_runner.py --scratch "$PWD/work/runner-tests"
python3 Tests/test_auto_model.py --scratch "$PWD/work/auto-model-tests"
```

The managed model driver uses the Swift interpreter/JIT and does not build an app or helper. See `VERIFICATION.md` for the actual checks completed on the packaged version and source update. The executable packages the Python backend using [PyInstaller's macOS support](https://pyinstaller.org/en/stable/usage.html).
