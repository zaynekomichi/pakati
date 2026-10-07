#!/usr/bin/env python3
"""Opt-in project installer for the Pakati prototype; no home-folder changes."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import shlex
import subprocess
import sys

from commands import engine_command

BEGIN = "<!-- agent-relay:begin -->"
END = "<!-- agent-relay:end -->"
HOOK_MARKER = "--relay-hook-v1"
TEMPLATE = """<!-- agent-relay-template: replace this comment and the prompts with real task notes. -->
# Task handoff

Goal and user constraints:

Completed changes and relevant files:

Decisions and reasons:

Commands/tests run and observed results:

Next concrete steps:

Blockers or user input needed:

Never put secrets, credentials, private reasoning, or transcript dumps here.
"""


def load_json(path: Path) -> dict:
    if path.is_symlink():
        raise ValueError(f"Refusing to modify a symlink: {path}")
    data = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected JSON object in {path}")
    return data


def instruction_block(task: str, store: Path) -> str:
    relay = shlex.join(engine_command("show")[:-1])
    store_arg = shlex.quote(str(store))
    return f"""{BEGIN}
## Pakati task handoff

This checkout is configured for task `{task}` in `.agent-relay.json`. The current user's request takes priority. Continue this saved task only when asked to continue it. If starting unrelated work, set `enabled` to false in that config or configure a different task before changing files.

On continuation, run `{relay} show --task {task} --store {store_arg}` and read the saved notes. Verify this checkout has the latest unfinished code. If another checkout owns the latest capsule or HEAD differs, restore into a new, nonexistent checkout using `{relay} restore --task {task} --store {store_arg} --into NEW_CHECKOUT_PATH`; preserve this checkout. After restore, checkpoint explicitly before the first commit to adopt it.

Keep `.agent-relay/notes.md` current after meaningful milestones: goal, constraints, changes, decisions, commands/tests and actual results, next steps, blockers. Before the first checkpoint, fill its template with real notes and remove the `agent-relay-template` comment. Include no secrets, private reasoning, or transcript dumps. Before switching assistants or finishing, update notes then run `{relay} checkpoint --task {task} --project . --agent AGENT --notes .agent-relay/notes.md --store {store_arg}` (`AGENT` is `codex` or `claude`).

Hooks snapshot files after tools at most once per minute and again at normal Stop; Claude also snapshots documented `rate_limit` failures. Hooks reuse notes unless you update them. Hooks never transfer a live session, launch agents, or guarantee a final snapshot on crashes or every usage-limit screen. Preserve project-specific rules above this block.
{END}
"""


def append_block(path: Path, block: str) -> str:
    if path.is_symlink():
        raise ValueError(f"Refusing to modify a symlink: {path}")
    text = path.read_text() if path.exists() else ""
    if text.count(BEGIN) != text.count(END) or text.count(BEGIN) > 1:
        raise ValueError(f"Malformed Pakati instruction markers in {path}")
    if BEGIN in text:
        start, end = text.index(BEGIN), text.index(END) + len(END)
        return text[:start] + block.rstrip() + text[end:]
    return text + ("\n\n" if text and not text.endswith("\n\n") else "") + block


def merge_hooks(config: dict, agent: str) -> dict:
    hooks = config.setdefault("hooks", {})
    if not isinstance(hooks, dict):
        raise ValueError("Existing hooks value must be an object")
    command = shlex.join([*engine_command("hook"), "--agent", agent, HOOK_MARKER])
    events = ["SessionStart", "PostToolUse", "Stop"]
    if agent == "claude":
        events.append("StopFailure")
    for event in events:
        groups = hooks.setdefault(event, [])
        if not isinstance(groups, list):
            raise ValueError(f"Existing hooks.{event} must be an array")
        # Only replace our own handlers; preserve every unrelated group/field.
        retained = []
        for group in groups:
            if not isinstance(group, dict) or not isinstance(group.get("hooks"), list):
                raise ValueError(f"Invalid matcher group in hooks.{event}")
            owned = lambda handler: (isinstance(handler, dict) and
                                     HOOK_MARKER in str(handler.get("command", "")))
            if any(owned(handler) for handler in group["hooks"]):
                other = [handler for handler in group["hooks"] if not owned(handler)]
                if other:
                    retained.append({**group, "hooks": other})
            else:
                retained.append(group)
        handler = {"type": "command", "command": command, "timeout": 90}
        if agent == "codex":
            handler["statusMessage"] = "Pakati: loading task notes" if event == "SessionStart" else "Pakati: saving checkpoint"
        group = {"hooks": [handler]}
        if event == "StopFailure":
            group["matcher"] = "rate_limit"
        retained.append(group)
        hooks[event] = retained
    return config


def atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(content)
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def reject_symlink_parents(root: Path, path: Path) -> None:
    for parent in (path, *path.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise ValueError(f"Refusing a project path with a symlink: {parent}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", required=True, type=Path)
    parser.add_argument("--store", required=True, type=Path,
                        help="Absolute capsule directory outside the supplied checkout")
    parser.add_argument("--task", required=True)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", args.task):
            raise ValueError("Task must contain 1–64 letters, numbers, underscores, or hyphens")
        project = args.project.expanduser().resolve(strict=True)
        result = subprocess.run(["git", "-C", str(project), "rev-parse", "--show-toplevel"],
                                capture_output=True, text=True, timeout=15, check=True)
        root = Path(result.stdout.strip()).resolve()
        if root != project:
            raise ValueError(f"Supply the Git checkout root: {root}")
        supplied_store = args.store.expanduser()
        if not supplied_store.is_absolute():
            raise ValueError("--store must be an absolute path")
        store = supplied_store.resolve()
        if store.is_relative_to(root):
            raise ValueError("--store must be outside the checkout")
        if store.exists() and not store.is_dir():
            raise ValueError("--store must name a directory")
        # A symlinked settings directory could otherwise modify a home config.
        for relative in ("AGENTS.md", "CLAUDE.md", ".agent-relay.json", ".agent-relay/notes.md",
                         ".codex/hooks.json", ".claude/settings.json"):
            reject_symlink_parents(root, root / relative)
        block = instruction_block(args.task, store)
        plans = {}
        for name in ("AGENTS.md", "CLAUDE.md"):
            path = root / name
            plans[path] = append_block(path, block)
            if name == "CLAUDE.md" and not path.exists():
                # Creating CLAUDE.md can replace Claude's fallback to AGENTS.md.
                # Import it so the user's existing project rules remain visible.
                plans[path] = "@AGENTS.md\n\n" + plans[path]
        relay_config_path = root / ".agent-relay.json"
        relay_config = load_json(relay_config_path)
        relay_config.update({"schema_version": 1, "task": args.task, "store": str(store),
                             "notes": ".agent-relay/notes.md", "enabled": True})
        plans[relay_config_path] = json.dumps(relay_config, indent=2) + "\n"
        for name, agent in ((".codex/hooks.json", "codex"), (".claude/settings.json", "claude")):
            path = root / name
            plans[path] = json.dumps(merge_hooks(load_json(path), agent), indent=2) + "\n"
        notes = root / ".agent-relay/notes.md"
        if notes.is_symlink():
            raise ValueError(f"Refusing a notes symlink: {notes}")
        if not notes.exists():
            plans[notes] = TEMPLATE
        # All parsing/validation occurs before the first project mutation.
        for path, content in plans.items():
            atomic_write(path, content)
        print(f"Configured Pakati task {args.task} in {root}")
        print(f"Shared capsule store: {store}")
        print("Review the appended rules and hook configuration, then reopen the assistant session.")
        print("Fill .agent-relay/notes.md and remove its template comment before the first checkpoint. "
              "Files were not gitignored or committed.")
        if (root / "AGENTS.override.md").exists():
            print("Warning: AGENTS.override.md exists and can supersede AGENTS.md. Add the relay "
                  "handoff instructions there if desired; the installer preserved it.", file=sys.stderr)
        return 0
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        print(f"relay setup: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
