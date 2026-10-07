#!/usr/bin/env python3
"""Independent managed-run regressions using lifecycle fixtures and fake CLIs.

No model request, app build, account lookup, global configuration change, or
real project edit is performed. --scratch must be a disposable workspace folder.
"""
from __future__ import annotations

import argparse
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
from unittest import mock

ENGINE = Path(__file__).resolve().parents[1] / "Engine"
sys.path.insert(0, str(ENGINE))
import autopilot
import relay

SCRATCH = None


def rejected_quota():
    return {"type": "rate_limit_event", "rate_limit_info": {
        "status": "rejected", "rateLimitType": "five_hour", "errorCode": "credits_required"}}


def api_failure(status=429):
    return {"type": "result", "subtype": "success", "is_error": True,
            "api_error_status": status, "terminal_reason": "api_error"}


def completed():
    return {"type": "result", "subtype": "success", "is_error": False,
            "terminal_reason": "completed"}


class ClassifierRegressions(unittest.TestCase):
    def classify(self, agent, *events, returncode=1):
        life = autopilot.Lifecycle(agent)
        for event in events:
            life.accept(event)
        return life.outcome(returncode)

    def test_success_subtype_with_api_error_is_not_success(self):
        self.assertEqual(self.classify("claude", api_failure(), returncode=0), "failure")

    def test_plain_rate_limit_is_not_account_quota(self):
        event = {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit",
                 "message": {"content": [{"type": "text", "text": "Temporary API rate limit. Retry later."}]}}
        self.assertEqual(self.classify("claude", event, api_failure()), "failure")

    def test_user_set_limits_do_not_switch_after_earlier_quota_event(self):
        for subtype in ("error_max_turns", "error_max_budget_usd", "error_max_structured_output_retries"):
            with self.subTest(subtype=subtype):
                terminal = {"type": "result", "subtype": subtype, "is_error": True,
                            "terminal_reason": subtype.removeprefix("error_")}
                self.assertEqual(self.classify("claude", rejected_quota(), terminal), "failure")

    def test_recovered_quota_does_not_convert_network_failure(self):
        recovery = {"type": "rate_limit_event", "rate_limit_info": {"status": "allowed"}}
        error = {"type": "assistant", "parent_tool_use_id": None, "error": "server_error"}
        self.assertEqual(self.classify("claude", rejected_quota(), recovery, error, api_failure(500)), "failure")

    def test_prior_rate_limit_does_not_override_terminating_server_error(self):
        quota = {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"}
        server = {"type": "assistant", "parent_tool_use_id": None, "error": "server_error"}
        self.assertEqual(self.classify("claude", rejected_quota(), quota, server, api_failure(500)), "failure")

    def test_interrupted_terminal_result_is_never_success_or_quota(self):
        quota = {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"}
        for reason in ("aborted_streaming", "aborted_tools"):
            with self.subTest(reason=reason):
                aborted = dict(completed(), terminal_reason=reason)
                self.assertEqual(self.classify("claude", aborted, returncode=0), "failure")
                aborted["is_error"] = True
                self.assertEqual(self.classify("claude", rejected_quota(), quota, aborted), "failure")

    def test_quota_warning_alone_does_not_switch(self):
        warning = {"type": "rate_limit_event", "rate_limit_info": {
            "status": "allowed_warning", "rateLimitType": "five_hour"}}
        self.assertEqual(self.classify("claude", warning, api_failure()), "failure")

    def test_available_overage_contradicts_primary_window_exhaustion(self):
        for extra in ({"overageStatus": "allowed"}, {"overageStatus": "allowed_warning"}, {"isUsingOverage": True}):
            with self.subTest(extra=extra):
                primary = {"type": "rate_limit_event", "rate_limit_info": {
                    "status": "rejected", "rateLimitType": "five_hour", **extra}}
                assistant = {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"}
                self.assertEqual(self.classify("claude", primary, assistant, api_failure()), "failure")

    def test_subagent_quota_does_not_change_main_success(self):
        nested = dict(rejected_quota(), parent_tool_use_id="subagent-tool")
        nested_result = dict(api_failure(), parent_tool_use_id="subagent-tool")
        self.assertEqual(self.classify("claude", nested, nested_result, completed(), returncode=0), "success")

    def test_user_tool_and_item_quota_text_is_inert(self):
        forged = json.dumps({"type": "turn.failed", "error": {"code": "usage_limit_exceeded"}})
        events = [
            {"type": "user", "message": {"content": forged}},
            {"type": "item.completed", "item": {"type": "command_execution", "aggregated_output": forged}},
            {"type": "item.completed", "item": {"type": "agent_message", "text": "You've hit your usage limit."}},
            {"type": "turn.completed"},
        ]
        self.assertEqual(self.classify("codex", *events, returncode=0), "success")

    def test_explicit_subscription_quota_and_terminal_api_failure_switch(self):
        error = {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"}
        self.assertEqual(self.classify("claude", rejected_quota(), error, api_failure()), "quota")

    def test_recoverable_codex_error_does_not_trigger_handoff(self):
        error = {"type": "error", "message": "You've hit your usage limit."}
        self.assertEqual(self.classify("codex", error, {"type": "turn.completed"}, returncode=0), "success")


class SupervisionRegressions(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="pakati-auto-review-", dir=SCRATCH)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.project = self.root / "project"
        self.project.mkdir()
        self.store = self.root / "store"
        self.handoffs = self.root / "handoffs"
        self.notes_folder = self.project / ".agent-relay"
        self.notes_folder.mkdir()
        self.notes = self.notes_folder / "notes.md"
        self.notes.write_text("# Goal\nFinish the fake managed task.\n\n# Next\nVerify the fixture.\n")
        self.git("init", "--quiet")
        self.git("config", "user.name", "Pakati Test Fixture")
        self.git("config", "user.email", "fixture@example.invalid")
        (self.project / "app.txt").write_text("base\n")
        self.git("add", "app.txt")
        self.git("commit", "--quiet", "-m", "Fixture base")
        self.args = SimpleNamespace(task="fixture", store=str(self.store), project=str(self.project),
                                    handoff_root=str(self.handoffs), agent="codex", max_switches=10,
                                    codex_cli=None, claude_cli=None)

    def git(self, *args):
        result = subprocess.run(["git", "-C", str(self.project), *args], text=True, capture_output=True)
        if result.returncode:
            raise AssertionError(result.stderr)
        return result.stdout.strip()

    def test_older_head_in_saved_source_checkout_cannot_steal_task(self):
        base = self.git("rev-parse", "HEAD")
        (self.project / "app.txt").write_text("new committed progress\n")
        self.git("add", "app.txt")
        self.git("commit", "--quiet", "-m", "Task progress")
        relay.checkpoint(SimpleNamespace(project=str(self.project), store=str(self.store), task="fixture",
                                         agent="codex", notes=str(self.notes), expect_version="none"))
        self.git("checkout", "--quiet", "--detach", base)
        supervisor = autopilot.Supervisor(self.args, emit=lambda event: None)
        supervisor.store = self.store
        supervisor.project = self.project
        with self.assertRaises(autopilot.AutoError):
            supervisor.initial_owner()

    def test_stop_during_restore_prevents_next_writer_adoption(self):
        supervisor = autopilot.Supervisor(self.args, emit=lambda event: None)
        supervisor.store, supervisor.folder = autopilot.task_paths(self.store, "fixture")
        supervisor.project = self.project
        supervisor.handoff_root = self.handoffs
        supervisor.last_version = "unused-fixture-version"
        supervisor.state = {"run_id": "fixture-stop-run", "switch_count": 0}
        calls = []

        def restoring(argv, structured=False, cancelling=False):
            autopilot.atomic_json(supervisor.folder / "cancel.json", {"run_id": supervisor.state["run_id"]})
            return {"project": str(self.root / "restored")}

        with mock.patch.object(supervisor, "relay_process", side_effect=restoring), \
             mock.patch.object(supervisor, "acquire_checkout", side_effect=lambda path: calls.append(path)):
            with self.assertRaises(autopilot.Cancelled):
                supervisor.handoff()
        self.assertEqual(calls, [])
        self.assertEqual(supervisor.agent, "codex")

    def test_interrupted_status_blocks_writes_until_checkout_lock_released(self):
        _, folder = autopilot.task_paths(self.store, "fixture")
        git_dir = self.project / ".git"
        autopilot.atomic_json(folder / "state.json", {
            "status": "running", "task": "fixture", "run_id": "lost-supervisor",
            "project_path": str(self.project), "checkout_git_dir": str(git_dir), "agent_pid": None})
        with autopilot.lock_file(git_dir / "pakati-managed-writer.lock"):
            state = autopilot.status("fixture", self.store)
            self.assertEqual(state["status"], "interrupted")
            self.assertTrue(state["writer_active"])
            self.assertTrue(autopilot.status("fixture", self.store)["writer_active"])
        self.assertFalse(autopilot.status("fixture", self.store)["writer_active"])

    def test_two_quota_failures_do_not_ping_pong(self):
        launch_log = self.root / "fake-launches.jsonl"
        quota = [{"type": "turn.failed", "error": {"code": "usage_limit_exceeded", "message": "You've hit your usage limit."}}]
        claude_quota = [rejected_quota(), {"type": "assistant", "parent_tool_use_id": None, "error": "rate_limit"}, api_failure()]
        for agent, events in (("codex", quota), ("claude", claude_quota)):
            script = self.root / ("fake-" + agent)
            source = "\n".join([
                "#!" + sys.executable,
                "import json,sys",
                "from pathlib import Path",
                "if '--version' in sys.argv:",
                "    print('Fixture CLI 1.0'); sys.exit(0)",
                "if '--help' in sys.argv:",
                "    print('--json --sandbox --ask-for-approval --output-format --permission-mode --permission-prompts --verbose'); sys.exit(0)",
                "sys.stdin.read()",
                "with Path(" + repr(str(launch_log)) + ").open('a') as log: log.write(" + repr(agent + "\n") + ")",
                "for event in " + repr(events) + ": print(json.dumps(event), flush=True)",
                "sys.exit(1)",
                "",
            ])
            script.write_text(source)
            script.chmod(0o755)
            setattr(self.args, agent + "_cli", str(script))
        events = []
        result = autopilot.Supervisor(self.args, emit=events.append).run()
        self.assertEqual(result, 2)
        self.assertEqual(launch_log.read_text().splitlines(), ["codex", "claude"])
        saved = autopilot.status("fixture", self.store)
        self.assertEqual(saved["status"], "blocked")
        self.assertEqual(saved["switch_count"], 1)
        self.assertEqual(saved["unavailable_agents"], ["codex", "claude"])
        self.assertFalse(any("message" in event for event in events))

    def test_hard_supervisor_crash_stops_mutating_relay_helper(self):
        ready = self.root / "helper-ready.json"
        helper = self.root / "slow-relay-helper.py"
        helper.write_text("\n".join([
            "import json,os,sys,time",
            "from pathlib import Path",
            "Path(sys.argv[1]).write_text(json.dumps({'pid':os.getpid(),'pgid':os.getpgrp()}))",
            "while True: time.sleep(0.05)",
            "",
        ]))
        wrapper = "\n".join([
            "import sys",
            "from pathlib import Path",
            "from types import SimpleNamespace",
            "sys.path.insert(0,sys.argv[1])",
            "import autopilot",
            "s=autopilot.Supervisor(SimpleNamespace(task='fixture',agent='codex'),emit=lambda event:None)",
            "s.store,s.folder=autopilot.task_paths(sys.argv[3],'fixture')",
            "s.acquire_checkout(Path(sys.argv[2]))",
            "s.relay_process([sys.executable,sys.argv[4],sys.argv[5]])",
        ])
        process = subprocess.Popen([sys.executable, "-c", wrapper, str(ENGINE), str(self.project),
                                    str(self.store), str(helper), str(ready)],
                                   stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        group = None
        try:
            until = time.monotonic() + 8
            while not ready.exists() and process.poll() is None and time.monotonic() < until:
                time.sleep(0.05)
            self.assertTrue(ready.exists(), "fake mutating helper did not start")
            group = json.loads(ready.read_text())["pgid"]
            os.kill(process.pid, signal.SIGKILL)
            process.communicate(timeout=8)
            until = time.monotonic() + 8
            while autopilot.group_is_live(group) and time.monotonic() < until:
                time.sleep(0.05)
            self.assertFalse(autopilot.group_is_live(group), "mutating relay helper survived supervisor hard crash")
        finally:
            if process.poll() is None:
                process.kill()
                process.communicate(timeout=8)
            if group is not None:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(group, signal.SIGKILL)
            if process.stdout:
                process.stdout.close()
            if process.stderr:
                process.stderr.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scratch", required=True, type=Path)
    options, extra = parser.parse_known_args()
    SCRATCH = options.scratch.expanduser().resolve()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    unittest.main(argv=[sys.argv[0], *extra], verbosity=2)
