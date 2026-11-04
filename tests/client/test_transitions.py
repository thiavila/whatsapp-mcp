"""Exercise real YAML/journal transitions; all service/network/notification I/O is fake."""
from contextlib import ExitStack
import io
import json
from pathlib import Path
import subprocess
import sys
import tarfile
import tempfile
import unittest
import time
from unittest.mock import patch, Mock

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/client'), str(ROOT / 'scripts/maintenance')]
import config as cfg
import client_executor as ce
import install_executor as installer
import indexa_auth_executor as auth
from test_runtime import home


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.notify = self.stack.enter_context(patch.object(ce.e, 'notify'))

    def transaction(self, root):
        sha = 'a' * 40; hermes = root / 'hermes'; hermes.mkdir(); home(hermes)
        # Two files so a crash after the first rename is a genuine partial apply.
        (hermes / 'profiles/melancia-roberto/config.yaml').write_text((hermes / 'config.yaml').read_text())
        self.stack.enter_context(patch.object(ce.e, 'BASE', root))
        self.stack.enter_context(patch.object(ce.e, 'HERMES', hermes))
        self.stack.enter_context(patch.object(cfg, 'RELEASES', str(root / 'client-releases') + '/'))
        self.stack.enter_context(patch.object(ce, 'current_source', return_value=True))
        self.stack.enter_context(patch.object(ce, 'verify_local'))
        self.stack.enter_context(patch.object(ce, 'source_fingerprint', return_value={'fixture': 'unchanged'}))
        self.stack.enter_context(patch.object(ce, 'gateway_count', return_value=3))
        self.stack.enter_context(patch.object(ce, 'processes', return_value=[]))
        self.stack.enter_context(patch.object(ce, 'live', return_value=False))
        self.stack.enter_context(patch.object(ce, 'activation', return_value=sha))
        self.stack.enter_context(patch.object(ce, 'stage', return_value={
            'run_id': 1, 'manifest': {'client_sha256': 'payload'}}))
        self.stack.enter_context(patch.object(ce.e, 'run', return_value=''))
        self.stack.enter_context(patch.object(ce.e, 'gateway_quiet', return_value=True))
        self.stack.enter_context(patch.object(ce.e, 'busy_service', return_value=False))
        self.stack.enter_context(patch.object(ce.e, 'active', return_value=True))
        def config(runtime, action, document=None):
            if action == 'plan': return cfg.plan(hermes, str(runtime / 'run-client'))
            if action == 'status': return cfg.status(hermes, document)
            if action == 'managed': return cfg.managed(hermes, str(runtime / 'run-client'))
            return cfg.update(hermes, document, rollback=action == 'rollback')
        self.stack.enter_context(patch.object(ce, 'config', side_effect=config))
        return sha, hermes

    def test_native_refusal_restores_timers_and_retries_without_blocking_candidate(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha, hermes = self.transaction(root)
            before = (hermes / 'config.yaml').read_bytes()
            def command(action, unit):
                if (action, unit) == ('stop', 'hermes-gateway.service'):
                    self.assertEqual(json.loads((root / 'client-pending.json').read_text())['phase'], 'staged')
                    raise subprocess.CalledProcessError(1, ['native-stop'])
            with patch.object(ce, 'command', side_effect=command) as calls:
                ce.deploy(1, sha)
            self.assertEqual((hermes / 'config.yaml').read_bytes(), before)
            self.assertFalse((root / 'client-pending.json').exists())
            self.assertFalse((root / 'client-blocked.json').exists())
            for timer in ce.e.WATCH_TIMERS:
                self.assertIn(('start', timer), [call.args for call in calls.call_args_list])
            self.notify.assert_not_called()

    def test_initial_proof_then_managed_updates_are_autonomous_and_new_legacy_entries_block(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); hermes = root / 'hermes'; hermes.mkdir(); home(hermes)
            first = 'a' * 40; second = 'b' * 40
            policy = {'schema': 1, 'enabled': True, 'repository': ce.e.REPOSITORY,
                      'workflow': ce.WORKFLOW, 'allow_automatic_managed_updates': True, 'activation_sha': first}
            with patch.object(ce.e, 'BASE', root), patch.object(ce.e, 'REPO', root / 'untouched-legacy-source'), patch.object(cfg, 'RELEASES', str(root / 'client-releases') + '/'):
                old_runner = str(ce.release(first) / 'runtime/run-client')
                first_plan = cfg.plan(hermes, old_runner)
                proof = {'sha': first, 'client_sha256': 'payload', 'config_inventory': first_plan['inventory'],
                         'external_consumers_fenced': True, 'profile_tools_compatible': True,
                         'checked_at': time.time() - 1, 'expires_at': time.time() + 300}
                with patch.object(ce, 'root_document', side_effect=lambda n: policy if n == 'client-policy.json' else proof):
                    self.assertEqual(ce.activation(first, {'client_sha256': 'payload'}, first_plan, 3), first)
                    proof['expires_at'] = time.time() - 1
                    self.assertIsNone(ce.activation(first, {'client_sha256': 'payload'}, first_plan, 3))
                cfg.update(hermes, first_plan)
                ce.e.atomic(root / 'client-last-success.json', {'sha': first, 'activation_sha': first})
                second_plan = cfg.plan(hermes, str(ce.release(second) / 'runtime/run-client'))
                with patch.object(ce, 'root_document', return_value=policy), patch.object(ce, 'verify_local'), patch.object(ce, 'live', return_value=True), patch.object(ce, 'config', side_effect=lambda runtime, action: cfg.managed(hermes, str(runtime / 'run-client'))), patch.object(ce, 'readiness', side_effect=AssertionError('managed update must not need new human proof')):
                    self.assertEqual(ce.activation(second, {'client_sha256': 'next'}, second_plan, 3), first)
                    # A newly introduced legacy caller must undergo fresh reconciliation.
                    path = hermes / 'profiles/memo-vic/config.yaml'
                    path.write_text(json.dumps({'mcp_servers': {'whatsapp-extra': {
                        'command': 'uv', 'args': ['--directory', cfg.LEGACY, 'run', 'main.py']}}}))
                    plan = cfg.plan(hermes, str(ce.release(second) / 'runtime/run-client'))
                    self.assertIsNone(ce.activation(second, {'client_sha256': 'next'}, plan, 3))

    def test_partial_apply_rolls_back_fields_and_preserves_latest_credentials_offline(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha, hermes = self.transaction(root)
            original = cfg.write; changed = False
            def write(path, text, expected):
                nonlocal changed
                original(path, text, expected)
                if not changed:
                    changed = True
                    path.write_text(path.read_text().replace('opaque-preserve', 'latest-credential'))
                    raise RuntimeError('crash after first config rename')
            with patch.object(ce, 'command'), patch.object(cfg, 'write', side_effect=write), patch.object(ce, 'admitted', side_effect=AssertionError('GitHub must not be used in recovery')):
                with self.assertRaisesRegex(RuntimeError, 'crash'):
                    ce.deploy(1, sha)
            for path in [hermes / 'config.yaml', hermes / 'profiles/melancia-roberto/config.yaml']:
                servers, _ = cfg.parse(cfg.read(path))
                self.assertEqual(servers['whatsapp']['command'], '/home/thiago/.local/bin/uv')
            self.assertIn('latest-credential', (hermes / 'config.yaml').read_text())
            self.assertTrue((root / 'client-blocked.json').exists())
            self.assertFalse((root / 'client-pending.json').exists())
            self.assertTrue(any(call.args[0].startswith('client-rollback-') for call in self.notify.call_args_list))

    def test_crash_before_any_stop_needs_no_artifact_or_github_to_resume(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            ce.e.atomic(root / 'client-pending.json', {'sha': 'a' * 40, 'phase': 'staged', 'consumers': {'hermes-gateway.service': True}})
            with patch.object(ce.e, 'BASE', root), patch.object(ce, 'verify_local', side_effect=AssertionError), patch.object(ce, 'admitted', side_effect=AssertionError), patch.object(ce, 'resume') as resume:
                self.assertEqual(ce.recover(), 'deferred')
                resume.assert_called_once()
            self.assertFalse((root / 'client-pending.json').exists())
            self.assertFalse((root / 'client-blocked.json').exists())

    def test_client_lane_error_does_not_prevent_the_go_lane(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha = 'a' * 40
            run = {'status': 'completed', 'conclusion': 'success', 'head_sha': sha, 'id': 1}
            fake_hermes = Mock()
            with patch.object(auth, 'poll_safely', return_value=True), patch.object(ce.e, 'BASE', root), patch.object(ce.e, 'recover'), patch.dict(sys.modules, {'hermes_executor': fake_hermes}), patch.object(ce, 'poll', side_effect=ValueError('client blocked')), patch.object(ce.e, 'api', side_effect=[{'id': 1, 'path': ce.e.WORKFLOW}, {'workflow_runs': [run]}, run]), patch.object(ce.e, 'admit_run'), patch.object(ce.e, 'verify'), patch.object(ce.e, 'deploy') as go:
                ce.e.poll(); go.assert_called_once_with(1, sha)
            self.assertTrue((root / 'client-last-error.json').exists())

    def test_distinct_candidate_errors_each_have_their_own_notification(self):
        with tempfile.TemporaryDirectory() as temp, patch.object(ce.e, 'BASE', Path(temp)):
            for sha in ['a' * 40, 'b' * 40]:
                def fail():
                    ce._candidate_sha = sha
                    raise ValueError('candidate digest mismatch')
                with patch.object(ce, 'poll', side_effect=fail): ce.poll_safely()
            keys = [call.args[0] for call in self.notify.call_args_list]
            self.assertEqual(len(set(keys)), 2)
            recorded = json.loads((Path(temp) / 'client-last-error.json').read_text())
            self.assertEqual(recorded['sha'], 'b' * 40)
            self.assertEqual(recorded['message'], 'candidate digest mismatch')

    def test_applied_recovery_refusal_resumes_timers_and_keeps_journal_for_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha, hermes = self.transaction(root)
            runtime = ce.release(sha) / 'runtime'
            plan = cfg.plan(hermes, str(runtime / 'run-client')); cfg.update(hermes, plan)
            tx = {'sha': sha, 'phase': 'applied', 'plan': plan, 'consumers': {'hermes-gateway.service': True},
                  'expected_clients': 3, 'legacy_inputs': {'fixture': 'unchanged'}}
            ce.e.atomic(root / 'client-pending.json', tx)
            with patch.object(ce, 'stop', side_effect=ce.Deferred('active work')), patch.object(ce, 'resume') as resume:
                self.assertEqual(ce.recover(), 'recovery-deferred'); resume.assert_called_once()
            self.assertTrue((root / 'client-pending.json').exists())
            with patch.object(ce, 'recover', return_value='recovery-deferred'), patch.object(ce.e, 'api') as api:
                ce.poll(); api.assert_not_called()

    def test_go_guard_allows_lazy_managed_clients_without_frozen_historical_count(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); ce.e.atomic(root / 'client-last-success.json', {'sha': 'a' * 40, 'expected_clients': 99})
            with patch.object(ce.e, 'BASE', root), patch.object(ce.subprocess, 'check_output', return_value=b'scripts/client/pins.json\n'), patch.object(ce.subprocess, 'run', return_value=Mock(returncode=0)), patch.object(ce, 'verify_local'), patch.object(ce, 'config', return_value={'all_managed': True}), patch.object(ce, 'processes', return_value=[]):
                self.assertTrue(ce.source_ready('b' * 40))

    def test_go_does_not_stop_or_fetch_artifact_before_client_activation(self):
        sha = 'a' * 40
        with patch.object(ce.e, 'ensure_clean'), patch.object(ce.e, 'gateway_quiet', return_value=True), patch.object(ce.e, 'active', return_value=True), patch.object(ce.e, 'health', return_value=True), patch.object(ce.e, 'run', side_effect=['', sha]), patch.object(ce, 'source_ready', return_value=False), patch.object(ce.e, 'artifact') as artifact, patch.object(ce.e, 'ctl') as ctl:
            ce.e.deploy(1, sha)
            artifact.assert_not_called(); ctl.assert_not_called()

    def test_cached_stage_skips_download_and_canary_after_atomic_retry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha = 'a' * 40
            manifest = {'schema': 1, 'repository': ce.e.REPOSITORY, 'sha': sha, 'versions': ce.VERSIONS,
                        'lock_sha256': 'x', 'source_sha256': {}}
            def artifact(run, commit, destination):
                with tarfile.open(destination, 'w:gz') as tar:
                    content = json.dumps(manifest).encode(); info = tarfile.TarInfo('runtime.json'); info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                return dict(manifest, client_sha256=ce.digest(destination))
            with patch.object(ce.e, 'BASE', root), patch.object(ce, 'artifact', side_effect=artifact), patch.object(ce, 'preflight', return_value={'ok': True, 'versions': ce.VERSIONS}) as canary, patch.object(ce.shutil, 'disk_usage', return_value=Mock(free=10**10)):
                with patch.object(ce.os, 'rename', side_effect=RuntimeError('crash before publish')):
                    with self.assertRaises(RuntimeError): ce.stage(1, sha)
                self.assertFalse((ce.release(sha) / 'runtime').exists())
                first = ce.stage(1, sha)
                with patch.object(ce, 'artifact', side_effect=AssertionError('no download')):
                    second = ce.stage(1, sha)
                self.assertEqual(first, second); canary.assert_called_once()

    def test_control_install_never_publishes_partial_modules(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); base = root / 'base'; base.mkdir()
            source = ROOT / 'scripts/maintenance'
            real_copy = installer.shutil.copyfileobj; count = 0
            def copy(before, after):
                nonlocal count
                count += 1
                if count == 2: raise RuntimeError('interrupted bootstrap')
                real_copy(before, after)
            with patch.object(installer.shutil, 'copyfileobj', side_effect=copy):
                with self.assertRaises(RuntimeError): installer.stage_control(base, source, 'a' * 40)
            self.assertFalse((base / 'control-releases' / ('a' * 40)).exists())
            code = installer.stage_control(base, source, 'a' * 40)
            self.assertTrue((code / 'client_executor.py').is_file())
            self.assertTrue((code / 'executor.py').is_file())

    def test_control_install_requires_reviewed_sha_before_any_mutation(self):
        with patch.object(installer.os, 'getuid', return_value=1000), patch.object(Path, 'home', return_value=Path('/home/thiago')), patch.object(Path, 'read_text', return_value=''), patch.object(Path, 'is_file', return_value=True), patch.object(installer.subprocess, 'check_output', side_effect=[b'', 'a' * 40]), patch.object(Path, 'mkdir') as mkdir, patch.object(installer, 'stage_control') as stage:
            with self.assertRaisesRegex(SystemExit, 'expected reviewed'):
                installer.main('b' * 40)
            mkdir.assert_not_called(); stage.assert_not_called()


if __name__ == '__main__':
    unittest.main()
