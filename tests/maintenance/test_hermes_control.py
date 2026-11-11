from contextlib import closing
import datetime as dt
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/maintenance'))
import hermes_executor
from hermes_control import stable_release,candidate_root,validate_request


class HermesControlTests(unittest.TestCase):
    def effective(self,*argv):
        return ('{ path='+argv[0]+' ; argv[]='+' '.join(argv)+
                ' ; ignore_errors=no ; start_time=[n/a] ; stop_time=[n/a] ; pid=0 ; code=(null) ; status=0/0 }')

    def version_output(self,version,sha,root):
        return (f'Hermes Agent v{version} (2026.9.24) · stable · local {sha[:12]}\n'
                f'Install directory: {root}\nInstall method: git')

    def test_only_aged_stable_releases(self):
        now=dt.datetime.now(dt.timezone.utc)
        release=dict(tag_name='v0.22.5',draft=False,prerelease=False,published_at=(now-dt.timedelta(days=4)).isoformat())
        stable_release(release,'v0.22.5','a'*40,now)
        for change in [dict(draft=True),dict(prerelease=True),dict(tag_name='canary'),dict(published_at=now.isoformat())]:
            with self.subTest(change=change),self.assertRaises(ValueError):stable_release(dict(release,**change),'v0.22.5','a'*40,now)

    def test_reject_paths_outside_fixed_worktrees(self):
        self.assertEqual(candidate_root('/home/thiago/worktrees/hermes-maintenance-test'),Path('/home/thiago/worktrees/hermes-maintenance-test').resolve())
        for path in ['/tmp/hermes-test','/home/thiago/worktrees/../../.ssh','/home/thiago/worktrees/other-repo']:
            with self.assertRaises(ValueError):candidate_root(path)

    def test_request_has_no_arbitrary_command_or_path(self):
        request=dict(schema=1,component='hermes',candidate_sha='a'*40,upstream_sha='b'*40,
                     tag='v0.22.5',receipt_sha256='c'*64,action='check')
        validate_request(request)
        for change in [dict(command='sh'),dict(action='shell'),dict(candidate_sha='bad'),dict(tag='main')]:
            with self.subTest(change=change),self.assertRaises(ValueError):validate_request(dict(request,**change))

    def test_isolated_command_keeps_user_service_bus(self):
        with patch.object(hermes_executor.subprocess,'check_output',return_value=b'active') as call:
            self.assertEqual(hermes_executor.command(['systemctl','--user','show']), 'active')
        env=call.call_args.kwargs['env']
        self.assertEqual(env['XDG_RUNTIME_DIR'],'/run/user/1000')
        self.assertEqual(env['DBUS_SESSION_BUS_ADDRESS'],'unix:path=/run/user/1000/bus')
        self.assertNotIn('ANTHROPIC_API_KEY',env)

    def test_expected_previous_launcher_is_atomically_taken_over_by_candidate(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);previous=root/'previous';candidate_root=root/'candidate'
            previous.write_text('#!/bin/sh\nprevious\n');previous.chmod(0o755)
            candidate=candidate_root/'.hermes/bin/hermes';candidate.parent.mkdir(parents=True)
            candidate.write_text('#!/bin/sh\ncandidate official launcher\n');candidate.chmod(0o755)
            previous_sha='b'*40;candidate_sha='a'*40
            def identify(args,**kwargs):
                if b'candidate official launcher' not in Path(args[0]).read_bytes():
                    return self.version_output('0.22.5+1.gbbbbbbb',previous_sha,root/'old-root')
                return self.version_output('0.22.6+1.gaaaaaaa',candidate_sha,candidate_root)
            with patch.object(hermes_executor,'command',side_effect=identify):
                previous_digest=hermes_executor.common.digest(previous)
                hermes_executor.prove_launcher(previous,previous_sha,previous_digest)
                old_identity=hermes_executor.file_identity(previous)
                hermes_executor.publish_file(candidate,previous,old_identity,mode=0o755)
                candidate_digest=hermes_executor.common.digest(candidate)
                identity=hermes_executor.prove_launcher(previous,candidate_sha,candidate_digest,candidate_root,'v0.22.6')
            self.assertEqual(previous.read_bytes(),candidate.read_bytes())
            quarantined=list(root.glob('.previous.quarantine.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'#!/bin/sh\nprevious\n')
            self.assertEqual(identity['root'],str(candidate_root.resolve()))
            self.assertEqual(previous.stat().st_mode&0o777,0o755)

    def test_spoofed_launcher_is_refused_without_execution_or_mutation(self):
        with tempfile.TemporaryDirectory() as temporary:
            launcher=Path(temporary)/'hermes';launcher.write_text('#!/bin/sh\nforeign\n');launcher.chmod(0o755)
            before=launcher.read_bytes()
            with patch.object(hermes_executor,'command') as command,self.assertRaisesRegex(ValueError,'independently trusted'):
                hermes_executor.prove_launcher(launcher,'b'*40,'0'*64)
            command.assert_not_called()
            self.assertEqual(launcher.read_bytes(),before)

    def test_existing_launcher_ownership_uses_checkout_metadata_without_executing_it(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);root=base/'hermes-old';launcher=root/'.hermes/bin/hermes'
            launcher.parent.mkdir(parents=True);launcher.write_text('trusted wrapper');launcher.chmod(0o755)
            installer=root/'scripts/install.sh';installer.parent.mkdir();installer.write_text('installer')
            units=Path(temporary)/'units';units.mkdir();shim=Path(temporary)/'shim'
            shim.write_bytes(launcher.read_bytes());shim.chmod(0o755)
            (units/'hermes-gateway.service').write_text(f'[Service]\nExecStart={launcher} gateway run\n')
            sha='b'*40
            receipt_dir=base/'receipts'/sha;receipt_dir.mkdir(parents=True);(receipt_dir/'tests.log').write_text('passed')
            receipt=dict(schema=1,candidate_sha=sha,result='passed',runner='official-per-file-private-home',
                         root=str(root),tests_log_sha256=hermes_executor.common.digest(receipt_dir/'tests.log'),
                         installer_sha256=hermes_executor.common.digest(installer),
                         launcher_sha256=hermes_executor.common.digest(launcher))
            (receipt_dir/'receipt.json').write_text(json.dumps(receipt))
            answers=['',self.effective(str(launcher),'gateway','run'),sha,'']
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'UNITS',units),patch.object(hermes_executor,'SHIM',shim),patch.object(hermes_executor,'candidate_root',return_value=root),patch.object(hermes_executor,'command',side_effect=answers) as command:
                identity=hermes_executor.trusted_existing_launcher(sha)
            self.assertEqual(identity['sha256'],hermes_executor.common.digest(shim))
            self.assertFalse(any(call.args[0] and call.args[0][0] in [str(launcher),str(shim)]
                                 for call in command.call_args_list))

    def test_exchange_mismatch_retains_both_entries_without_racy_restoration(self):
        with tempfile.TemporaryDirectory() as temporary:
            target=Path(temporary)/'target';target.write_text('owned')
            expected=hermes_executor.file_identity(target);real_exchange=hermes_executor.rename_exchange
            calls=0
            def race(left,right):
                nonlocal calls
                calls+=1
                if calls==1:target.write_text('foreign mutation')
                real_exchange(left,right)
            with patch.object(hermes_executor,'rename_exchange',side_effect=race),self.assertRaisesRegex(ValueError,'changed Hermes file'):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertEqual(target.read_bytes(),b'candidate')
            quarantined=list(Path(temporary).glob('.target.quarantine.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'foreign mutation')
            self.assertEqual(calls,1)

    def test_post_exchange_symlink_is_preserved_with_displaced_file_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'target';foreign=root/'foreign'
            target.write_text('owned');foreign.write_text('foreign')
            expected=hermes_executor.file_identity(target);real_exchange=hermes_executor.rename_exchange
            def race(left,right):
                real_exchange(left,right)
                target.unlink();target.symlink_to(foreign)
            with patch.object(hermes_executor,'rename_exchange',side_effect=race),self.assertRaises(ValueError):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertTrue(target.is_symlink())
            self.assertEqual(target.resolve(),foreign)
            quarantined=list(root.glob('.target.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'owned')

    def test_post_exchange_directory_is_preserved_with_displaced_file_quarantined(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'target';target.write_text('owned')
            expected=hermes_executor.file_identity(target);real_exchange=hermes_executor.rename_exchange
            def race(left,right):
                real_exchange(left,right)
                target.unlink();target.mkdir();(target/'marker').write_text('foreign')
            with patch.object(hermes_executor,'rename_exchange',side_effect=race),self.assertRaises(ValueError):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertTrue(target.is_dir())
            self.assertEqual((target/'marker').read_text(),'foreign')
            quarantined=list(root.glob('.target.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'owned')

    def test_exchange_exception_after_swap_retains_both_entries_without_second_exchange(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'target';target.write_text('owned')
            expected=hermes_executor.file_identity(target);real_exchange=hermes_executor.rename_exchange
            calls=0
            def fail_after_exchange(left,right):
                nonlocal calls
                calls+=1;real_exchange(left,right)
                if calls==1:raise OSError('synthetic post-exchange failure')
            with patch.object(hermes_executor,'rename_exchange',side_effect=fail_after_exchange),self.assertRaisesRegex(OSError,'synthetic'):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertEqual(target.read_bytes(),b'candidate')
            quarantined=list(root.glob('.target.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'owned')
            self.assertEqual(calls,1)

    def test_post_exchange_verification_exception_retains_both_entries(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'target';target.write_text('owned')
            expected=hermes_executor.file_identity(target)
            with patch.object(hermes_executor,'fsync_directory',side_effect=OSError('synthetic fsync failure')),self.assertRaisesRegex(OSError,'synthetic'):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertEqual(target.read_bytes(),b'candidate')
            quarantined=list(root.glob('.target.*'))
            self.assertEqual(len(quarantined),1)
            self.assertEqual(quarantined[0].read_text(),'owned')

    def test_publication_quarantine_is_bounded_before_new_file_creation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);target=root/'target';target.write_text('owned')
            expected=hermes_executor.file_identity(target)
            for n in range(8):(root/f'.target.quarantine.{n}').write_text('evidence')
            with self.assertRaisesRegex(ValueError,'quarantine limit'):
                hermes_executor.atomic_publish_bytes(target,b'candidate',0o755,expected)
            self.assertEqual(target.read_text(),'owned')
            self.assertEqual(len(list(root.glob('.target.quarantine.*'))),8)

    def test_promotion_preflight_reserves_rollback_slots_before_disruption(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);units=root/'units';units.mkdir();shim=root/'bin/hermes'
            shim.parent.mkdir(parents=True)
            dashboard=units/'hermes-dashboard.service';gateway=units/'hermes-gateway.service'
            for path in [shim,dashboard,gateway]:path.write_text('owned')
            for n in range(6):(shim.parent/f'.{shim.name}.quarantine.{n}').write_text('evidence')
            with patch.object(hermes_executor,'SHIM',shim),patch.object(hermes_executor,'UNITS',units):
                hermes_executor.preflight_publication_capacity(True)
                (shim.parent/f'.{shim.name}.quarantine.6').write_text('evidence')
                with self.assertRaisesRegex(ValueError,'quarantine limit'):
                    hermes_executor.preflight_publication_capacity(True)

    def test_rollback_preflight_needs_only_one_reserved_slot(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);units=root/'units';units.mkdir();shim=root/'bin/hermes'
            shim.parent.mkdir(parents=True)
            for path in [shim,units/'hermes-dashboard.service',units/'hermes-gateway.service']:
                path.write_text('owned')
            for n in range(7):(shim.parent/f'.{shim.name}.quarantine.{n}').write_text('evidence')
            with patch.object(hermes_executor,'SHIM',shim),patch.object(hermes_executor,'UNITS',units):
                hermes_executor.preflight_publication_capacity(False)
                (shim.parent/f'.{shim.name}.quarantine.7').write_text('evidence')
                with self.assertRaisesRegex(ValueError,'quarantine limit'):
                    hermes_executor.preflight_publication_capacity(False)

    def test_dashboard_unit_is_atomically_aligned_to_global_launcher(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);units=root/'units';units.mkdir();shim=root/'bin/hermes'
            shim.parent.mkdir();shim.write_text('candidate launcher');shim.chmod(0o755)
            (units/'hermes-gateway.service').write_text('candidate gateway unit')
            unit=units/'hermes-dashboard.service'
            unit.write_text('[Service]\nExecStart=/old/hermes dashboard --no-open --host 127.0.0.1\nRestart=on-failure\n')
            identity=hermes_executor.file_identity(unit)
            with patch.object(hermes_executor,'UNITS',units),patch.object(hermes_executor,'SHIM',shim),patch.object(
                    hermes_executor,'command',return_value=self.effective(str(shim),'dashboard','--no-open','--host','127.0.0.1')):
                hermes_executor.align_dashboard_unit(identity)
                installed=hermes_executor.installation()
            text=unit.read_text()
            self.assertIn(f'ExecStart={shim} dashboard --no-open --host 127.0.0.1',text)
            self.assertIn('Restart=on-failure',text)
            self.assertEqual(installed,{str(path):hermes_executor.file_identity(path) for path in
                [shim,units/'hermes-gateway.service',unit]})

    def test_gateway_and_dashboard_units_are_semantically_validated(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);units=root/'units';units.mkdir();shim=root/'bin/hermes'
            shim.parent.mkdir();shim.write_text('candidate');shim.chmod(0o755)
            candidate=root/'.hermes/bin/hermes';candidate.parent.mkdir(parents=True);candidate.write_text('candidate')
            gateway=units/'hermes-gateway.service';dashboard=units/'hermes-dashboard.service'
            gateway.write_text(f'[Service]\nExecStart={candidate} gateway run\n')
            dashboard.write_text(f'[Service]\nExecStart={shim} dashboard --no-open\n')
            def effective(args,**kwargs):
                unit=args[3]
                return (self.effective(str(candidate),'gateway','run') if unit=='hermes-gateway.service'
                        else self.effective(str(shim),'dashboard','--no-open'))
            with patch.object(hermes_executor,'UNITS',units),patch.object(hermes_executor,'SHIM',shim),patch.object(hermes_executor,'command',side_effect=effective):
                hermes_executor.validate_gateway_unit(root);hermes_executor.validate_dashboard_unit()
            # A drop-in can override a correct base file; effective systemd
            # metadata, not shlex of the fragment, is authoritative.
            with patch.object(hermes_executor,'command',return_value=self.effective('/foreign/hermes','gateway','run')):
                with self.assertRaisesRegex(ValueError,'candidate checkout'):hermes_executor.validate_gateway_unit(root)
            dashboard.write_text(f'[Service]\nExecStart={shim} dashboard\nExecStart=/bin/true\n')
            with self.assertRaisesRegex(ValueError,'one ExecStart'):hermes_executor.base_unit_exec(dashboard)

    def test_compressed_backup_restores_consistent_data(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);home=root/'home';home.mkdir()
            database=home/'state.db'
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.execute('CREATE TABLE sessions(id INTEGER PRIMARY KEY,value TEXT)')
                connection.execute("INSERT INTO sessions VALUES(1,'synthetic')")
            output=root/'backup.zip'
            with patch.object(hermes_executor,'HOME',home):
                schemas=hermes_executor.snapshot_archive(output)
            with zipfile.ZipFile(output) as archive:
                self.assertEqual(archive.namelist(),['state.db'])
                self.assertIsNone(archive.testzip())
                restored=root/'restored.db';restored.write_bytes(archive.read('state.db'))
            self.assertEqual(hermes_executor.common.db_schema(restored),schemas[str(database)])
            with closing(sqlite3.connect(restored)) as connection:
                self.assertEqual(connection.execute('SELECT value FROM sessions').fetchone()[0],'synthetic')
            self.assertEqual(output.stat().st_mode&0o777,0o600)

    def test_failed_canary_is_not_repeated_on_every_poll(self):
        with tempfile.TemporaryDirectory() as temporary:
            run=dict(id=1,status='completed',conclusion='success',path=hermes_executor.WORKFLOW,
                     workflow_id=7,repository={'full_name':'thiavila/whatsapp-mcp'},
                     head_repository={'full_name':'thiavila/whatsapp-mcp'},head_branch='main',event='workflow_dispatch')
            def api(path):
                if '/workflows/' in path and '/runs?' not in path:return {'id':7}
                if '/runs?' in path:return {'workflow_runs':[run]}
                return run
            with patch.object(hermes_executor,'BASE',Path(temporary)),patch.object(hermes_executor,'recover'),patch.object(hermes_executor.common,'api',side_effect=api),patch.object(hermes_executor,'request_artifact',return_value={'candidate_sha':'a'*40}),patch.object(hermes_executor,'apply',side_effect=ValueError('schema changed')) as apply:
                with self.assertRaises(ValueError):hermes_executor.poll()
                hermes_executor.poll()
                self.assertEqual(apply.call_count,1)

    def test_rollback_block_is_rearmed_only_by_new_control_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary)
            run=dict(id=1,status='completed',conclusion='success',path=hermes_executor.WORKFLOW,
                     workflow_id=7,repository={'full_name':'thiavila/whatsapp-mcp'},
                     head_repository={'full_name':'thiavila/whatsapp-mcp'},head_branch='main',event='workflow_dispatch')
            request={'candidate_sha':'a'*40}
            def api(path):
                if '/workflows/' in path and '/runs?' not in path:return {'id':7}
                if '/runs?' in path:return {'workflow_runs':[run]}
                return run
            module_path=Path(__file__).resolve().parents[2]/'scripts/maintenance/hermes_executor.py'
            current=hermes_executor.common.digest(module_path)
            (base/'blocked.json').write_text(json.dumps({
                'candidate_sha':'a'*40,'control_sha256':current}))
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'recover'),patch.object(hermes_executor.common,'api',side_effect=api),patch.object(hermes_executor,'request_artifact',return_value=request),patch.object(hermes_executor,'apply',return_value=True) as apply:
                hermes_executor.poll()
                apply.assert_not_called()
                (base/'blocked.json').write_text(json.dumps({
                    'candidate_sha':'a'*40,'control_sha256':'b'*64}))
                hermes_executor.poll()
                apply.assert_called_once_with(request,1)
                self.assertEqual(json.loads((base/'last-run.json').read_text()),{'run_id':1})

    def test_canary_never_migrates_archived_or_nested_session_backups(self):
        home=hermes_executor.HOME
        current=[home/'state.db',home/'profiles/memo-vic/state.db']
        historical=[home/'profiles/memo-vic/state-snapshots/old/state.db',home/'profiles/memo-vic/home/.hermes/state.db']
        schemas={str(p):'hash' for p in current+historical}
        copies=Path('/private-copy')
        result=hermes_executor.active_session_copies(copies,schemas)
        self.assertEqual(result,[str(copies/p.relative_to(home)) for p in current])


if __name__=='__main__':unittest.main()
