#!/usr/bin/env python3
"""Exercise managed handoff through a relocated frozen Pakati engine.

Requires --engine and --scratch. Every repository, CLI, configuration, and
checkpoint is a disposable local fixture. The fake CLIs make no model requests.
Test-runner Python is never used by the packaged engine or its generated hooks.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
from pathlib import Path
import shutil
import shlex
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ENGINE = None
SCRATCH = None
EXPECTED_VERSION = None
ACTIVE = {"running", "checkpointing", "switching"}

CODEX_OK = [{"type": "turn.completed"}]
CODEX_QUOTA = [{"type": "turn.failed", "error": {
    "code": "usage_limit_exceeded", "message": "You've hit your usage limit."}}]
CLAUDE_OK = [{"type": "result", "subtype": "success", "is_error": False,
              "terminal_reason": "completed"}]
CLAUDE_QUOTA = [
    {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "errorCode": "credits_required"}},
    {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"},
    {"type": "result", "subtype": "success", "is_error": True,
     "terminal_reason": "api_error", "api_error_status": 429},
]

FAKE_CLI = r'''
import json, os, pathlib, subprocess, sys, time
config = json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())
if '--version' in sys.argv:
    print('Pakati fake CLI 1.0'); sys.exit(0)
if '--help' in sys.argv:
    print('--ask-for-approval --json --sandbox --output-format --verbose --permission-mode --permission-prompts'); sys.exit(0)
prompt = sys.stdin.read()
cwd = pathlib.Path.cwd()
assert '# Goal' in prompt and 'packaged fixture' in prompt, 'saved task notes must be supplied'
assert not any(k.startswith('GIT_') for k in os.environ), 'inherited Git redirects must be removed'
assert not any(k in os.environ for k in ('CODEX_API_KEY','OPENAI_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN')), 'API keys must not be forwarded'
with pathlib.Path(config['log']).open('a') as output:
    output.write(json.dumps({'agent':config['agent'],'project':str(cwd),'pid':os.getpid(),'pgid':os.getpgrp()})+'\n')
task = json.loads((cwd/'.agent-relay.json').read_text())
pointer = json.loads((pathlib.Path(task['store'])/'tasks'/task['task']/'latest.json').read_text())
meta = json.loads((pathlib.Path(task['store'])/'tasks'/task['task']/'versions'/pointer['version']/'metadata.json').read_text())
assert pathlib.Path(meta['project']['path']).resolve() == cwd.resolve(), 'adoption must be saved before launch'
if config.get('edit'):
    (cwd/'code.txt').write_text('staged packaged progress\n')
    subprocess.run(['git','add','code.txt'],check=True)
    (cwd/'code.txt').write_text('staged packaged progress\nplus unstaged progress\n')
    (cwd/'pending.txt').write_text('untracked packaged progress\n')
    (cwd/'.agent-relay/notes.md').write_text('# Goal\nFinish the packaged fixture.\n# Progress\nFake CLI saved code and notes.\n# Next\nCheck the transferred index and pending file.\n')
if config.get('stop_hook'):
    hooks = json.loads((cwd/'.codex/hooks.json').read_text())
    command = hooks['hooks']['Stop'][0]['hooks'][0]['command']
    result = subprocess.run(['/bin/sh','-c',command],input=json.dumps({'hook_event_name':'Stop','cwd':str(cwd)}),text=True,capture_output=True)
    assert result.returncode == 0 and 'advisory:' not in result.stderr, result.stderr
if config.get('child'):
    code = "import json,os,pathlib,signal,sys,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); pathlib.Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()})); time.sleep(120)"
    subprocess.Popen([sys.executable,'-c',code,config['child']])
if config.get('huge'):
    sys.stderr.write('fixture-stderr-'*30000); sys.stderr.flush()
    print('fixture-too-large-'*70000,flush=True)
if config.get('sleep'):
    time.sleep(config['sleep'])
for event in config['events']:
    print(json.dumps(event),flush=True)
sys.exit(config.get('exit',0))
'''


def fixture_environment():
    result = {k: v for k, v in os.environ.items() if not k.startswith("GIT_") and
              not k.startswith("_PYI_") and k not in {"CLAUDECODE", "PYINSTALLER_RESET_ENVIRONMENT",
              "OPENAI_API_KEY", "CODEX_API_KEY", "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"}}
    result["PATH"] = "/usr/bin:/bin:/usr/sbin:/sbin"
    return result


def live(pid):
    if not isinstance(pid, int) or pid < 2:
        return False
    result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="],
                            capture_output=True, text=True, timeout=5)
    return result.returncode == 0 and bool(result.stdout.strip()) and not result.stdout.strip().startswith("Z")


class PackagedManagedTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.installation = tempfile.TemporaryDirectory(prefix="pakati-auto-frozen-", dir=SCRATCH)
        cls.installation_root = Path(cls.installation.name)
        folder = cls.installation_root / "installed helper with spaces '$()"
        folder.mkdir()
        cls.engine = folder / "relay-engine"
        shutil.copyfile(ENGINE, cls.engine)
        cls.engine.chmod(0o700)

    @classmethod
    def tearDownClass(cls):
        cls.installation.cleanup()

    def setUp(self):
        self.fixture = tempfile.TemporaryDirectory(prefix="fixture-", dir=self.installation_root)
        self.addCleanup(self.fixture.cleanup)
        self.root = Path(self.fixture.name)
        self.project = self.root / "project folder '$()"
        self.project.mkdir()
        self.store = self.root / "checkpoint store"
        self.handoffs = self.root / "new checkouts"
        self.log = self.root / "launches.jsonl"
        self.child = self.root / "child.json"
        self.env = fixture_environment()
        self.git("init", "-q")
        self.git("config", "user.name", "Pakati Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.project / "code.txt").write_text("base\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "Packaged fixture base")
        notes = self.project / ".agent-relay" / "notes.md"
        notes.parent.mkdir()
        notes.write_text("# Goal\nFinish the packaged fixture.\n# Progress\nBase committed.\n# Next\nComplete and verify.\n")
        notes.chmod(0o600)
        self.codex = self.fake("codex", CODEX_OK)
        self.claude = self.fake("claude", CLAUDE_OK)

    def git(self, *args, project=None):
        result = subprocess.run(["git", "-C", str(project or self.project), *args],
                                env=self.env, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def fake(self, agent, events, **options):
        script = self.root / (agent + " fake CLI '$().py")
        script.write_text("#!" + sys.executable + "\n" + FAKE_CLI)
        script.chmod(0o700)
        script.with_suffix(".json").write_text(json.dumps({
            "agent": agent, "events": events, "log": str(self.log), **options}))
        return script

    def cli(self, *args, ok=True, timeout=45):
        result = subprocess.run([str(self.engine), *map(str, args)], env=self.env,
                                capture_output=True, text=True, timeout=timeout)
        if ok:
            self.assertEqual(result.returncode, 0, result.stderr + "\n" + result.stdout)
        return result

    def run_command(self, agent="codex", task="fixture", max_switches=1, store=None):
        return [str(self.engine), "auto", "run", "--task", task, "--store", str(store or self.store),
                "--project", str(self.project), "--handoff-root", str(self.handoffs),
                "--agent", agent, "--codex-cli", str(self.codex), "--claude-cli", str(self.claude),
                "--max-switches", str(max_switches)]

    def run_managed(self, **options):
        result = subprocess.run(self.run_command(**options), env=self.env,
                                capture_output=True, text=True, timeout=90)
        events = [json.loads(line) for line in result.stdout.splitlines()]
        self.assertTrue(all(event.get("type") in {"state", "event"} for event in events), result.stdout)
        return result, events, self.status()

    def spawn(self, **options):
        return subprocess.Popen(self.run_command(**options), env=self.env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                text=True, start_new_session=True)

    def status(self):
        return json.loads(self.cli("auto", "status", "--task", "fixture", "--store", self.store).stdout)

    def wait_running(self, process):
        state_path = self.store / ".autopilot" / "fixture" / "state.json"
        until = time.monotonic() + 35
        state = {}
        while time.monotonic() < until:
            if state_path.exists():
                state = json.loads(state_path.read_text())
                if state.get("agent_pid") and self.child.exists():
                    return state
            if process.poll() is not None:
                out, err = process.communicate(timeout=5)
                self.fail("Worker exited before fake agent started: " + out + "\n" + err)
            time.sleep(0.05)
        self.fail("Fake agent did not start: " + repr(state))

    def launches(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def clean_worker(self, process, state=None):
        # These are fixture PIDs captured directly from our just-created run.
        state = state or {}
        child = json.loads(self.child.read_text()) if self.child.exists() else {}
        for pid in {state.get("pid"), child.get("pid")}:
            if live(pid):
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.kill(pid, signal.SIGKILL)
        for group in {process.pid, state.get("agent_pid"), child.get("pgid")}:
            if isinstance(group, int) and group > 1:
                with contextlib.suppress(ProcessLookupError, PermissionError):
                    os.killpg(group, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.communicate(timeout=8)
        if process.stdout:
            process.stdout.close()
        if process.stderr:
            process.stderr.close()

    def test_relocated_helper_is_frozen_and_preflight_works_without_python_on_path(self):
        info = json.loads(self.cli("preflight").stdout)
        self.assertTrue(info["bundled_python"])
        if EXPECTED_VERSION is not None:
            self.assertEqual(info["version"], EXPECTED_VERSION)
        ready = json.loads(self.cli("auto", "preflight", "--codex-cli", self.codex,
                                    "--claude-cli", self.claude).stdout)
        self.assertTrue(ready["ready"])
        self.assertEqual(self.launches(), [])

    def test_quota_handoff_preserves_work_notes_and_frozen_stop_hook(self):
        self.codex = self.fake("codex", CODEX_QUOTA, edit=True, stop_hook=True, exit=1)
        self.env["OPENAI_API_KEY"] = "fixture-not-a-secret"
        self.env["GIT_DIR"] = "/fixture-invalid-git-dir"
        result, events, state = self.run_managed()
        self.assertEqual(result.returncode, 0, result.stderr + repr(state))
        self.assertEqual(state["status"], "completed")
        self.assertFalse(state["writer_active"])
        self.assertEqual(state["switch_count"], 1)
        launched = self.launches()
        self.assertEqual([item["agent"] for item in launched], ["codex", "claude"])
        restored = Path(state["project_path"])
        self.assertNotEqual(restored, self.project)
        self.assertEqual(Path(launched[-1]["project"]), restored)
        self.assertEqual((restored / "code.txt").read_text(), "staged packaged progress\nplus unstaged progress\n")
        # The runner environment deliberately contains a Git redirect; the
        # fixture verification command uses a clean environment instead.
        self.env.pop("GIT_DIR")
        self.assertEqual(self.git("show", ":code.txt", project=restored), "staged packaged progress")
        self.assertEqual((restored / "pending.txt").read_text(), "untracked packaged progress\n")
        self.assertIn("Fake CLI saved code and notes", (restored / ".agent-relay/notes.md").read_text())
        shown = json.loads(self.cli("show", "--task", "fixture", "--store", self.store).stdout)
        self.assertEqual(Path(shown["metadata"]["project"]["path"]), restored)
        self.assertIn("Fake CLI saved code and notes", shown["notes"])
        command = json.loads((restored / ".codex/hooks.json").read_text())["hooks"]["Stop"][0]["hooks"][0]["command"]
        self.assertEqual(Path(shlex.split(command)[0]), self.engine)
        self.assertNotIn("engine.py", command)
        self.assertIn("handoff_ready", [event.get("event") for event in events])

    def test_claude_subscription_quota_can_handoff_to_codex(self):
        self.claude = self.fake("claude", CLAUDE_QUOTA, edit=True, exit=1)
        result, _, state = self.run_managed(agent="claude")
        self.assertEqual(result.returncode, 0, result.stderr + repr(state))
        self.assertEqual(state["status"], "completed")
        self.assertEqual([item["agent"] for item in self.launches()], ["claude", "codex"])
        self.assertEqual(state["switch_count"], 1)

    def test_both_quota_failures_never_ping_pong(self):
        self.codex = self.fake("codex", CODEX_QUOTA, exit=1)
        self.claude = self.fake("claude", CLAUDE_QUOTA, exit=1)
        result, _, state = self.run_managed(max_switches=10)
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["unavailable_agents"], ["codex", "claude"])
        self.assertEqual([item["agent"] for item in self.launches()], ["codex", "claude"])
        self.assertFalse(state["writer_active"])

    def test_plain_rate_limit_and_tool_text_do_not_switch(self):
        self.claude = self.fake("claude", [
            {"type": "assistant", "error": "rate_limit", "parent_tool_use_id": None},
            {"type": "result", "subtype": "success", "is_error": True,
             "terminal_reason": "api_error", "api_error_status": 429},
        ], exit=1)
        result, _, state = self.run_managed(agent="claude")
        self.assertEqual(result.returncode, 2)
        self.assertEqual(state["status"], "failed")
        self.assertEqual([item["agent"] for item in self.launches()], ["claude"])
        self.log.unlink()
        self.codex = self.fake("codex", [
            {"type": "item.completed", "item": {"type": "command_execution",
                "aggregated_output": "You've hit your usage limit."}},
            {"type": "turn.completed"},
        ])
        result, _, state = self.run_managed()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(state["status"], "completed")
        self.assertEqual([item["agent"] for item in self.launches()], ["codex"])

    def test_high_volume_agent_output_is_drained_without_transcripts(self):
        self.codex = self.fake("codex", CODEX_OK, huge=True)
        result, events, state = self.run_managed()
        self.assertEqual(result.returncode, 0, result.stderr + repr(state))
        self.assertEqual(state["status"], "completed")
        self.assertNotIn("fixture-stderr-", result.stdout + result.stderr)
        self.assertNotIn("fixture-too-large-", result.stdout + result.stderr)
        self.assertFalse(any("message" in event or "transcript" in event for event in events))

    def test_duplicate_writer_rejected_and_stop_settles_descendants(self):
        self.codex = self.fake("codex", CODEX_OK, child=str(self.child), sleep=120)
        process = self.spawn()
        state = None
        try:
            state = self.wait_running(process)
            duplicate = subprocess.run(self.run_command(task="another", store=self.root / "another-store"),
                                       env=self.env, capture_output=True, text=True, timeout=35)
            self.assertEqual(duplicate.returncode, 2, duplicate.stderr)
            self.assertIn("already owns", duplicate.stdout)
            self.cli("auto", "stop", "--task", "fixture", "--store", self.store)
            out, err = process.communicate(timeout=35)
            self.assertEqual(process.returncode, 0, err + out)
            final = self.status()
            self.assertEqual(final["status"], "stopped")
            self.assertFalse(final["writer_active"])
            self.assertFalse(live(json.loads(self.child.read_text())["pid"]))
            self.assertFalse(live(state["agent_pid"]))
        finally:
            self.clean_worker(process, state)

    def hard_crash(self, outer):
        self.codex = self.fake("codex", CODEX_OK, child=str(self.child), sleep=120)
        process = self.spawn()
        state = None
        try:
            state = self.wait_running(process)
            target = process.pid if outer else state["pid"]
            self.assertTrue(live(target))
            os.kill(target, signal.SIGKILL)
            until = time.monotonic() + 12
            descendants = [state["pid"], state["agent_pid"], json.loads(self.child.read_text())["pid"]]
            while any(live(pid) for pid in descendants) and time.monotonic() < until:
                time.sleep(0.05)
            survivors = [pid for pid in descendants if live(pid)]
            self.assertEqual(survivors, [], "Frozen supervisor/agent survived hard crash: " + repr(survivors))
            process.communicate(timeout=12)
            final = self.status()
            self.assertEqual(final["status"], "interrupted")
            self.assertFalse(final["writer_active"])
        finally:
            self.clean_worker(process, state)

    def test_hard_application_supervisor_crash_stops_writer(self):
        self.hard_crash(outer=False)

    def test_hard_outer_bootloader_crash_stops_writer(self):
        self.hard_crash(outer=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", required=True, type=Path)
    parser.add_argument("--scratch", required=True, type=Path)
    parser.add_argument("--expected-version")
    options, extra = parser.parse_known_args()
    ENGINE = options.engine.expanduser().resolve()
    if not ENGINE.is_file() or not os.access(ENGINE, os.X_OK):
        parser.error("--engine must be an executable frozen relay-engine")
    SCRATCH = options.scratch.expanduser().resolve()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    EXPECTED_VERSION = options.expected_version
    unittest.main(argv=[sys.argv[0], *extra], verbosity=2)
