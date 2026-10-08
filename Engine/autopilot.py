#!/usr/bin/env python3
"""Pakati managed CLI runs. Only confirmed provider quota failures cause a handoff.

This supervisor never reads authentication files or parses tool/user text as an
error. The separate CLI processes load their normal account login and project
rules. A successful response ends this run; it does not prove the goal finished.
"""
from __future__ import annotations

import argparse
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import uuid

import relay
from commands import engine_command

ACTIVE = {"running", "checkpointing", "switching"}
SNAPSHOT_SECONDS = 60.0
MAX_LINE = 1024 * 1024
MAX_CAPTURE = 65536
MAX_NOTES = 131072
QUOTA_CODES = {"usage_limit_reached", "usage_limit_exceeded", "credits_required"}
CLAUDE_WINDOWS = {"five_hour", "seven_day", "seven_day_opus", "seven_day_sonnet", "overage"}


class AutoError(Exception):
    pass


class Cancelled(AutoError):
    pass


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def safe_env():
    # Keep account/profile/project settings, but do not initiate API-key auth or
    # accidentally redirect Git to a checkout inherited from a caller.
    excluded = {"CODEX_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}
    return {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and k not in excluded}


def atomic_json(path, value):
    if path.is_symlink():
        raise AutoError("Managed state files must not be symlinks.")
    fd, temporary = tempfile.mkstemp(prefix=".pending-", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def read_json(path):
    if path.is_symlink():
        raise AutoError("Managed state files must not be symlinks.")
    if not path.exists():
        return None
    if path.stat().st_size > MAX_CAPTURE:
        raise AutoError("Managed state file exceeds its size limit.")
    try:
        result = json.loads(path.read_text(encoding="utf-8"))
    except (ValueError, UnicodeError) as error:
        raise AutoError("Managed state is invalid; inspect it before starting another run.") from error
    if not isinstance(result, dict):
        raise AutoError("Managed state must be a JSON object.")
    return result


def secure_directory(path):
    # Reject existing symlink components before resolving a user supplied path.
    path = Path(path).expanduser().absolute()
    for component in [*reversed(path.parents), path]:
        if component.is_symlink():
            raise AutoError("Managed state and handoff directories must not contain symlinks.")
        if component.exists() and not component.is_dir():
            raise AutoError("Managed state and handoff paths must be directories.")
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    return path.resolve()


def task_paths(store, task):
    if not relay.TASK_RE.fullmatch(task):
        raise AutoError("Task ID must be 1–64 letters, numbers, underscores or hyphens.")
    store = secure_directory(store)
    folder = secure_directory(store / ".autopilot" / task)
    return store, folder


def lock_file(path):
    flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    os.set_inheritable(fd, False)
    stream = os.fdopen(fd, "r+")
    try:
        fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        stream.close()
        raise AutoError("A managed run already owns this task or checkout.")
    return stream


def inside(path, parent):
    return path == parent or parent in path.parents


def locate_cli(agent, override=None):
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_absolute():
            raise AutoError("An explicit CLI path must be absolute.")
        candidates = [candidate]
    else:
        found = shutil.which(agent)
        candidates = [Path(found)] if found else []
        candidates += [Path.home() / ".local/bin" / agent, Path.home() / ".npm-global/bin" / agent,
                       Path("/opt/homebrew/bin") / agent, Path("/usr/local/bin") / agent]
        if agent == "codex":
            candidates += [Path(app) / suffix for app in ("/Applications/Codex.app", "/Applications/ChatGPT.app")
                           for suffix in ("Contents/Resources/codex", "Contents/Resources/codex-cli/CodexCLI.app/Contents/MacOS/codex")]
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate.resolve())
    raise AutoError("CLI executable was not found. Install the CLI and sign in, or choose its path.")


def bounded_process(argv, timeout, check_cancel=None, started=None, pass_fds=(), cwd=None):
    process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=safe_env(), start_new_session=True, pass_fds=pass_fds, cwd=cwd)
    if started:
        started(process)
    output = bytearray()
    excessive = False
    selector = selectors.DefaultSelector()
    os.set_blocking(process.stdout.fileno(), False)
    selector.register(process.stdout, selectors.EVENT_READ)
    until = time.monotonic() + timeout
    try:
        while selector.get_map() or process.poll() is None:
            if check_cancel:
                check_cancel()
            if time.monotonic() > until:
                raise AutoError("CLI or relay operation timed out.")
            if process.poll() is not None:
                settle_group(process)
            for key, _ in selector.select(0.05):
                chunk = os.read(key.fd, 16384)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                remaining = MAX_CAPTURE - len(output)
                output.extend(chunk[:remaining])
                excessive = excessive or len(chunk) > remaining
        return process.returncode, bytes(output), excessive
    finally:
        selector.close()
        settle_group(process)
        process.stdout.close()


def probe_command(argv, check_cancel=None):
    # Only help/version: no account lookup, model request or API credentials.
    returncode, content, excessive = bounded_process(argv, 15, check_cancel)
    if returncode or excessive:
        raise AutoError("CLI help/version failed or returned excessive output.")
    return content.decode("utf-8", errors="replace")


def preflight(codex_cli=None, claude_cli=None, check_cancel=None):
    agents = {}
    for agent, override in (("codex", codex_cli), ("claude", claude_cli)):
        entry = {"available": False, "path": None, "version": None, "detail": ""}
        try:
            if agent == "claude" and os.environ.get("CLAUDECODE"):
                raise AutoError("Claude's nested-session guard is active. Start Pakati from a standalone app or terminal.")
            path = locate_cli(agent, override)
            entry["path"] = path
            version = probe_command([path, "--version"], check_cancel).strip().splitlines()
            entry["version"] = version[0][:160] if version else ""
            help_text = probe_command([path, "exec", "--help"] if agent == "codex" else [path, "--help"], check_cancel)
            required = ["--json", "--sandbox"] if agent == "codex" else ["--output-format", "--permission-mode", "--permission-prompts", "--verbose"]
            # Approval is a Codex global flag, not necessarily in exec help.
            if agent == "codex":
                global_help = probe_command([path, "--help"], check_cancel)
                if "--ask-for-approval" not in global_help:
                    raise AutoError("Codex CLI lacks the required approval flag; update the CLI.")
            if any(flag not in help_text for flag in required):
                raise AutoError("CLI lacks the required structured-output or permission flags; update the CLI.")
            entry.update(available=True, detail="CLI flags verified. Existing CLI login will be used; account availability is checked only when a run starts.")
        except Cancelled:
            raise
        except (AutoError, OSError) as error:
            entry["detail"] = str(error)[:300]
        agents[agent] = entry
    ready = all(entry["available"] for entry in agents.values())
    return {"ready": ready, "agents": agents, "detail": "Both CLIs are ready." if ready else "Both supported CLIs are required before starting a managed run."}


def quota_message(value):
    # Deliberately narrow provider messages, only called on lifecycle error
    # envelopes. Generic 429/rate_limit/network errors are never sufficient.
    if not isinstance(value, str):
        return False
    return bool(re.match(r"^(?:you(?:'|’)ve hit your usage limit|you have (?:hit|reached) your usage limit|your (?:weekly |monthly |five.hour |5.hour )?usage limit (?:has been reached|is exhausted)|you(?:'|’)re out of extra usage)", value.strip(), re.I))


class Lifecycle:
    def __init__(self, agent):
        self.agent = agent
        self.success = False
        self.failed = False
        self.quota = False
        self.permission = False
        self.terminal = False
        self.subscription_rejected = False
        self.api_quota_failure = False
        self.events = 0

    def accept(self, event):
        if not isinstance(event, dict):
            return
        self.events += 1
        kind = event.get("type")
        if self.agent == "codex":
            if kind == "turn.completed":
                self.success = True
                self.terminal = True
            elif kind == "turn.failed":
                self.failed = True
                self.terminal = True
                error = event.get("error")
                if isinstance(error, dict):
                    self.quota = error.get("code") in QUOTA_CODES or quota_message(error.get("message"))
                    self.permission = error.get("code") in {"permission_denied", "approval_required", "sandbox_denied"}
            # `error` alone can describe a recoverable reconnect attempt. It
            # does not replace the terminal turn.failed signal.
        else:
            # Subagent events are not the main run's lifecycle.
            if event.get("parent_tool_use_id"):
                return
            if kind == "rate_limit_event":
                info = event.get("rate_limit_info")
                if isinstance(info, dict):
                    overage_available = info.get("overageStatus") in {"allowed", "allowed_warning"} or info.get("isUsingOverage") is True
                    self.subscription_rejected = (info.get("status") == "rejected" and
                                                  (info.get("errorCode") == "credits_required" or
                                                   (info.get("rateLimitType") in CLAUDE_WINDOWS and not overage_available)))
            elif kind == "assistant" and isinstance(event.get("error"), str):
                self.api_quota_failure = event["error"] == "rate_limit"
            elif kind == "system" and event.get("subtype") == "permission_denied":
                self.permission = True
            elif kind == "result":
                self.terminal = True
                self.permission = self.permission or bool(event.get("permission_denials"))
                reason = event.get("terminal_reason")
                interrupted = reason is not None and reason not in {"completed", "api_error"}
                self.failed = (event.get("is_error") is True or event.get("subtype") != "success" or
                               reason == "api_error" or interrupted or bool(event.get("api_error_status")))
                self.success = not self.failed
                http_status = event.get("api_error_status")
                provider_quota = http_status == 429 if http_status is not None else self.api_quota_failure
                local_limit = event.get("subtype") in {"error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"}
                self.quota = (self.failed and not local_limit and reason in {None, "api_error"} and
                              provider_quota and self.subscription_rejected)

    def outcome(self, returncode):
        if self.permission:
            return "permission"
        if self.failed and self.quota and self.terminal:
            return "quota"
        if returncode == 0 and self.success and self.terminal and not self.failed:
            return "success"
        return "failure"


def group_exists(pid):
    try:
        os.killpg(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def group_is_live(pid):
    # Read-only liveness check for stale records. Never signal a recorded PID.
    result = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=3,
                            start_new_session=True)
    if result.returncode:
        return True
    return any(len(parts) >= 2 and parts[0] == str(pid) and not parts[1].startswith("Z")
               for parts in (line.split() for line in result.stdout.splitlines()))


def settle_group(process):
    """Reap the leader and stop descendants, even after a successful exit."""
    for sig, deadline in ((signal.SIGTERM, 2.0), (signal.SIGKILL, 2.0)):
        if not group_exists(process.pid):
            break
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        until = time.monotonic() + deadline
        while time.monotonic() < until:
            process.poll()
            if not group_exists(process.pid):
                break
            time.sleep(0.025)
    if process.poll() is None:
        process.wait(timeout=2)
    # Dead orphan zombies can temporarily retain a PGID; SIGKILL has already
    # prevented them writing. Check live group members with ps, not pid reuse.
    if group_exists(process.pid):
        try:
            listing = subprocess.run(["/bin/ps", "-axo", "pgid=,stat="], capture_output=True, text=True, timeout=3)
            live = any(len(parts) >= 2 and parts[0] == str(process.pid) and not parts[1].startswith("Z")
                       for parts in (line.split() for line in listing.stdout.splitlines()))
            if listing.returncode or live:
                raise AutoError("An agent process group could not be stopped; no handoff was started.")
        except subprocess.SubprocessError as error:
            raise AutoError("Could not confirm the agent process group stopped.") from error


def guard(argv):
    """Child watchdog: EOF of the supervisor lifeline stops the entire session.

    The watchdog owns the inherited checkout lock until its child and ordinary
    descendants stop. It survives a hard-killed supervisor without depending on
    stale PIDs in a disk file. This private command never runs a shell.
    """
    options = argparse.ArgumentParser(add_help=False)
    options.add_argument("--lifeline-fd", type=int, required=True)
    options.add_argument("--checkout-fd", type=int, required=True)
    options.add_argument("argv", nargs=argparse.REMAINDER)
    args = options.parse_args(argv)
    command = args.argv[1:] if args.argv and args.argv[0] == "--" else args.argv
    if not command:
        return 2
    stopping = False

    def cancelled(signum, frame):
        nonlocal stopping
        stopping = True

    signal.signal(signal.SIGTERM, cancelled)
    signal.signal(signal.SIGINT, cancelled)
    selector = selectors.DefaultSelector()
    selector.register(args.lifeline_fd, selectors.EVENT_READ)
    if selector.select(0) and not os.read(args.lifeline_fd, 1):
        selector.close()
        os.close(args.lifeline_fd)
        os.close(args.checkout_fd)
        return 0
    process = subprocess.Popen(command, stdin=sys.stdin, stdout=sys.stdout, stderr=sys.stderr,
                               env=safe_env(), pass_fds=(args.checkout_fd,))
    cleaned = False
    try:
        while process.poll() is None and not stopping:
            if selector.select(0.1):
                if not os.read(args.lifeline_fd, 1):
                    stopping = True
        # All ordinary tools share this watchdog's fresh session/group. TERM
        # also reaches this watchdog; its handler keeps the lock until cleanup.
        os.killpg(os.getpgrp(), signal.SIGTERM)
        until = time.monotonic() + 2
        live = True
        while time.monotonic() < until:
            process.poll()
            listing = subprocess.run(["/bin/ps", "-axo", "pid=,pgid=,stat="], capture_output=True, text=True, timeout=3,
                                     start_new_session=True)
            live = listing.returncode != 0 or any(len(parts) >= 3 and parts[1] == str(os.getpgrp()) and
                                                  parts[0] != str(os.getpid()) and not parts[2].startswith("Z")
                                                  for parts in (line.split() for line in listing.stdout.splitlines()))
            if not live:
                break
            time.sleep(0.05)
        if live:
            # Includes the watchdog itself. A hard cleanup returns failure,
            # ensuring a replacement never starts on an uncertain success.
            os.killpg(os.getpgrp(), signal.SIGKILL)
        process.wait(timeout=2)
        cleaned = True
        return process.returncode
    finally:
        if not cleaned:
            # Unexpected watchdog errors must not leave a live writer behind.
            os.killpg(os.getpgrp(), signal.SIGKILL)
        selector.close()
        os.close(args.lifeline_fd)
        os.close(args.checkout_fd)


def status(task, store):
    _, folder = task_paths(store, task)
    try:
        lock = lock_file(folder / "run.lock")
    except AutoError:
        state = read_json(folder / "state.json") or {"status": "running", "task": task, "detail": "A managed run is starting."}
        state["writer_active"] = True
        return state
    with lock:
        state = read_json(folder / "state.json") or {"schema_version": 1, "task": task, "status": "idle", "detail": "No managed run has started."}
        if state.get("status") in ACTIVE:
            # No owner of the run lock survives. Do not signal recorded PIDs:
            # they could now belong to unrelated processes.
            orphaned = state.get("agent_pid")
            state.update(status="interrupted", pid=None, orphaned_group_pid=orphaned, updated_at=now(),
                         detail="The previous supervisor ended unexpectedly. Review the checkout and checkpoint before starting again.")
            atomic_json(folder / "state.json", state)
        state["writer_active"] = writer_active(state)
        return state


def writer_active(state):
    git_dir = state.get("checkout_git_dir")
    if isinstance(git_dir, str) and Path(git_dir).is_absolute():
        path = Path(git_dir) / "pakati-managed-writer.lock"
        try:
            fd = os.open(path, os.O_RDWR | getattr(os, "O_NOFOLLOW", 0))
        except FileNotFoundError:
            pass
        except OSError:
            return True
        else:
            try:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    return True
            finally:
                os.close(fd)
    orphan = state.get("agent_pid") or state.get("orphaned_group_pid")
    return isinstance(orphan, int) and orphan > 1 and group_is_live(orphan)


def stop(task, store):
    _, folder = task_paths(store, task)
    state = status(task, store)
    if state.get("status") in ACTIVE and state.get("run_id"):
        atomic_json(folder / "cancel.json", {"run_id": state["run_id"], "requested_at": now()})
        state = dict(state, detail="Stop requested; Pakati is settling the agent process group and saving a checkpoint.")
    return state


class Supervisor:
    def __init__(self, args, emit=None, snapshot_seconds=SNAPSHOT_SECONDS):
        self.args = args
        self.emit_callback = emit or self.print_event
        self.snapshot_seconds = snapshot_seconds
        self.cancelled = False
        self.child = None
        self.lifeline = None
        self.checkout_lock = None
        self.state = {}
        self.folder = None
        self.project = None
        self.agent = args.agent
        self.last_version = None
        # A onefile build has an outer launcher and an application child. A
        # hard-killed launcher cannot forward a signal to this supervisor.
        # Source runs retain their existing independent CLI lifetime.
        self.launcher_pid = os.getppid() if getattr(sys, "frozen", False) else None
        self.launcher_lost = False

    @staticmethod
    def print_event(value):
        print(json.dumps(value, ensure_ascii=False), flush=True)

    def emit(self, event, **extra):
        envelope = dict(self.state, type="event", event=event, **extra)
        self.emit_callback(envelope)

    def update(self, status_value, detail, **extra):
        if status_value not in ACTIVE and self.checkout_lock:
            # Terminal state is emitted only after child cleanup and snapshots.
            # Release our ownership before computing whether another writer is
            # still active, so the UI can safely allow edits after completion.
            self.checkout_lock.close()
            self.checkout_lock = None
        self.state.update(status=status_value, detail=detail, updated_at=now(),
                          project_path=str(self.project) if self.project else None, agent=self.agent,
                          writer_active=status_value in ACTIVE or writer_active(self.state), **extra)
        atomic_json(self.folder / "state.json", self.state)
        self.emit_callback(dict(self.state, type="state"))

    def check_cancel(self):
        if self.launcher_pid is not None and (self.launcher_pid <= 1 or os.getppid() != self.launcher_pid):
            self.launcher_lost = True
            self.cancelled = True
            raise Cancelled("Managed engine launcher exited unexpectedly.")
        request = read_json(self.folder / "cancel.json")
        if self.cancelled or (request and request.get("run_id") == self.state.get("run_id")):
            self.cancelled = True
            raise Cancelled("Stop requested.")

    def acquire_checkout(self, project):
        output = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel", "--absolute-git-dir"],
                                env=safe_env(), capture_output=True, text=True, timeout=15)
        if output.returncode or len(output.stdout.splitlines()) != 2:
            raise AutoError("Managed project must be an existing Git checkout with a commit.")
        root, git_dir = [Path(value).resolve() for value in output.stdout.splitlines()]
        if root != Path(project).expanduser().resolve():
            raise AutoError("Managed runs must use the Git checkout root.")
        self.state["checkout_git_dir"] = str(git_dir)
        lock = lock_file(git_dir / "pakati-managed-writer.lock")
        previous = self.checkout_lock
        self.checkout_lock = lock
        if previous:
            previous.close()
        self.project = root
        self.git_dir = git_dir
        self.state["checkout_git_dir"] = str(git_dir)

    def notes_path(self):
        path = self.project / ".agent-relay" / "notes.md"
        if path.is_symlink() or path.parent.is_symlink() or not path.is_file():
            raise AutoError("Save meaningful handoff notes in .agent-relay/notes.md before starting.")
        if path.stat().st_size > MAX_NOTES:
            raise AutoError("Handoff notes exceed the 128 KiB managed-run limit.")
        text = path.read_text(encoding="utf-8")
        if "<!-- agent-relay-template:" in text or len(text.strip()) < 20:
            raise AutoError("Replace the handoff notes template with the actual goal, progress and next steps before starting.")
        return path, text

    def configure(self):
        self.check_cancel()
        argv = engine_command("setup") + ["--project", str(self.project), "--store", str(self.store), "--task", self.args.task]
        self.relay_process(argv)
        self.notes_path()

    def relay_process(self, argv, structured=False, cancelling=False):
        # Setup is invoked through the same engine used by hook commands. Its
        # output is private bounded capture, never a usage-limit classifier.
        read_fd, write_fd = os.pipe()
        command = engine_command("auto") + ["_guard", "--lifeline-fd", str(read_fd), "--checkout-fd", str(self.checkout_lock.fileno()), "--", *argv]
        try:
            returncode, captured, excessive = bounded_process(command, 90, None if cancelling else self.check_cancel,
                                                             lambda process: setattr(self, "child", process),
                                                             (self.checkout_lock.fileno(), read_fd), self.project)
            if returncode:
                raise AutoError("Relay operation failed. Inspect this checkout's notes, settings and checkpoint owner before retrying.")
            if excessive:
                raise AutoError("Relay operation returned excessive output.")
        finally:
            self.child = None
            os.close(read_fd)
            os.close(write_fd)
        if not cancelling:
            self.check_cancel()
        if structured:
            try:
                value = json.loads(captured)
                if not isinstance(value, dict):
                    raise ValueError("not an object")
                return value
            except (ValueError, UnicodeError) as error:
                raise AutoError("Relay operation did not return valid JSON.") from error

    def snapshot(self, reason, required=True, cancelling=False):
        if not cancelling:
            self.check_cancel()
        path, _ = self.notes_path()
        self.update("checkpointing", "Saving the current checkout and latest handoff notes.", checkpoint_reason=reason)
        try:
            # Read the latest note file inside relay.checkpoint. Never write the
            # app's stale note buffer back to the project.
            self.initial_owner()
            argv = engine_command("checkpoint") + ["--project", str(self.project), "--store", str(self.store), "--task", self.args.task,
                                                   "--agent", self.agent, "--notes", str(path), "--expect-version", self.last_version]
            result = self.relay_process(argv, structured=True, cancelling=cancelling)
            self.last_version = result["version"]
            self.state["checkpoint_version"] = self.last_version
            self.emit("checkpoint_saved", detail="Checkpoint saved.", checkpoint_version=self.last_version,
                      excluded_untracked_count=len(result.get("excluded_untracked", [])))
        except (AutoError, relay.RelayError, OSError, UnicodeError) as error:
            if isinstance(error, Cancelled):
                raise
            if required:
                raise AutoError("Checkpoint failed; the checkout remains in place and no fallback was launched. " + str(error)) from error
            self.emit("checkpoint_deferred", detail="The checkout changed during its periodic snapshot, or its checkpoint owner changed. A final snapshot will be required before handoff.")
        if not cancelling:
            self.check_cancel()

    def initial_owner(self):
        task_path = relay.task_dir(self.store, self.args.task)
        latest = relay.latest_version(task_path)
        if latest:
            _, metadata, _ = relay.load_capsule(self.store, self.args.task, latest)
            saved = Path(metadata["project"]["path"]).resolve()
            head = subprocess.run(["git", "-C", str(self.project), "rev-parse", "HEAD"], env=safe_env(),
                                  capture_output=True, text=True, timeout=15)
            if head.returncode:
                raise AutoError("Cannot verify this checkout's HEAD.")
            current_head = head.stdout.strip()
            if saved == self.project:
                ancestor = subprocess.run(["git", "-C", str(self.project), "merge-base", "--is-ancestor", metadata["project"]["head"], current_head],
                                          env=safe_env(), capture_output=True, timeout=15)
                if ancestor.returncode:
                    raise AutoError("This checkout is older than or diverged from the latest checkpoint. Adopt it explicitly before starting.")
            else:
                provenance = read_json(self.git_dir / "agent-relay-restore.json")
                if not (provenance and provenance.get("task") == self.args.task and provenance.get("version") == latest and
                        Path(provenance.get("store", "/invalid")).resolve() == self.store and provenance.get("head") == current_head):
                    raise AutoError("Another checkout owns the latest checkpoint. Restore and adopt it explicitly before starting here.")
        self.last_version = latest or "none"

    def prompt(self):
        _, text = self.notes_path()
        return ("The user has started a Pakati managed run to continue this task. Follow the project rules and the current task notes below. "
                "Continue the goal within your normal tool permissions. Keep .agent-relay/notes.md accurate after milestones, with progress, "
                "decisions, tests, blockers and next steps so another assistant can continue. Do not launch another assistant or change Pakati's "
                "managed state. Do not deliberately detach tools into new process sessions or leave independent project writers running: "
                "handoff depends on settling your tools before another assistant starts. If a permission blocks progress, report the blocker. "
                "A final response ends this managed run.\n\n" + text)

    def command(self, path):
        if self.agent == "codex":
            return [path, "--ask-for-approval", "never", "exec", "--json", "--sandbox", "workspace-write", "-"]
        return [path, "-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits", "--permission-prompts", "none"]

    def run_agent(self, path):
        self.check_cancel()
        life = Lifecycle(self.agent)
        prompt = self.prompt().encode("utf-8")
        read_fd, write_fd = os.pipe()
        command = engine_command("auto") + ["_guard", "--lifeline-fd", str(read_fd), "--checkout-fd", str(self.checkout_lock.fileno()),
                                             "--", *self.command(path)]
        try:
            process = subprocess.Popen(command, cwd=self.project, env=safe_env(), stdin=subprocess.PIPE,
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True,
                                   pass_fds=(self.checkout_lock.fileno(), read_fd))
        except BaseException:
            os.close(write_fd)
            raise
        finally:
            os.close(read_fd)
        self.lifeline = write_fd
        self.child = process
        self.update("running", "Assistant CLI is working in this checkout.", agent_pid=process.pid)
        selector = selectors.DefaultSelector()
        buffers = {"stdout": bytearray(), "stderr": bytearray()}
        dropped_line = False
        next_snapshot = time.monotonic() + self.snapshot_seconds
        stdin_data = prompt
        stdin_offset = 0
        for name, stream in (("stdout", process.stdout), ("stderr", process.stderr)):
            os.set_blocking(stream.fileno(), False)
            selector.register(stream, selectors.EVENT_READ, name)
        os.set_blocking(process.stdin.fileno(), False)
        selector.register(process.stdin, selectors.EVENT_WRITE, "stdin")
        exit_at = None
        try:
            while selector.get_map():
                self.check_cancel()
                returncode = process.poll()
                if returncode is not None and exit_at is None:
                    # Tools/background shells must stop before their files are
                    # snapshotted or a replacement writer is allowed to start.
                    settle_group(process)
                    exit_at = time.monotonic()
                if exit_at is not None and time.monotonic() - exit_at > 3:
                    break
                if returncode is None and time.monotonic() >= next_snapshot:
                    self.snapshot("periodic", required=False)
                    self.update("running", "Assistant CLI is working in this checkout.")
                    next_snapshot = time.monotonic() + self.snapshot_seconds
                for key, mask in selector.select(0.1):
                    name = key.data
                    if name == "stdin":
                        try:
                            size = os.write(key.fd, stdin_data[stdin_offset:stdin_offset + 16384])
                            stdin_offset += size
                        except BrokenPipeError:
                            stdin_offset = len(stdin_data)
                        if stdin_offset >= len(stdin_data):
                            selector.unregister(key.fileobj)
                            key.fileobj.close()
                        continue
                    chunk = os.read(key.fd, 16384)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    if name == "stderr":
                        buffers[name].extend(chunk)
                        del buffers[name][:-MAX_CAPTURE]
                        continue
                    for byte in chunk.splitlines(keepends=True):
                        if not dropped_line:
                            buffers[name].extend(byte)
                            if len(buffers[name]) > MAX_LINE:
                                buffers[name].clear()
                                dropped_line = True
                        if byte.endswith(b"\n"):
                            if not dropped_line:
                                try:
                                    life.accept(json.loads(buffers[name]))
                                except (ValueError, UnicodeError):
                                    pass
                            buffers[name].clear()
                            dropped_line = False
            if buffers["stdout"] and not dropped_line:
                try:
                    life.accept(json.loads(buffers["stdout"]))
                except (ValueError, UnicodeError):
                    pass
            if process.poll() is None:
                while process.poll() is None:
                    self.check_cancel()
                    time.sleep(0.05)
            settle_group(process)
            self.check_cancel()
            self.emit("assistant_exit", detail="Assistant CLI exited; its process group has stopped.", exit_code=process.returncode,
                      lifecycle_events=life.events)
            return life.outcome(process.returncode)
        finally:
            selector.close()
            settle_group(process)
            for stream in (process.stdin, process.stdout, process.stderr):
                if stream and not stream.closed:
                    stream.close()
            self.child = None
            self.state["agent_pid"] = None
            os.close(write_fd)
            self.lifeline = None

    def handoff(self):
        self.check_cancel()
        self.update("switching", "Restoring a fresh checkout for the other assistant.")
        next_agent = "claude" if self.agent == "codex" else "codex"
        target = self.handoff_root / (self.args.task + "-" + self.state["run_id"][:12] + "-" + str(self.state["switch_count"] + 1) + "-" + next_agent)
        argv = engine_command("restore") + ["--store", str(self.store), "--task", self.args.task, "--version", self.last_version, "--into", str(target)]
        restored = self.relay_process(argv, structured=True)
        self.check_cancel()
        self.acquire_checkout(restored["project"])
        self.agent = next_agent
        self.state["switch_count"] += 1
        self.configure()
        self.snapshot("adoption")
        self.check_cancel()
        self.emit("handoff_ready", detail="The new checkout is configured and its adoption checkpoint is saved.")

    def validate_paths(self, raw_handoff):
        for component in [*reversed(raw_handoff.parents), raw_handoff]:
            if component.is_symlink():
                raise AutoError("Handoff directories must not contain symlinks.")
        if inside(self.store, self.project) or inside(self.handoff_root, self.project) or inside(self.project, self.handoff_root):
            raise AutoError("Checkpoint store and handoff root must be outside the source checkout; the checkout must not be inside the handoff root.")
        if inside(self.store, self.handoff_root) or inside(self.handoff_root, self.store):
            raise AutoError("Checkpoint store and handoff root must be separate directories.")

    def run(self):
        if not 0 <= self.args.max_switches <= 10:
            raise AutoError("--max-switches must be between 0 and 10.")
        self.store, self.folder = task_paths(self.args.store, self.args.task)
        with lock_file(self.folder / "run.lock"):
            prior = read_json(self.folder / "state.json")
            if prior:
                orphan = prior.get("agent_pid") or prior.get("orphaned_group_pid")
                if isinstance(orphan, int) and orphan > 1 and group_is_live(orphan):
                    raise AutoError("A previous agent process group may still be running. Review and stop it explicitly before starting another writer.")
            self.state = {"schema_version": 1, "task": self.args.task, "run_id": uuid.uuid4().hex,
                          "pid": os.getpid(), "agent_pid": None, "switch_count": 0, "started_at": now(), "unavailable_agents": []}
            self.project = Path(self.args.project).expanduser().resolve()
            raw_handoff = Path(self.args.handoff_root).expanduser().absolute()
            self.handoff_root = raw_handoff.resolve()
            previous_handlers = {}
            for sig in (signal.SIGTERM, signal.SIGINT):
                previous_handlers[sig] = signal.getsignal(sig)
                signal.signal(sig, lambda signum, frame: setattr(self, "cancelled", True))
            try:
                self.update("running", "Checking both CLI installations and this checkout.")
                self.validate_paths(raw_handoff)
                checked = preflight(self.args.codex_cli, self.args.claude_cli, self.check_cancel)
                if not checked["ready"]:
                    raise AutoError(checked["detail"])
                self.check_cancel()
                self.acquire_checkout(self.project)
                self.handoff_root = secure_directory(self.handoff_root)
                self.notes_path()
                self.initial_owner()
                self.configure()
                self.snapshot("initial")
                while True:
                    self.check_cancel()
                    outcome = self.run_agent(checked["agents"][self.agent]["path"])
                    self.snapshot("final")
                    if outcome == "success":
                        self.update("completed", "Assistant response complete. Review the result and notes; full task completion has not been independently verified.", pid=None)
                        return 0
                    if outcome == "permission":
                        self.update("blocked", "A tool permission was denied. Review permissions and continue explicitly; no automatic handoff was started.", pid=None)
                        return 2
                    if outcome != "quota":
                        self.update("failed", "Assistant CLI ended without a successful result or a verified account usage-limit failure. No automatic handoff was started.", pid=None)
                        return 2
                    self.state["unavailable_agents"].append(self.agent)
                    other = "claude" if self.agent == "codex" else "codex"
                    if self.state["switch_count"] >= self.args.max_switches or other in self.state["unavailable_agents"]:
                        self.update("blocked", "Account usage is unavailable or the automatic switch limit was reached. The final checkpoint is saved; continue explicitly when usage returns.", pid=None)
                        return 2
                    self.handoff()
            except Cancelled:
                if self.child:
                    settle_group(self.child)
                    self.child = None
                if self.checkout_lock:
                    with contextlib.suppress(AutoError, OSError, relay.RelayError):
                        self.snapshot("stopped", cancelling=True)
                if self.launcher_lost:
                    self.update("interrupted", "The engine launcher ended unexpectedly. Its agent has stopped; inspect the checkout and latest checkpoint before continuing.", pid=None, agent_pid=None)
                else:
                    self.update("stopped", "Managed run stopped. The checkout remains available; inspect its latest checkpoint and notes before continuing.", pid=None, agent_pid=None)
                return 0
            except (AutoError, OSError, relay.RelayError, UnicodeError, subprocess.SubprocessError) as error:
                if self.child:
                    settle_group(self.child)
                    self.child = None
                self.update("blocked", str(error)[:1000], pid=None, agent_pid=None)
                return 2
            finally:
                if self.checkout_lock:
                    self.checkout_lock.close()
                    self.checkout_lock = None
                for sig, handler in previous_handlers.items():
                    signal.signal(sig, handler)


def parser():
    result = argparse.ArgumentParser(description="Pakati managed automatic handoff. Uses installed CLI account logins; never bypasses tool permissions.")
    sub = result.add_subparsers(dest="command", required=True)
    ready = sub.add_parser("preflight", help="Verify both CLI installations without starting a model.")
    run = sub.add_parser("run", help="Continue saved task notes, switching only after verified account quota failure.")
    for command in (ready, run):
        command.add_argument("--codex-cli")
        command.add_argument("--claude-cli")
    for command in (run, sub.add_parser("status"), sub.add_parser("stop")):
        command.add_argument("--task", required=True)
        command.add_argument("--store", required=True)
    run.add_argument("--project", required=True)
    run.add_argument("--agent", choices=("codex", "claude"), required=True)
    run.add_argument("--handoff-root", required=True)
    run.add_argument("--max-switches", type=int, default=1)
    return result


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "_guard":
        return guard(sys.argv[2:])
    args = parser().parse_args()
    try:
        if args.command == "preflight":
            result = preflight(args.codex_cli, args.claude_cli)
            print(json.dumps(result, ensure_ascii=False))
            return 0 if result["ready"] else 2
        if args.command == "status":
            print(json.dumps(status(args.task, args.store), ensure_ascii=False))
            return 0
        if args.command == "stop":
            print(json.dumps(stop(args.task, args.store), ensure_ascii=False))
            return 0
        return Supervisor(args).run()
    except (AutoError, OSError, relay.RelayError) as error:
        state = {"type": "state", "status": "blocked", "task": getattr(args, "task", None),
                 "detail": str(error)[:1000], "pid": None, "writer_active": True}
        if getattr(args, "task", None) and getattr(args, "store", None):
            with contextlib.suppress(AutoError, OSError):
                state.update(status(args.task, args.store))
                state.update(type="state", detail=str(error)[:1000])
        print(json.dumps(state), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
