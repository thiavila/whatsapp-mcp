import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import sys
import tarfile
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/maintenance'))
sys.path.insert(0, str(ROOT / 'scripts/client'))
import config as cfg
import launch
import client_archive as archive
import client_executor as ce

RUNNER = cfg.RELEASES + 'a' * 40 + '/runtime/run-client'
OLD = {'command': '/home/thiago/.local/bin/uv',
       'args': ['--directory', cfg.LEGACY, 'run', 'main.py']}


def home(root, block=False):
    for profile in cfg.PROFILES[1:]:
        (root / 'profiles' / profile).mkdir(parents=True)
    text = ('# retained comment\nmcp_servers:\n  whatsapp:\n'
            '    command: /home/thiago/.local/bin/uv\n')
    if block:
        text += '    args:\n      - --directory\n      - ' + cfg.LEGACY + '\n      - run\n      - main.py\n'
    else:
        text += '    args: ' + json.dumps(OLD['args']) + '\n'
    text += '    env:\n      SECRET: "opaque-preserve"\n    headers: {Authorization: "opaque-header"}\nother: kept\n'
    (root / 'config.yaml').write_text(text)
    return text


class ConfigTests(unittest.TestCase):
    def test_crlf_and_credentials_remain_byte_identical(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); before = home(root).replace('\n', '\r\n')
            path = root / 'config.yaml'; path.write_bytes(before.encode())
            plan = cfg.plan(root, RUNNER); cfg.update(root, plan)
            after = path.read_bytes().decode()
            self.assertEqual(after[after.index('    env:'):], before[before.index('    env:'):])

    def test_python_or_cwd_alias_cannot_hide_a_legacy_client(self):
        for command, args, cwd in [(cfg.LEGACY + '/.venv/bin/python', ['main.py'], ''),
                                    ('python', ['main.py'], cfg.LEGACY)]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); home(root)
                value = {'mcp_servers': {'alias': {'command': command, 'args': args, 'cwd': cwd}}}
                (root / 'config.yaml').write_text(json.dumps(value))
                with self.assertRaisesRegex(ValueError, 'unrecognized'):
                    cfg.plan(root, RUNNER)

    def test_inline_and_block_args_preserve_unrelated_bytes_and_values(self):
        for block in [False, True]:
            with self.subTest(block=block), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); before = home(root, block)
                plan = cfg.plan(root, RUNNER)
                self.assertNotIn('opaque', json.dumps(plan))
                cfg.update(root, plan)
                after = (root / 'config.yaml').read_text()
                self.assertTrue(after.startswith('# retained comment\n'))
                self.assertEqual(after[after.index('    env:'):], before[before.index('    env:'):])
                servers, _ = cfg.parse(after)
                self.assertEqual(servers['whatsapp']['command'], RUNNER)
                self.assertEqual(servers['whatsapp']['args'], [])
                cfg.update(root, plan)  # Crash after file replacement is idempotent.
                cfg.update(root, plan, rollback=True)
                restored, _ = cfg.parse((root / 'config.yaml').read_text())
                self.assertEqual(restored['whatsapp']['args'], OLD['args'])
                self.assertEqual(restored['whatsapp']['command'], OLD['command'])

    def test_apply_refuses_foreign_edits_before_any_write(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); before = home(root)
            plan = cfg.plan(root, RUNNER)
            path = root / 'config.yaml'; changed = before + 'foreign: keep\n'; path.write_text(changed)
            with self.assertRaisesRegex(ValueError, 'changed after staging'):
                cfg.update(root, plan)
            self.assertEqual(path.read_text(), changed)

    def test_rollback_preserves_latest_credential_and_unrelated_edit(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); home(root)
            plan = cfg.plan(root, RUNNER); cfg.update(root, plan)
            path = root / 'config.yaml'
            path.write_text(path.read_text().replace('opaque-preserve', 'latest-credential') + 'foreign: keep\n')
            cfg.update(root, plan, rollback=True)
            after = path.read_text()
            self.assertIn('latest-credential', after); self.assertIn('foreign: keep', after)
            self.assertNotIn('opaque-preserve', after)

    def test_foreign_command_is_never_overwritten_by_rollback(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); home(root)
            plan = cfg.plan(root, RUNNER); cfg.update(root, plan)
            path = root / 'config.yaml'; path.write_text(path.read_text().replace(RUNNER, '/foreign/runner'))
            before = path.read_text()
            with self.assertRaisesRegex(ValueError, 'foreign command'):
                cfg.update(root, plan, rollback=True)
            self.assertEqual(before, path.read_text())

    def test_new_profile_or_unrecognized_runner_blocks_before_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); before = home(root)
            (root / 'profiles/new-profile').mkdir()
            with self.assertRaisesRegex(ValueError, 'inventory'):
                cfg.plan(root, RUNNER)
            self.assertEqual((root / 'config.yaml').read_text(), before)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); before = home(root)
            path = root / 'config.yaml'; path.write_text(before.replace('/home/thiago/.local/bin/uv', '/unknown/uv'))
            with self.assertRaisesRegex(ValueError, 'unrecognized'):
                cfg.plan(root, RUNNER)

    def test_duplicate_keys_and_aliases_are_rejected(self):
        for text in ['mcp_servers: {}\nmcp_servers: {}\n', 'a: &x {}\nb: *x\n']:
            with self.assertRaises(ValueError):
                cfg.parse(text)

    def test_symlinked_profile_config_is_not_followed(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); home(root)
            path = root / 'profiles/melancia-roberto/config.yaml'; path.symlink_to(root / 'config.yaml')
            with self.assertRaisesRegex(ValueError, 'symlink'):
                cfg.plan(root, RUNNER)


class ArchiveTests(unittest.TestCase):
    def tar(self, root, name='ok', data=b'data', link=None):
        path = root / 'client.tar.gz'
        with tarfile.open(path, 'w:gz') as tar:
            info = tarfile.TarInfo(name)
            if link:
                info.type = tarfile.SYMTYPE; info.linkname = link
                tar.addfile(info)
            else:
                info.size = len(data); tar.addfile(info, io.BytesIO(data))
        return path

    def test_paths_and_links_cannot_escape(self):
        for name, link in [('../escape', None), ('/escape', None), ('python/lib', '../../../escape')]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); payload = self.tar(root, name, link=link)
                with self.assertRaises(ValueError):
                    archive.extract(payload, root / 'runtime')
                self.assertFalse((root / 'runtime').exists())

    def test_modified_or_added_extracted_code_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); payload = self.tar(root)
            target = root / 'runtime'; archive.extract(payload, target); archive.verify_tree(payload, target)
            (target / 'ok').write_bytes(b'foreign')
            with self.assertRaises(ValueError): archive.verify_tree(payload, target)
            (target / 'ok').write_bytes(b'data'); (target / 'extra').touch()
            with self.assertRaises(ValueError): archive.verify_tree(payload, target)


class RunnerTests(unittest.TestCase):
    def test_actual_command_pins_both_executables_and_disables_resolvers(self):
        root = Path('/isolated/runtime'); args = launch.command(root)
        self.assertEqual(args[0], str(root / 'uv'))
        self.assertEqual(args[args.index('--python') + 1], str(root / 'python/bin/python3'))
        for flag in ['--no-config', '--offline', '--no-project', '--no-sync', '--frozen', '--no-python-downloads']:
            self.assertIn(flag, args)
        env = launch.environment({'UV_WITH': 'evil', 'PYTHONPATH': '/evil', 'VIRTUAL_ENV': '/evil',
                                  'WHATSAPP_BRIDGE_TOKEN': 'opaque', 'WHATSAPP_STORE_DIR': 'existing',
                                  'UV_ENV_FILE': '/synthetic/product.env', 'UV_ENV_FILE_OVERRIDE': 'true',
                                  'UV_NO_ENV_FILE': '1'})
        self.assertNotIn('UV_WITH', env); self.assertNotIn('PYTHONPATH', env); self.assertNotIn('VIRTUAL_ENV', env)
        self.assertEqual(env['WHATSAPP_BRIDGE_TOKEN'], 'opaque')
        self.assertEqual(env['WHATSAPP_STORE_DIR'], 'existing')
        self.assertEqual(env['UV_ENV_FILE'], '/synthetic/product.env')
        self.assertEqual(env['UV_ENV_FILE_OVERRIDE'], 'true')
        self.assertEqual(env['UV_NO_ENV_FILE'], '1')
        self.assertNotIn('--no-env-file', args)
        self.assertEqual(env['WHATSAPP_BRIDGE_DIR'], str(launch.LEGACY.parent / 'whatsapp-bridge'))


class ClientExecutorTests(unittest.TestCase):
    def setUp(self):
        # Never let synthetic tests invoke the product's notification CLI.
        self.notification = patch.object(ce.e, 'notify')
        self.notify = self.notification.start(); self.addCleanup(self.notification.stop)

    def test_root_proof_binds_payload_all_profiles_inventory_and_expiry(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha = 'a' * 40; now = time.time()
            inventory = {p: None for p in ce.PROFILES}
            proof = dict(sha=sha, client_sha256='payload', config_inventory=inventory,
                         external_consumers_fenced=True, profile_tools_compatible=True,
                         checked_at=now - 1, expires_at=now + 300)
            path = root / 'client-readiness.json'; path.write_text(json.dumps(proof))
            st = SimpleNamespace(st_mode=0o100644, st_uid=0, st_nlink=1, st_size=path.stat().st_size)
            with patch.object(ce.e, 'BASE', root), patch.object(ce.os, 'fstat', return_value=st):
                self.assertTrue(ce.readiness(sha, {'client_sha256': 'payload'}, {'inventory': inventory}))
                for field, value in [('sha', 'b' * 40), ('expires_at', now - 1), ('external_consumers_fenced', False), ('config_inventory', {})]:
                    path.write_text(json.dumps(dict(proof, **{field: value})))
                    self.assertFalse(ce.readiness(sha, {'client_sha256': 'payload'}, {'inventory': inventory}))
            path.write_text(json.dumps(proof))
            with patch.object(ce.e, 'BASE', root):
                self.assertFalse(ce.readiness(sha, {'client_sha256': 'payload'}, {'inventory': inventory}))

    def test_old_python_children_cannot_satisfy_post_cutover_proof(self):
        runtime = Path('/candidate/runtime')
        with patch.object(ce, 'processes', return_value=[(1, '/old/python3.11')]):
            self.assertFalse(ce.live(runtime, 1))
        with patch.object(ce, 'processes', return_value=[(1, str(runtime / 'python/bin/python3'))]), patch.object(ce, 'gateway_count', return_value=1), patch.object(ce, 'digest', return_value='mapped-bytes'):
            self.assertTrue(ce.live(runtime, 1))
        with patch.object(ce, 'processes', return_value=[(1, str(runtime / 'python/bin/python3') + ' (deleted)')]):
            self.assertFalse(ce.live(runtime, 1))
        with patch.object(ce, 'processes', return_value=[(1, str(runtime / 'python/bin/python3'))]), patch.object(ce, 'gateway_count', return_value=1), patch.object(ce, 'digest', side_effect=['mapped-old', 'current-file']):
            self.assertFalse(ce.live(runtime, 1))

    def test_no_artifact_can_touch_bridges_or_data_in_client_stop(self):
        with patch.object(ce, 'command') as command, patch.object(ce.e, 'busy_service', return_value=False), patch.object(ce, 'processes', return_value=[]):
            ce.stop({})
        units = [call.args[1] for call in command.call_args_list]
        self.assertNotIn('whatsapp-bridge-indexa.service', units)
        self.assertIn('hermes-gateway.service', units); self.assertIn(ce.DASHBOARD, units)

    def test_watcher_or_native_stop_refusal_prevents_config_mutation(self):
        with patch.object(ce, 'command') as command, patch.object(ce.e, 'busy_service', return_value=True):
            with self.assertRaisesRegex(ce.Deferred, 'watcher'):
                ce.stop({})
        self.assertNotIn('hermes-gateway.service', [c.args[1] for c in command.call_args_list])

    def test_client_artifact_is_bound_to_versions_repository_sha_and_digest(self):
        payload = b'fixture'; sha = 'a' * 40
        manifest = dict(schema=1, repository=ce.e.REPOSITORY, sha=sha, versions=ce.VERSIONS,
                        lock_sha256='b' * 64, source_sha256={}, client_sha256=hashlib.sha256(payload).hexdigest())
        for corrupt in [False, True]:
            data = io.BytesIO()
            with zipfile.ZipFile(data, 'w') as archive:
                archive.writestr('manifest.json', json.dumps(dict(manifest, versions={} if corrupt else ce.VERSIONS)))
                archive.writestr('client.tar.gz', payload)
            raw = data.getvalue()
            record = dict(name=ce.ARTIFACT, expired=False, size_in_bytes=len(raw), id=1, digest='sha256:' + hashlib.sha256(raw).hexdigest())
            with tempfile.TemporaryDirectory() as temp, patch.object(ce.e, 'api', return_value={'artifacts': [record]}), patch.object(ce.subprocess, 'check_output', return_value=raw):
                if corrupt:
                    with self.assertRaisesRegex(ValueError, 'manifest'):
                        ce.artifact(1, sha, Path(temp) / 'payload')
                else:
                    ce.artifact(1, sha, Path(temp) / 'payload')

    def test_recovery_rolls_back_only_commands_then_prior_consumers(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha = 'a' * 40
            tx = dict(sha=sha, run_id=1, client_sha256='digest', plan={'records': []},
                      consumers={'hermes-gateway.service': True}, expected_clients=3)
            ce.e.atomic(root / 'client-pending.json', tx)
            with patch.object(ce.e, 'BASE', root), patch.object(ce, 'digest', return_value='digest'), patch.object(ce, 'verify_tree'), patch.object(ce, 'admitted'), patch.object(ce, 'config', side_effect=[{'new_commands': False}, None]) as config, patch.object(ce, 'stop') as stop, patch.object(ce, 'resume') as resume:
                ce.recover()
                self.assertEqual(config.call_args_list[-1].args[1], 'rollback')
                stop.assert_called_once(); resume.assert_called_once()
            self.assertFalse((root / 'client-pending.json').exists())
            self.assertTrue((root / 'client-blocked.json').exists())

    def test_crash_after_success_reconciles_without_restarting_healthy_clients(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); sha = 'a' * 40
            ce.e.atomic(root / 'client-pending.json', dict(sha=sha, run_id=1, client_sha256='digest', plan={}, consumers={}, expected_clients=3))
            with patch.object(ce.e, 'BASE', root), patch.object(ce, 'digest', return_value='digest'), patch.object(ce, 'verify_tree'), patch.object(ce, 'admitted'), patch.object(ce, 'config', return_value={'new_commands': True}), patch.object(ce, 'live', return_value=True), patch.object(ce.e, 'active', return_value=True), patch.object(ce, 'resume'), patch.object(ce, 'stop') as stop:
                ce.recover(); stop.assert_not_called()
            self.assertTrue((root / 'client-last-success.json').exists())
            self.assertFalse((root / 'client-pending.json').exists())


if __name__ == '__main__':
    unittest.main()
