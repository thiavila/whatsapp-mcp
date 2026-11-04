"""Synthetic auth transition tests. No VM, real credential, WhatsApp DB or LLM."""
from contextlib import ExitStack
import copy
import hashlib
import http.server
import io
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
import zipfile

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT / 'scripts/maintenance'), str(ROOT / 'scripts/client')]
import config as cfg
import indexa_auth_common as a
import indexa_auth_config as ac
import indexa_auth_executor as ae
import indexa_auth_probe as probe
import indexa_auth_runtime as wrapper

SHA = 'a' * 40
RUNTIME = Path(cfg.RELEASES) / SHA / 'runtime'
TOKEN_FILE = '/home/thiago/.config/whatsapp-bridges/approved-indexa.token'
POLICY = {'schema': 1, 'enabled': True, 'repository': a.REPOSITORY, 'workflow': a.WORKFLOW,
          'activation_sha': SHA, 'token_ref': 'indexa-rest', 'token_file': TOKEN_FILE,
          'consumers': [{'profile': 'indexa-roberto', 'server': 'whatsapp_indexa'}]}


def home(root, style='block'):
    for name in cfg.PROFILES[1:]:
        (root / 'profiles' / name).mkdir(parents=True)
    path = cfg.config_path(root, 'indexa-roberto')
    entry = {'command': str(RUNTIME / 'run-client'), 'args': [], 'env': {'SECRET': 'opaque-do-not-journal'}}
    value = {'mcp_servers': {'whatsapp_indexa': entry}, 'other': 'preserved'}
    if style == 'flow':
        text = json.dumps(value) + '\n'
    else:
        text = ('# retained\nmcp_servers:\n  whatsapp_indexa:\n    command: ' + str(RUNTIME / 'run-client') +
                '\n    args: []\n    env:\n      SECRET: "opaque-do-not-journal" # retained env comment\nother: preserved\n')
    path.write_bytes(text.encode())
    return path


class ConfigTests(unittest.TestCase):
    def test_serialized_plan_applies_exact_planned_bytes(self):
        # The real adapter and journal serialize dictionaries with sort_keys.
        # Field insertion order must survive that boundary deterministically.
        for style, env_style in [('block', 'present'), ('block', 'absent'),
                                 ('block', 'null'), ('block', 'empty'),
                                 ('flow', 'present')]:
            for sort_keys in [False, True]:
                with self.subTest(style=style, env=env_style, sorted=sort_keys), tempfile.TemporaryDirectory() as temp:
                    root = Path(temp); path = home(root, style)
                    if env_style != 'present':
                        text = cfg.read(path); start = text.index('    env:'); end = text.index('other:')
                        replacement = {'absent': '', 'null': '    env: null\n', 'empty': '    env: {}\n'}[env_style]
                        path.write_text(text[:start] + replacement + text[end:])
                    plan = json.loads(json.dumps(ac.inventory(root, POLICY, RUNTIME, cfg), sort_keys=sort_keys))
                    ac.apply(root, POLICY, RUNTIME, plan, cfg)
                    after = path.read_bytes()
                    self.assertEqual(cfg.sha(cfg.read(path)), plan['records'][0]['after'])
                    ac.apply(root, POLICY, RUNTIME, plan, cfg)
                    self.assertEqual(path.read_bytes(), after)

    def test_exact_targets_preserve_opaque_bytes_and_crlf(self):
        for style in ['block', 'flow']:
            with self.subTest(style=style), tempfile.TemporaryDirectory() as temp:
                root = Path(temp); path = home(root, style)
                before = path.read_bytes().replace(b'\n', b'\r\n'); path.write_bytes(before)
                plan = ac.inventory(root, POLICY, RUNTIME, cfg)
                self.assertNotIn('opaque', json.dumps(plan))
                ac.apply(root, POLICY, RUNTIME, plan, cfg)
                after = path.read_bytes()
                self.assertIn(b'\r\n', after)
                if style == 'block':
                    self.assertIn(b'SECRET: "opaque-do-not-journal" # retained env comment\r\n', after)
                env = cfg.parse(after.decode())[0]['whatsapp_indexa']['env']
                self.assertEqual({k: env[k] for k in plan['new']}, plan['new'])
                ac.apply(root, POLICY, RUNTIME, plan, cfg)
                self.assertEqual(path.read_bytes(), after)

    def test_absent_or_null_env_and_new_foreign_unrelated_value_survive(self):
        for envline in ['', '    env: null\n', '    env: {}\n']:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); path = home(root)
                text = cfg.read(path); start = text.index('    env:'); end = text.index('other:')
                path.write_text(text[:start] + envline + text[end:])
                plan = ac.inventory(root, POLICY, RUNTIME, cfg)
                path.write_text(cfg.read(path) + 'new_foreign_setting: retained\n')
                ac.apply(root, POLICY, RUNTIME, plan, cfg)
                self.assertTrue(cfg.read(path).endswith('new_foreign_setting: retained\n'))

    def test_inline_credential_unknown_reference_and_extra_consumer_refused(self):
        for changes in [{'WHATSAPP_BRIDGE_TOKEN': 'secret-must-never-be-output'},
                        {'WHATSAPP_BRIDGE_TOKEN_FILE': '/different/token'},
                        {'WHATSAPP_API_BASE_URL': 'http://evil.example/api'}]:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); path = home(root)
                value = cfg.yaml.safe_load(cfg.read(path)); value['mcp_servers']['whatsapp_indexa']['env'].update(changes)
                path.write_text(json.dumps(value)); before = path.read_bytes()
                with self.assertRaises(ValueError) as error: ac.inventory(root, POLICY, RUNTIME, cfg)
                self.assertNotIn('secret-must', str(error.exception)); self.assertEqual(path.read_bytes(), before)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = home(root)
            cfg.config_path(root, 'default').write_bytes(path.read_bytes())
            with self.assertRaisesRegex(ValueError, 'unapproved'): ac.inventory(root, POLICY, RUNTIME, cfg)

    def test_melancia_reference_cannot_be_shared(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = home(root)
            value = cfg.yaml.safe_load(cfg.read(path)); env = value['mcp_servers']['whatsapp_indexa']['env']
            env.update(WHATSAPP_BRIDGE_PORT='8083', WHATSAPP_BRIDGE_TOKEN_FILE=TOKEN_FILE)
            cfg.config_path(root, 'default').write_text(json.dumps(value))
            with self.assertRaisesRegex(ValueError, 'shared'): ac.inventory(root, POLICY, RUNTIME, cfg)

    def test_partial_apply_recovers_forward_and_preserves_foreign_auth_conflict(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); first = home(root); second = cfg.config_path(root, 'default')
            second.write_bytes(first.read_bytes())
            value = copy.deepcopy(POLICY); value['consumers'].append({'profile': 'default', 'server': 'whatsapp_indexa'})
            plan = ac.inventory(root, value, RUNTIME, cfg); real = cfg.write; calls = 0
            def interrupted(*args):
                nonlocal calls
                calls += 1
                if calls == 2: raise OSError('simulated crash')
                return real(*args)
            with patch.object(cfg, 'write', side_effect=interrupted):
                with self.assertRaises(OSError): ac.apply(root, value, RUNTIME, plan, cfg)
            ac.apply(root, value, RUNTIME, plan, cfg)
            self.assertEqual(cfg.parse(cfg.read(first))[0]['whatsapp_indexa']['env']['WHATSAPP_BRIDGE_TOKEN_FILE'], TOKEN_FILE)
            data = cfg.yaml.safe_load(cfg.read(first)); data['mcp_servers']['whatsapp_indexa']['env']['WHATSAPP_BRIDGE_TOKEN_FILE'] = '/foreign/reference'
            first.write_text(json.dumps(data)); before = first.read_bytes()
            with self.assertRaises(ValueError): ac.apply(root, value, RUNTIME, plan, cfg)
            self.assertEqual(first.read_bytes(), before)

    def test_aliases_duplicates_and_legacy_runner_fail_closed(self):
        for text in ['mcp_servers: {x: &x {command: x}, y: *x}',
                     'mcp_servers: {}\nmcp_servers: {}\n']:
            with tempfile.TemporaryDirectory() as temp:
                root = Path(temp); path = home(root); path.write_text(text)
                with self.assertRaises(ValueError): ac.inventory(root, POLICY, RUNTIME, cfg)
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp); path = home(root); path.write_text(cfg.read(path).replace(str(RUNTIME / 'run-client'), 'uv'))
            with self.assertRaisesRegex(ValueError, 'managed'): ac.inventory(root, POLICY, RUNTIME, cfg)


class RuntimeTests(unittest.TestCase):
    def test_missing_or_changed_policy_never_execs_binary(self):
        for document in [{**POLICY, 'enabled': False}, POLICY]:
            with patch.object(a, 'root_document', return_value=document), patch.object(sys, 'argv', ['wrapper', 'wrong']), patch.object(os, 'execve') as execute:
                with self.assertRaises(ValueError): wrapper.main()
                execute.assert_not_called()

    def test_wrapper_overrides_inherited_open_settings_but_keeps_store(self):
        with patch.object(a, 'token', return_value='x' * 44):
            env = a.secure_environment(POLICY, {'WHATSAPP_BRIDGE_TOKEN': 'foreign',
                'WHATSAPP_BRIDGE_AUTH_MODE': 'off', 'WHATSAPP_BRIDGE_REQUIRE_TOKEN': 'false',
                'WHATSAPP_BRIDGE_PORT': '8083', 'WHATSAPP_STORE_DIR': 'existing-store'})
        self.assertNotIn('WHATSAPP_BRIDGE_TOKEN', env)
        self.assertEqual(env['WHATSAPP_BRIDGE_AUTH_MODE'], 'enforce')
        self.assertEqual(env['WHATSAPP_BRIDGE_REQUIRE_TOKEN'], 'true')
        self.assertEqual(env['WHATSAPP_STORE_DIR'], 'existing-store')
        self.assertEqual(env['WHATSAPP_BRIDGE_TOKEN_FILE'], TOKEN_FILE)

    def test_token_file_safe_metadata_and_current_bytes_only(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp).resolve() / 'token'; path.write_text('x' * 44 + '\n'); path.chmod(0o600)
            original = os.fstat
            def metadata(fd):
                st = original(fd)
                return SimpleNamespace(st_mode=st.st_mode, st_nlink=st.st_nlink, st_uid=1000, st_size=st.st_size)
            with patch.object(a, 'token_path', return_value=path), patch.object(os, 'fstat', side_effect=metadata):
                self.assertEqual(a.token(TOKEN_FILE), 'x' * 44)
                path.write_text('y' * 44); self.assertEqual(a.token(TOKEN_FILE), 'y' * 44)
                path.chmod(0o644)
                with self.assertRaises(ValueError): a.token(TOKEN_FILE)
                path.chmod(0o600); link = path.with_name('hardlink'); os.link(path, link)
                with self.assertRaises(ValueError): a.token(TOKEN_FILE)
                link.unlink(); path.unlink(); path.symlink_to(link)
                with self.assertRaises(ValueError): a.token(TOKEN_FILE)


class TransactionTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack(); self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory())).resolve()
        self.stack.enter_context(patch.object(ae.e, 'BASE', self.root))
        self.stack.enter_context(patch.object(ae, 'DROPIN', self.root / 'unit.d/90-indexa-auth.conf'))
        self.notify = self.stack.enter_context(patch.object(ae.e, 'notify'))
        self.states = {u: True for u in ae.c.consumers()}
        self.tx = {'schema': 1, 'sha': SHA, 'phase': 'secure-hold', 'consumers': self.states,
                   'policy_sha256': a.fingerprint(POLICY), 'control_sha256': ae.module_hashes(),
                   'binary_sha256': 'binary', 'client_sha': SHA, 'client_sha256': 'client', 'plan': {},
                   'unit_baseline': {'files': {'base': 'hash'}, 'path': a.BINARY, 'argv': a.BINARY},
                   'dropin': ae.dropin(POLICY)}
        self.stack.enter_context(patch.object(a, 'root_document', return_value=POLICY))

    def pending(self, phase='secure-hold'):
        self.tx['phase'] = phase; ae.e.atomic(self.root / ae.PENDING, self.tx)

    def test_staged_refusal_restores_states_without_writing_or_stopping_bridge(self):
        self.pending('staged')
        with patch.object(ae.c, 'resume') as resume, patch.object(ae.e, 'ctl') as ctl, patch.object(ae, 'install_dropin') as install:
            ae.recover()
        resume.assert_called_once_with(self.states); ctl.assert_not_called(); install.assert_not_called()
        self.assertFalse((self.root / ae.PENDING).exists())

    def test_crash_after_secure_hold_and_missing_token_stops_open_bridge_first(self):
        self.pending()
        with patch.object(ae, 'unit_snapshot'), patch.object(ae, 'unit_matches', return_value='new'), patch.object(ae, 'install_dropin'), patch.object(ae.c, 'stop'), patch.object(a, 'token', side_effect=ValueError('missing')), patch.object(ae.e, 'ctl') as ctl, patch.object(ae.c, 'resume') as resume:
            with self.assertRaises(ValueError): ae.recover()
        ctl.assert_called_once_with('stop', a.UNIT); resume.assert_not_called()
        self.assertEqual(json.loads((self.root / ae.PENDING).read_text())['phase'], 'secure-hold')

    def test_config_failure_keeps_secure_dropin_pending_and_never_restores_secrets(self):
        self.pending()
        self.stack.enter_context(patch.object(a, 'token', return_value='secret-only-memory'))
        self.stack.enter_context(patch.object(ae, 'document', side_effect=lambda n: self.tx if n == ae.PENDING else {'sha': SHA, 'client_sha256': 'client'}))
        self.stack.enter_context(patch.object(ae.c, 'verify_local'))
        self.stack.enter_context(patch.object(ae, 'module_hashes', return_value=self.tx['control_sha256']))
        self.stack.enter_context(patch.object(ae.e, 'digest', return_value='binary'))
        self.stack.enter_context(patch.object(ae.c, 'stop'))
        self.stack.enter_context(patch.object(ae, 'unit_snapshot', return_value={}))
        self.stack.enter_context(patch.object(ae, 'unit_matches', return_value='new'))
        with patch.object(ae, 'install_dropin') as install, patch.object(ae, 'adapter', side_effect=ValueError('conflict')), patch.object(ae.e, 'ctl') as ctl, patch.object(ae.c, 'resume') as resume:
            with self.assertRaises(ValueError): ae.finish(self.tx)
        install.assert_called_once(); resume.assert_not_called()
        self.assertNotIn(('start', a.UNIT), [v.args for v in ctl.call_args_list])
        self.assertNotIn('secret-only-memory', (self.root / ae.PENDING).read_text())

    def test_pending_auth_blocks_all_other_lanes_even_recovery(self):
        self.pending()
        with patch.object(ae, 'recover', side_effect=ValueError('private')), patch.object(ae.e, 'ctl'), patch.object(ae.c, 'stop'), patch.object(ae.e, 'recover') as go_recover, patch.dict(sys.modules, {'hermes_executor': Mock()}):
            ae.e.poll()
        go_recover.assert_not_called(); self.assertTrue((self.root / ae.PENDING).exists())

    def test_before_activation_failure_does_not_disable_other_lanes(self):
        with patch.object(ae, 'recover'), patch.object(ae.e, 'api', side_effect=ValueError('missing workflow')):
            self.assertTrue(ae.poll_safely())

    def test_unit_foreign_change_and_existing_file_preserved(self):
        with self.assertRaises(ValueError): ae.unit_matches(self.tx, {'files': {}, 'path': 'foreign', 'argv': 'foreign'})
        ae.DROPIN.parent.mkdir(); ae.DROPIN.write_text('foreign-unit-setting')
        with patch.object(ae.e, 'run') as run:
            with self.assertRaises(ValueError): ae.install_dropin(self.tx)
        run.assert_not_called(); self.assertEqual(ae.DROPIN.read_text(), 'foreign-unit-setting')

    def test_atomic_dropin_is_idempotent_and_has_no_secret(self):
        with patch.object(ae.e, 'run'):
            ae.install_dropin(self.tx); ae.install_dropin(self.tx)
        self.assertEqual(ae.DROPIN.read_text(), self.tx['dropin'])
        self.assertNotIn(TOKEN_FILE, self.tx['dropin'])

    def test_readiness_binds_every_metadata_field_and_expires(self):
        record = {'sha': SHA, 'manifest': {'config_sha256': a.fingerprint(a.CONFIG), 'control_sha256': ae.module_hashes()}}
        plan = {'inventory': {p: None for p in cfg.PROFILES}}
        client = {'sha': SHA, 'client_sha256': 'client'}; unit = self.tx['unit_baseline']
        with patch.object(ae.e, 'digest', return_value='binary'), patch.object(a, 'root_document', side_effect=ValueError):
            self.assertFalse(ae.readiness(record, POLICY, plan, unit, client))
        proof = json.loads((self.root / 'indexa-auth-stage.json').read_text())
        proof.update({k: True for k in ['external_consumers_fenced', 'effective_environment_verified', 'token_provenance_indexa', 'profile_tools_compatible']})
        proof.update(checked_at=time.time() - 2, expires_at=time.time() + 10)
        with patch.object(ae.e, 'digest', return_value='binary'), patch.object(a, 'root_document', return_value=proof):
            self.assertTrue(ae.readiness(record, POLICY, plan, unit, client))
            proof['unit_sha256'] = 'foreign'; self.assertFalse(ae.readiness(record, POLICY, plan, unit, client))

    def test_artifact_has_only_config_and_hashes_and_rejects_wrong_control(self):
        sys.path.insert(0, str(ROOT / 'scripts/auth'))
        import build
        out = self.root / 'output'; build.build(SHA, out)
        manifest = json.loads((out / 'manifest.json').read_text())
        ae.manifest_valid(manifest, SHA)
        manifest['control_sha256']['executor.py'] = 'foreign'
        with self.assertRaises(ValueError): ae.manifest_valid(manifest, SHA)
        self.assertEqual(json.loads((out / 'auth.json').read_text()), a.CONFIG)
        self.assertEqual({p.name for p in out.iterdir()}, {'auth.json', 'manifest.json'})

    def successful_environment(self):
        self.events = []; installed = {'value': False}
        client = {'sha': SHA, 'client_sha256': 'client'}
        self.stack.enter_context(patch.object(a, 'token', return_value='in-memory-only'))
        self.stack.enter_context(patch.object(ae, 'module_hashes', return_value=self.tx['control_sha256']))
        self.stack.enter_context(patch.object(ae.e, 'digest', return_value='binary'))
        self.stack.enter_context(patch.object(ae, 'managed_runtime', return_value=(RUNTIME, client)))
        self.stack.enter_context(patch.object(ae.c, 'last_success', return_value=client))
        self.stack.enter_context(patch.object(ae.c, 'verify_local'))
        self.stack.enter_context(patch.object(ae.e, 'gateway_quiet', return_value=True))
        self.stack.enter_context(patch.object(ae.e, 'active', return_value=True))
        self.stack.enter_context(patch.object(ae, 'readiness', return_value=True))
        self.stack.enter_context(patch.object(ae.c, 'stop', side_effect=lambda states: self.events.append('stop-consumers')))
        self.stack.enter_context(patch.object(ae.c, 'resume', side_effect=lambda states: self.events.append('resume')))
        self.stack.enter_context(patch.object(ae.e, 'ctl', side_effect=lambda action, unit: self.events.append((action, unit))))
        self.stack.enter_context(patch.object(ae.e, 'healthy'))
        self.stack.enter_context(patch.object(ae, 'unit_snapshot', side_effect=lambda: {'installed': True} if installed['value'] else self.tx['unit_baseline']))
        self.stack.enter_context(patch.object(ae, 'unit_matches', side_effect=lambda tx, current: 'new' if current.get('installed') else 'old'))
        def install(tx):
            self.events.append('secure-wrapper'); installed['value'] = True
        self.stack.enter_context(patch.object(ae, 'install_dropin', side_effect=install))
        plan = {'inventory': {}, 'new': a.client_fields(TOKEN_FILE), 'records': []}
        def adapter(runtime, script, action, data):
            self.events.append(action)
            if action == 'plan': return plan
            if action == 'apply':
                self.assertEqual(json.loads((self.root / ae.PENDING).read_text())['phase'], 'secure-hold')
            return {'ok': True, 'unauthenticated_status': 401, 'authenticated_status': 200}
        self.stack.enter_context(patch.object(ae, 'adapter', side_effect=adapter))
        # finish reads the same real client success pointer during recovery.
        ae.e.atomic(self.root / 'client-last-success.json', client)
        return {'sha': SHA, 'run_id': 1, 'manifest': {'config_sha256': a.fingerprint(a.CONFIG)}}

    def test_full_transaction_orders_preflight_config_enforce_proof_then_resume(self):
        record = self.successful_environment()
        ae.deploy(record)
        events = self.events
        self.assertLess(events.index('before'), events.index('stop-consumers'))
        self.assertLess(events.index('secure-wrapper'), events.index('apply'))
        self.assertLess(events.index('apply'), events.index(('start', a.UNIT)))
        self.assertLess(events.index('after'), events.index('resume'))
        self.assertFalse((self.root / ae.PENDING).exists())
        success = json.loads((self.root / 'indexa-auth-last-success.json').read_text())
        self.assertEqual(success['phase'], 'validated'); self.assertNotIn('in-memory-only', json.dumps(success))
        self.assertTrue(all(v[1] == a.UNIT for v in events if isinstance(v, tuple)))

    def test_failed_health_recovers_offline_forward_without_open_rollback(self):
        record = self.successful_environment()
        with patch.object(ae.e, 'healthy', side_effect=ValueError('disconnected')):
            with self.assertRaises(ValueError): ae.deploy(record)
        self.assertNotIn('resume', self.events)
        self.assertTrue((self.root / ae.PENDING).exists())
        with patch.object(ae.e, 'api', side_effect=AssertionError('offline recovery must not need GitHub')):
            ae.recover()
        self.assertFalse((self.root / ae.PENDING).exists())
        self.assertIn('resume', self.events)

    def test_validated_crash_rechecks_reads_without_stopping_new_work(self):
        self.successful_environment(); self.pending('validated')
        ae.install_dropin(self.tx); self.events.clear()
        with patch.object(ae.e, 'api', side_effect=AssertionError('offline')):
            ae.recover()
        self.assertEqual(self.events, ['after', 'resume'])
        self.assertFalse((self.root / ae.PENDING).exists())

    def test_validated_recovery_restarts_stopped_secure_unit_before_probe(self):
        self.successful_environment(); self.pending('validated')
        ae.install_dropin(self.tx); self.events.clear()
        with patch.object(ae.e, 'active', return_value=False), patch.object(ae.e, 'healthy') as healthy:
            ae.recover()
        self.assertEqual(self.events, [('start', a.UNIT), 'after', 'resume'])
        healthy.assert_called_once_with(a.UNIT, 8080)
        self.assertFalse((self.root / ae.PENDING).exists())

    def test_arming_journal_publishes_boot_guard_before_secure_hold(self):
        record = self.successful_environment()
        def publish(tx):
            self.assertEqual(json.loads((self.root / ae.PENDING).read_text())['phase'], 'arming')
            raise OSError('crash before atomic publish')
        with patch.object(ae, 'install_dropin', side_effect=publish):
            with self.assertRaises(OSError): ae.deploy(record)
        self.assertEqual(json.loads((self.root / ae.PENDING).read_text())['phase'], 'arming')
        self.assertNotIn('resume', self.events)
        ae.recover()
        self.assertFalse((self.root / ae.PENDING).exists())

    def test_secure_hold_native_deferral_propagates_for_notification(self):
        record = self.successful_environment(); calls = 0
        def stop(states):
            nonlocal calls
            calls += 1
            if calls == 2: raise ae.c.Deferred('new watcher work')
        with patch.object(ae.c, 'stop', side_effect=stop):
            with self.assertRaises(ae.c.Deferred): ae.deploy(record)
        self.assertEqual(json.loads((self.root / ae.PENDING).read_text())['phase'], 'secure-hold')
        self.assertNotIn('resume', self.events)


class HttpTests(unittest.TestCase):
    def test_empty_sdk_labels_never_hide_401_in_error_preserving_read(self):
        def result(value):
            return SimpleNamespace(isError=False, content=[SimpleNamespace(type='text', text=json.dumps(value))])
        self.assertEqual(probe.labels_count(result([])), 0)
        with self.assertRaisesRegex(ValueError, 'authenticated'):
            probe.groups_proof(result({'success': False, 'message': 'HTTP 401'}))
        probe.groups_proof(result({'success': True, 'groups': []}))
        with self.assertRaises(ValueError): probe.groups_proof(result([]))

    def test_actual_loopback_401_200_and_health_not_just_status(self):
        state = {'enforce': True, 'connected': True}
        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args): pass
            def do_GET(self):
                if self.path == '/api/health':
                    value = {'success': True, 'connected': state['connected'], 'logged_in': True, 'auth': 'enforce'}; status = 200
                elif self.path == '/api/labels':
                    status = 200 if not state['enforce'] or self.headers.get('Authorization') == 'Bearer synthetic' else 401
                    value = {'success': True, 'labels': []}
                else: status = 302; value = {}
                raw = json.dumps(value).encode(); self.send_response(status); self.end_headers(); self.wfile.write(raw)
        server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        worker = threading.Thread(target=server.serve_forever, daemon=True); worker.start()
        original = probe.http.client.HTTPConnection
        try:
            with patch.object(probe.http.client, 'HTTPConnection', side_effect=lambda host, port, timeout: original(host, server.server_port, timeout=timeout)):
                self.assertEqual(probe.rest('synthetic', True), 0)
                state['connected'] = False
                with self.assertRaisesRegex(ValueError, 'health'): probe.rest('synthetic', True)
                state['connected'] = True; state['enforce'] = False
                with self.assertRaisesRegex(ValueError, 'negative'): probe.rest('synthetic', True)
                self.assertEqual(probe.rest('synthetic', False), 0)
        finally:
            server.shutdown(); server.server_close(); worker.join(timeout=2)


if __name__ == '__main__':
    unittest.main()
