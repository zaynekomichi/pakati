"""Run the core integration checks through the packaged native engine.

Only --scratch disposable repositories are changed; no home-folder settings,
model calls, desktop sessions, or network access are used. Test runner Python
is not used by the packaged engine.
"""
import argparse
import json
import os
import shlex
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ENGINE = None
SCRATCH = None


def run(*args, cwd=None, ok=True, input=None):
    result = subprocess.run([str(x) for x in args], cwd=cwd, input=input,
                            text=True, capture_output=True)
    if ok and result.returncode:
        raise AssertionError(f'{args}: {result.stderr}\n{result.stdout}')
    return result


class RelayTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='relay-packaged-', dir=SCRATCH)
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.repo = self.root / 'source'
        self.repo.mkdir()
        self.store = self.root / 'shared'
        self.notes = self.root / 'notes.md'
        self.notes.write_text('# Goal\nFinish the fixture task.\n\n# Next\nRun verification.\n')
        run('git', 'init', self.repo)
        self.git('config', 'user.name', 'Relay Fixture')
        self.git('config', 'user.email', 'fixture@example.invalid')
        for name, data in {'app.txt': 'base\n', 'delete.txt': 'delete me\n',
                           'cancel.txt': 'base\n', 'run.sh': '#!/bin/sh\ntrue\n',
                           '.gitignore': 'ignored/\n'}.items():
            (self.repo / name).write_text(data)
        (self.repo / 'binary.bin').write_bytes(bytes(range(256)))
        self.git('add', '.')
        self.git('commit', '-m', 'Fixture base')

    def git(self, *args, cwd=None, ok=True):
        return run('git', '-C', cwd or self.repo, *args, ok=ok)

    def cli(self, *args, ok=True):
        return run(ENGINE, *args, ok=ok)

    def checkpoint(self, project=None, notes=True):
        args = ['checkpoint', '--task', 'fixture', '--store', self.store,
                '--project', project or self.repo, '--agent', 'codex']
        if notes:
            args += ['--notes', self.notes]
        return self.cli(*args)

    def restore(self, destination=None, ok=True):
        return self.cli('restore', '--task', 'fixture', '--store', self.store,
                        '--into', destination or self.root / 'restored', ok=ok)

    def test_worktree_transfer_preserves_index_and_files(self):
        worktree = self.root / 'codex-worktree'
        self.git('worktree', 'add', '--detach', worktree)
        (worktree / 'app.txt').write_text('staged\n')
        self.git('add', 'app.txt', cwd=worktree)
        (worktree / 'app.txt').write_text('staged\nplus working change\n')
        # A net diff against HEAD cannot preserve this staged version.
        (worktree / 'cancel.txt').write_text('staged version\n')
        self.git('add', 'cancel.txt', cwd=worktree)
        (worktree / 'cancel.txt').write_text('base\n')
        (worktree / 'delete.txt').unlink()
        (worktree / 'binary.bin').write_bytes(b'\x00\xffunfinished\x00')
        (worktree / 'new-file.bin').write_bytes(b'\x00new\xff')
        (worktree / 'run.sh').chmod(0o755)
        (worktree / 'new-command.sh').write_text('#!/bin/sh\ntrue\n')
        (worktree / 'new-command.sh').chmod(0o755)
        (worktree / '.env').write_text('SECRET=fixture-only\n')
        (worktree / '.env.example').write_text('SECRET=\n')
        (worktree / 'ignored').mkdir()
        (worktree / 'ignored' / 'cache').write_text('do not transfer\n')
        before = self.git('status', '--porcelain', cwd=worktree).stdout
        self.checkpoint(worktree)
        self.assertEqual(before, self.git('status', '--porcelain', cwd=worktree).stdout)
        self.restore()
        target = self.root / 'restored'
        self.assertEqual((target / 'app.txt').read_text(), 'staged\nplus working change\n')
        self.assertEqual(self.git('show', ':app.txt', cwd=target).stdout, 'staged\n')
        self.assertEqual(self.git('show', ':cancel.txt', cwd=target).stdout, 'staged version\n')
        self.assertEqual((target / 'cancel.txt').read_text(), 'base\n')
        self.assertFalse((target / 'delete.txt').exists())
        self.assertEqual((target / 'binary.bin').read_bytes(), b'\x00\xffunfinished\x00')
        self.assertEqual((target / 'new-file.bin').read_bytes(), b'\x00new\xff')
        self.assertTrue(os.access(target / 'run.sh', os.X_OK))
        self.assertTrue(os.access(target / 'new-command.sh', os.X_OK))
        self.assertFalse((target / '.env').exists())
        self.assertTrue((target / '.env.example').exists())
        self.assertFalse((target / 'ignored').exists())

    def test_unpushed_commits_survive_source_removal(self):
        (self.repo / 'app.txt').write_text('unpushed commit\n')
        self.git('add', 'app.txt')
        self.git('commit', '-m', 'Unpushed work')
        head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.git('checkout', '--detach')
        self.checkpoint()
        self.repo.rename(self.root / 'source-unavailable')
        self.restore()
        target = self.root / 'restored'
        self.assertEqual(self.git('rev-parse', 'HEAD', cwd=target).stdout.strip(), head)
        self.assertEqual((target / 'app.txt').read_text(), 'unpushed commit\n')

    def test_existing_destination_is_untouched(self):
        self.checkpoint()
        destination = self.root / 'restored'
        destination.mkdir()
        (destination / 'keep.txt').write_text('keep\n')
        result = self.restore(destination, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual((destination / 'keep.txt').read_text(), 'keep\n')
        self.assertEqual(list(destination.iterdir()), [destination / 'keep.txt'])

    def test_reused_notes_and_versioned_snapshots(self):
        self.checkpoint()
        (self.repo / 'app.txt').write_text('later progress\n')
        self.checkpoint(notes=False)
        result = self.cli('show', '--task', 'fixture', '--store', self.store)
        self.assertIn('Finish the fixture task.', result.stdout)
        self.restore()
        self.assertEqual((self.root / 'restored' / 'app.txt').read_text(), 'later progress\n')

    def test_task_path_traversal_rejected(self):
        result = self.cli('checkpoint', '--task', '../escape', '--store', self.store,
                          '--project', self.repo, '--agent', 'codex', '--notes', self.notes,
                          ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((self.root / 'escape').exists())

    def test_first_checkpoint_requires_notes(self):
        result = self.cli('checkpoint', '--task', 'fixture', '--store', self.store,
                          '--project', self.repo, '--agent', 'codex', ok=False)
        self.assertNotEqual(result.returncode, 0)

    def test_untracked_symlink_does_not_copy_target(self):
        outside = self.root / 'outside.txt'
        outside.write_text('outside repository\n')
        (self.repo / 'unsafe-link').symlink_to(outside)
        result = self.cli('checkpoint', '--task', 'fixture', '--store', self.store,
                          '--project', self.repo, '--agent', 'codex', '--notes', self.notes,
                          ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(outside.read_text(), 'outside repository\n')

    def shown(self):
        return json.loads(self.cli('show', '--task', 'fixture', '--store', self.store).stdout)

    def setup_hooks(self):
        return run(ENGINE, 'setup', '--project', self.repo,
                   '--store', self.store, '--task', 'fixture')

    def hook(self, event, project=None, agent='codex'):
        payload = {'cwd': str(project or self.repo), 'hook_event_name': event,
                   'tool_name': 'Bash', 'tool_input': {'command': 'write fixture files'}}
        if event == 'StopFailure':
            payload['error'] = 'rate_limit'
        return run(ENGINE, 'hook', '--agent', agent,
                   '--relay-hook-v1', input=json.dumps(payload))

    def test_generated_hook_executes_with_spaces_and_shell_characters(self):
        # These strings are data, including literal command-substitution syntax.
        odd_repo = self.root / "source folder ' $(touch SHOULD_NOT_EXIST)"
        self.repo.rename(odd_repo)
        self.repo = odd_repo
        self.store = self.root / "shared store ' $(touch SHOULD_NOT_EXIST)"
        self.setup_hooks()
        (self.repo / '.agent-relay/notes.md').write_text(self.notes.read_text())
        configuration = json.loads((self.repo / '.claude/settings.json').read_text())
        command = configuration['hooks']['SessionStart'][-1]['hooks'][0]['command']
        tokens = shlex.split(command)
        self.assertTrue(Path(tokens[0]).is_file())
        self.assertTrue(os.access(tokens[0], os.X_OK))
        self.assertIn('hook', tokens)
        self.assertFalse(any(token.endswith('.py') for token in tokens))
        payload = json.dumps({'cwd': str(self.repo), 'hook_event_name': 'SessionStart'})
        result = run('/bin/sh', '-c', command, cwd=self.repo, input=payload)
        context = json.loads(result.stdout)['hookSpecificOutput']['additionalContext']
        self.assertIn('fixture', context)
        stop_command = configuration['hooks']['Stop'][-1]['hooks'][0]['command']
        run('/bin/sh', '-c', stop_command, cwd=self.repo,
            input=json.dumps({'cwd': str(self.repo), 'hook_event_name': 'Stop'}))
        self.assertIn('Finish the fixture task.', self.shown()['notes'])
        self.assertFalse((self.repo / 'SHOULD_NOT_EXIST').exists())
        self.assertFalse((self.root / 'SHOULD_NOT_EXIST').exists())

    def test_setup_preserves_authored_task_notes(self):
        (self.repo / '.agent-relay').mkdir()
        notes_path = self.repo / '.agent-relay/notes.md'
        authored = '# Goal\nAuthoritative local task progress.\n'
        notes_path.write_text(authored)
        notes_path.chmod(0o600)
        self.setup_hooks()
        self.assertEqual(notes_path.read_text(), authored)
        self.assertEqual(notes_path.stat().st_mode & 0o777, 0o600)
        self.setup_hooks()
        self.assertEqual(notes_path.read_text(), authored)

    def test_corrupt_artifact_is_rejected_before_restore(self):
        self.checkpoint()
        artifact = Path(self.shown()['capsule_path']) / 'staged.patch'
        artifact.write_bytes(b'corrupted')
        result = self.restore(ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('integrity', result.stderr.lower())
        self.assertFalse((self.root / 'restored').exists())

    def test_failed_checkpoint_preserves_previous(self):
        self.checkpoint()
        version = self.shown()['metadata']['version']
        (self.repo / 'unsupported').symlink_to(self.notes)
        result = self.cli('checkpoint', '--task', 'fixture', '--store', self.store,
                          '--project', self.repo, '--agent', 'codex', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shown()['metadata']['version'], version)

    def test_unchanged_notes_keep_original_timestamp(self):
        self.checkpoint()
        first = self.shown()['metadata']
        (self.repo / 'app.txt').write_text('newer code\n')
        self.checkpoint()
        latest = self.shown()['metadata']
        self.assertEqual(first['notes_at'], latest['notes_at'])
        self.assertNotEqual(first['snapshot_at'], latest['snapshot_at'])

    def test_stale_expected_version_cannot_replace_latest(self):
        self.checkpoint()
        first = self.shown()['metadata']['version']
        self.checkpoint()
        latest = self.shown()['metadata']['version']
        result = self.cli('checkpoint', '--task', 'fixture', '--store', self.store,
                          '--project', self.repo, '--agent', 'claude',
                          '--expect-version', first, ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.shown()['metadata']['version'], latest)

    def test_setup_preserves_rules_settings_and_is_idempotent(self):
        (self.repo / 'AGENTS.md').write_text('Existing project rules.\n')
        custom = {'permissions': {'deny': ['Bash(rm:*)']}, 'hooks': {
            'Stop': [{'hooks': [{'type': 'command', 'command': 'true'}]}]}}
        (self.repo / '.claude').mkdir()
        settings = self.repo / '.claude/settings.json'
        settings.write_text(json.dumps(custom))
        self.setup_hooks()
        paths = [self.repo / x for x in ('AGENTS.md', 'CLAUDE.md', '.agent-relay.json',
                 '.codex/hooks.json', '.claude/settings.json', '.agent-relay/notes.md')]
        first = {p: p.read_bytes() for p in paths}
        self.setup_hooks()
        self.assertEqual(first, {p: p.read_bytes() for p in paths})
        self.assertTrue((self.repo / 'AGENTS.md').read_text().startswith('Existing project rules.'))
        self.assertIn('@AGENTS.md', (self.repo / 'CLAUDE.md').read_text())
        config = json.loads(settings.read_text())
        self.assertEqual(config['permissions'], custom['permissions'])
        self.assertEqual(config['hooks']['Stop'][0], custom['hooks']['Stop'][0])
        self.assertEqual(len(config['hooks']['Stop']), 2)

    def test_hooks_load_notes_and_capture_failure_despite_throttle(self):
        self.setup_hooks()
        (self.repo / '.agent-relay/notes.md').write_text(self.notes.read_text())
        self.hook('PostToolUse')
        first = self.shown()['metadata']['version']
        start = json.loads(self.hook('SessionStart').stdout)
        self.assertIn('Finish the fixture task.', start['hookSpecificOutput']['additionalContext'])
        (self.repo / 'app.txt').write_text('last tool before rate limit\n')
        self.hook('PostToolUse')
        self.assertEqual(self.shown()['metadata']['version'], first)
        self.hook('StopFailure', agent='claude')
        self.assertNotEqual(self.shown()['metadata']['version'], first)
        self.restore()
        self.assertEqual((self.root / 'restored/app.txt').read_text(), 'last tool before rate limit\n')

    def test_restored_checkout_adopts_task_and_old_source_is_skipped(self):
        self.setup_hooks()
        (self.repo / '.agent-relay/notes.md').write_text(self.notes.read_text())
        self.hook('Stop')
        self.restore()
        target = self.root / 'restored'
        (target / 'app.txt').write_text('continued by Claude\n')
        self.hook('Stop', project=target, agent='claude')
        version = self.shown()['metadata']['version']
        self.assertEqual(Path(self.shown()['metadata']['project']['path']).resolve(), target.resolve())
        result = self.hook('Stop')
        self.assertIn('another checkout', result.stderr)
        self.assertEqual(self.shown()['metadata']['version'], version)

    def test_setup_rejects_symlink_parent_before_writing(self):
        external = self.root / 'external-config'
        external.mkdir()
        (self.repo / '.claude').symlink_to(external, target_is_directory=True)
        result = run(ENGINE, 'setup', '--project', self.repo,
                     '--store', self.store, '--task', 'fixture', ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse((external / 'settings.json').exists())
        self.assertFalse((self.repo / '.agent-relay.json').exists())

    def test_user_diff_prefix_setting_cannot_break_restore(self):
        self.git('config', 'diff.noprefix', 'true')
        (self.repo / 'app.txt').write_text('unfinished change\n')
        self.checkpoint()
        self.restore()
        self.assertEqual((self.root / 'restored/app.txt').read_text(), 'unfinished change\n')

    def test_hidden_index_edits_are_rejected(self):
        self.git('update-index', '--assume-unchanged', 'app.txt')
        (self.repo / 'app.txt').write_text('hidden unfinished edit\n')
        result = self.cli('checkpoint', '--task', 'fixture', '--store', self.store,
                          '--project', self.repo, '--agent', 'codex', '--notes', self.notes,
                          ok=False)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('index', result.stderr.lower())

    def test_hooks_accept_new_commits_but_skip_older_branch(self):
        self.setup_hooks()
        (self.repo / '.agent-relay/notes.md').write_text(self.notes.read_text())
        original_head = self.git('rev-parse', 'HEAD').stdout.strip()
        self.hook('Stop')
        first = self.shown()['metadata']['version']
        (self.repo / 'app.txt').write_text('new task commit\n')
        self.git('add', 'app.txt')
        self.git('commit', '-m', 'Continue the task')
        self.hook('Stop')
        latest = self.shown()['metadata']['version']
        self.assertNotEqual(first, latest)
        self.git('checkout', '--detach', original_head)
        result = self.hook('Stop')
        self.assertIn('skipped', result.stderr.lower())
        self.assertEqual(self.shown()['metadata']['version'], latest)


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--engine', required=True, type=Path,
                        help='Packaged native agent-relay-engine executable')
    parser.add_argument('--scratch', required=True, type=Path,
                        help='Workspace directory for disposable fixture repositories')
    options, extra = parser.parse_known_args()
    ENGINE = options.engine.expanduser().resolve(strict=True)
    SCRATCH = options.scratch.expanduser().resolve()
    SCRATCH.mkdir(parents=True, exist_ok=True)
    if not ENGINE.is_file() or not os.access(ENGINE, os.X_OK):
        parser.error('--engine must name an executable regular file')
    unittest.main(argv=[sys.argv[0], *extra], verbosity=2)
