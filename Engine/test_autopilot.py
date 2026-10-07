#!/usr/bin/env python3
"""Managed handoff regressions. Fake CLIs only; no accounts or model requests."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest

import autopilot
import relay

ENGINE = Path(__file__).with_name("engine.py").resolve()
FAKE = r'''
import json, os, pathlib, subprocess, sys, time
config = json.loads(pathlib.Path(__file__).with_suffix('.json').read_text())
if '--version' in sys.argv:
    print('fake-cli 99.0'); sys.exit(0)
if '--help' in sys.argv:
    print('--ask-for-approval --json --sandbox --output-format --verbose --permission-mode --permission-prompts'); sys.exit(0)
prompt = sys.stdin.read()
cwd = pathlib.Path.cwd()
with open(config['log'], 'a') as output:
    output.write(json.dumps({'agent':config['agent'], 'project':str(cwd), 'argv':sys.argv[1:],
                             'git_env': [k for k in os.environ if k.startswith('GIT_')],
                             'keys': [k for k in ['OPENAI_API_KEY','CODEX_API_KEY','ANTHROPIC_API_KEY','ANTHROPIC_AUTH_TOKEN'] if k in os.environ]})+'\n')
if config.get('assert_adoption'):
    task = json.loads((cwd/'.agent-relay.json').read_text())
    pointer = json.loads((pathlib.Path(task['store'])/'tasks'/task['task']/'latest.json').read_text())
    meta = json.loads((pathlib.Path(task['store'])/'tasks'/task['task']/'versions'/pointer['version']/'metadata.json').read_text())
    assert pathlib.Path(meta['project']['path']).resolve()==cwd.resolve(), 'adoption must precede CLI launch'
if config.get('edit'):
    (cwd/'code.txt').write_text('staged by fake CLI\n')
    subprocess.run(['git','add','code.txt'],check=True)
    (cwd/'code.txt').write_text('unstaged by fake CLI\n')
    (cwd/'new.txt').write_text('untracked pending work\n')
    (cwd/'.agent-relay/notes.md').write_text('# Goal\nFinish the feature.\n# Progress\nCLI saved staged and unstaged edits.\n# Next\nTest transferred work.\n')
if config.get('stop_hook'):
    settings = json.loads((cwd/'.codex/hooks.json').read_text())
    command = settings['hooks']['Stop'][0]['hooks'][0]['command']
    result = subprocess.run(['/bin/sh','-c',command],input=json.dumps({'hook_event_name':'Stop','cwd':str(cwd)}),text=True,capture_output=True)
    assert result.returncode==0 and 'advisory:' not in result.stderr, result.stderr
if config.get('child'):
    child = subprocess.Popen([sys.executable,'-c',"import pathlib,time; p=pathlib.Path("+repr(config['child'])+"); p.write_text(str(__import__('os').getpid())); time.sleep(60)"])
if config.get('huge'):
    sys.stderr.write('E'*200000); sys.stderr.flush()
    print('N'*1200000,flush=True)
if config.get('sleep'):
    time.sleep(config['sleep'])
for event in config['events']:
    print(json.dumps(event),flush=True)
sys.exit(config.get('exit',0))
'''


class ManagedTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pakati-auto-test-", dir="/private/tmp")
        self.root = Path(self.temporary.name)
        self.project = self.root / "project with space $() apostrophe'"
        self.project.mkdir()
        self.store = self.root / "checkpoints"
        self.handoffs = self.root / "handoffs"
        self.log = self.root / "launches.jsonl"
        self.child_pid = self.root / "child.pid"
        self.old_environment = dict(os.environ)
        os.environ.pop("CLAUDECODE", None)
        for key in list(os.environ):
            if key.startswith("GIT_"):
                os.environ.pop(key)
        self.git("init", "-q")
        self.git("config", "user.name", "Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.project / "code.txt").write_text("initial\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "fixture")
        (self.project / ".agent-relay").mkdir()
        (self.project / ".agent-relay/notes.md").write_text("# Goal\nComplete fixture feature.\n# Progress\nInitial scaffold.\n# Next\nImplement and verify.\n")
        self.codex = self.fake("codex", [{"type": "turn.completed"}])
        self.claude = self.fake("claude", [{"type": "result", "subtype": "success", "is_error": False}], assert_adoption=True)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self.old_environment)
        self.temporary.cleanup()

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.project), *args], env=autopilot.safe_env(), capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def fake(self, agent, events, **options):
        script = self.root / (agent + " fake'$().py")
        script.write_text("#!" + sys.executable + "\n" + FAKE)
        script.chmod(0o700)
        script.with_suffix(".json").write_text(json.dumps(dict(agent=agent, events=events, log=str(self.log), **options)))
        return script

    def args(self, **overrides):
        values = dict(task="fixture", store=str(self.store), project=str(self.project), agent="codex", handoff_root=str(self.handoffs),
                      codex_cli=str(self.codex), claude_cli=str(self.claude), max_switches=1)
        values.update(overrides)
        return SimpleNamespace(**values)

    def run_managed(self, **overrides):
        events = []
        runner = autopilot.Supervisor(self.args(**overrides), emit=events.append)
        code = runner.run()
        return code, events, autopilot.status("fixture", self.store)

    def spawn(self, **overrides):
        args = self.args(**overrides)
        command = [sys.executable, str(ENGINE), "auto", "run"]
        for key in ("task", "store", "project", "agent", "handoff_root", "codex_cli", "claude_cli", "max_switches"):
            command += ["--" + key.replace("_", "-"), str(getattr(args, key))]
        return subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=autopilot.safe_env())

    def wait_state(self, predicate, timeout=15):
        until = time.monotonic() + timeout
        while time.monotonic() < until:
            state = autopilot.status("fixture", self.store)
            if predicate(state):
                return state
            time.sleep(0.05)
        self.fail("State predicate did not become true: " + str(state))

    def launches(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def test_preflight_and_existing_login_environment(self):
        os.environ["OPENAI_API_KEY"] = "fake-not-a-credential"
        os.environ["GIT_DIR"] = "/invalid/inherited"
        result = autopilot.preflight(str(self.codex), str(self.claude))
        self.assertTrue(result["ready"])
        code, _, state = self.run_managed()
        self.assertEqual(code, 0)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(self.launches()[0]["git_env"], [])
        self.assertEqual(self.launches()[0]["keys"], [])
        argv = self.launches()[0]["argv"]
        self.assertEqual(argv, ["--ask-for-approval", "never", "exec", "--json", "--sandbox", "workspace-write", "-"])

    def test_missing_or_unsupported_cli_blocks_before_agent(self):
        self.claude.write_text("#!/bin/sh\nprintf '%s\\n' 'old cli'\n")
        result = autopilot.preflight(str(self.codex), str(self.claude))
        self.assertFalse(result["ready"])
        code, _, state = self.run_managed()
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(self.launches(), [])

    def test_nested_claude_guard_is_preserved(self):
        os.environ["CLAUDECODE"] = "1"
        result = autopilot.preflight(str(self.codex), str(self.claude))
        self.assertFalse(result["ready"])
        self.assertIn("nested-session", result["agents"]["claude"]["detail"])
        self.assertEqual(os.environ["CLAUDECODE"], "1")

    def test_interrupted_status_blocks_edits_until_checkout_lock_releases(self):
        git_dir = Path(self.git("rev-parse", "--absolute-git-dir").strip())
        _, folder = autopilot.task_paths(self.store, "fixture")
        autopilot.atomic_json(folder / "state.json", {"status": "running", "task": "fixture", "run_id": "stale",
                                                     "pid": os.getpid(), "agent_pid": None, "checkout_git_dir": str(git_dir)})
        lock = autopilot.lock_file(git_dir / "pakati-managed-writer.lock")
        try:
            first = autopilot.status("fixture", self.store)
            self.assertEqual(first["status"], "interrupted")
            self.assertTrue(first["writer_active"])
            self.assertTrue(autopilot.status("fixture", self.store)["writer_active"])
        finally:
            lock.close()
        self.assertFalse(autopilot.status("fixture", self.store)["writer_active"])

    def test_quota_transfer_preserves_index_notes_untracked_and_stop_hook(self):
        self.codex = self.fake("codex", [{"type": "turn.failed", "error": {"message": "You've hit your usage limit. Try later."}}],
                               exit=1, edit=True, stop_hook=True)
        code, events, state = self.run_managed()
        self.assertEqual(code, 0, state)
        self.assertEqual(state["status"], "completed")
        self.assertEqual(state["switch_count"], 1)
        self.assertEqual([x["agent"] for x in self.launches()], ["codex", "claude"])
        target = Path(state["project_path"])
        self.assertNotEqual(target, self.project)
        self.assertEqual((target / "code.txt").read_text(), "unstaged by fake CLI\n")
        self.assertEqual((target / "new.txt").read_text(), "untracked pending work\n")
        staged = subprocess.run(["git", "-C", str(target), "show", ":code.txt"], capture_output=True, text=True, timeout=15)
        self.assertEqual(staged.stdout, "staged by fake CLI\n")
        self.assertEqual((target / ".agent-relay/notes.md").read_bytes(), (self.project / ".agent-relay/notes.md").read_bytes())
        self.assertIn("handoff_ready", [x.get("event") for x in events])
        self.assertEqual(json.loads((target / ".agent-relay.json").read_text())["store"], str(self.store))
        self.assertEqual(self.launches()[1]["argv"], ["-p", "--output-format", "stream-json", "--verbose", "--permission-mode", "acceptEdits", "--permission-prompts", "none"])

    def test_both_accounts_exhausted_launch_each_once(self):
        self.codex = self.fake("codex", [{"type": "turn.failed", "error": {"code": "usage_limit_exceeded"}}], exit=1)
        self.claude = self.fake("claude", [{"type": "rate_limit_event", "rate_limit_info": {"status": "rejected", "errorCode": "credits_required"}},
                                           {"type": "assistant", "error": "rate_limit"},
                                           {"type": "result", "subtype": "success", "is_error": True, "terminal_reason": "api_error", "api_error_status": 429}], exit=1, assert_adoption=True)
        code, _, state = self.run_managed()
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(state["unavailable_agents"], ["codex", "claude"])
        self.assertEqual([x["agent"] for x in self.launches()], ["codex", "claude"])

    def test_user_tool_text_and_plain_rate_limit_never_switch(self):
        events = [{"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": "You've hit your usage limit"}},
                  {"type": "error", "message": "You've hit your usage limit"}, {"type": "turn.completed"}]
        self.codex = self.fake("codex", events, huge=True)
        code, emitted, state = self.run_managed()
        self.assertEqual(code, 0)
        self.assertEqual(state["switch_count"], 0)
        self.assertNotIn("You've hit your usage limit", json.dumps(emitted))
        self.assertEqual(len(self.launches()), 1)
        self.claude = self.fake("claude", [{"type": "assistant", "error": "rate_limit"},
                                           {"type": "result", "subtype": "success", "is_error": True, "api_error_status": 429}], exit=1)
        code, _, state = self.run_managed(agent="claude")
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["switch_count"], 0)

    def test_permission_denial_blocks_without_switch(self):
        self.claude = self.fake("claude", [{"type": "system", "subtype": "permission_denied"},
                                           {"type": "result", "subtype": "success", "is_error": False, "permission_denials": [{"tool_name": "Bash"}]}])
        code, _, state = self.run_managed(agent="claude")
        self.assertEqual(code, 2)
        self.assertEqual(state["status"], "blocked")
        self.assertEqual(len(self.launches()), 1)

    def test_meaningful_notes_required_without_overwrite(self):
        notes = self.project / ".agent-relay/notes.md"
        original = "<!-- agent-relay-template: placeholder -->\n# Goal\n"
        notes.write_text(original)
        code, _, state = self.run_managed()
        self.assertEqual(code, 2)
        self.assertEqual(notes.read_text(), original)
        self.assertEqual(self.launches(), [])

    def test_periodic_snapshot_reads_external_note_updates(self):
        self.codex = self.fake("codex", [{"type": "turn.completed"}], edit=True, sleep=0.5)
        emitted = []
        runner = autopilot.Supervisor(self.args(), emit=emitted.append, snapshot_seconds=0.1)
        self.assertEqual(runner.run(), 0)
        self.assertIn("periodic", [e.get("checkpoint_reason") for e in emitted])
        _, _, notes = relay.load_capsule(self.store, "fixture")
        self.assertIn("CLI saved staged and unstaged edits", notes)

    def test_task_and_checkout_writer_exclusion_stop_and_group_cleanup(self):
        self.codex = self.fake("codex", [{"type": "turn.completed"}], child=str(self.child_pid), sleep=60)
        process = self.spawn()
        try:
            state = self.wait_state(lambda value: value.get("agent_pid") is not None)
            until = time.monotonic() + 5
            while not self.child_pid.exists() and time.monotonic() < until:
                time.sleep(0.05)
            self.assertTrue(self.child_pid.exists())
            duplicate = self.spawn()
            out, err = duplicate.communicate(timeout=15)
            self.assertEqual(duplicate.returncode, 2, err)
            self.assertIn("already owns", out)
            other = self.spawn(task="other", store=str(self.root / "other-store"))
            out, err = other.communicate(timeout=15)
            self.assertEqual(other.returncode, 2, err)
            self.assertIn("already owns", out)
            self.assertTrue(autopilot.status("other", self.root / "other-store")["writer_active"])
            requested = autopilot.stop("fixture", self.store)
            self.assertIn("Stop requested", requested["detail"])
            out, err = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 0, err)
            self.assertEqual(autopilot.status("fixture", self.store)["status"], "stopped")
            self.assertFalse(self.alive(int(self.child_pid.read_text())))
            self.assertFalse(self.alive(state["agent_pid"]))
            self.assertEqual(len(self.launches()), 1)
        finally:
            if process.poll() is None:
                process.terminate()
                process.communicate(timeout=15)

    @staticmethod
    def alive(pid):
        result = subprocess.run(["/bin/ps", "-p", str(pid), "-o", "stat="], capture_output=True, text=True, timeout=5)
        return result.returncode == 0 and not result.stdout.strip().startswith("Z")

    def test_sigterm_and_hard_supervisor_crash_cleanup(self):
        for sig in (signal.SIGTERM, signal.SIGKILL):
            with self.subTest(signal=sig):
                self.codex = self.fake("codex", [{"type": "turn.completed"}], child=str(self.child_pid), sleep=60)
                self.child_pid.unlink(missing_ok=True)
                process = self.spawn()
                try:
                    state = self.wait_state(lambda value: value.get("agent_pid") is not None)
                    until = time.monotonic() + 5
                    while not self.child_pid.exists() and time.monotonic() < until:
                        time.sleep(0.05)
                    self.assertTrue(self.child_pid.exists())
                    os.kill(process.pid, sig)
                    process.wait(timeout=15)
                    until = time.monotonic() + 10
                    while self.alive(state["agent_pid"]) and time.monotonic() < until:
                        time.sleep(0.05)
                    self.assertFalse(self.alive(state["agent_pid"]))
                    self.assertFalse(self.alive(int(self.child_pid.read_text())))
                    after = autopilot.status("fixture", self.store)
                    self.assertEqual(after["status"], "stopped" if sig == signal.SIGTERM else "interrupted")
                    # A restart can proceed only after the watchdog ended.
                    self.codex = self.fake("codex", [{"type": "turn.completed"}])
                    code, _, state = self.run_managed()
                    self.assertEqual(code, 0, state)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=15)
                    if process.stdout:
                        process.stdout.close()
                    if process.stderr:
                        process.stderr.close()

    def test_handoff_root_and_stale_head_guard(self):
        code, _, state = self.run_managed(handoff_root=str(self.project / "handoffs"))
        self.assertEqual(code, 2)
        self.assertEqual(self.launches(), [])
        self.assertEqual(self.run_managed()[0], 0)
        saved_head = self.git("rev-parse", "HEAD").strip()
        self.git("checkout", "--orphan", "different")
        (self.project / "code.txt").write_text("unrelated\n")
        self.git("add", "code.txt")
        self.git("commit", "-qm", "unrelated")
        code, _, state = self.run_managed()
        self.assertEqual(code, 2)
        self.assertIn("diverged", state["detail"])
        _, metadata, _ = relay.load_capsule(self.store, "fixture")
        self.assertEqual(metadata["project"]["head"], saved_head)


if __name__ == "__main__":
    unittest.main(verbosity=2)
