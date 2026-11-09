from contextlib import closing
import datetime as dt
import json
import sqlite3
import tempfile
import zipfile
from pathlib import Path
import sys
import unittest
from unittest.mock import call,patch
import hermes_executor

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/maintenance'))
from hermes_control import stable_release,candidate_root,validate_request


class HermesControlTests(unittest.TestCase):
    def forward_fixture(self,root):
        home=root/'home';base=root/'deploy';backup=base/'runs/1/backup'
        (home/'cron').mkdir(parents=True);backup.mkdir(parents=True)
        state=home/'state.db';executions=home/'cron/executions.db'
        with closing(sqlite3.connect(state)) as connection,connection:
            connection.execute('CREATE TABLE schema_version (version INTEGER NOT NULL)')
            connection.execute('INSERT INTO schema_version VALUES (32)')
        with closing(sqlite3.connect(executions)) as connection,connection:
            connection.execute("CREATE TABLE executions (id TEXT, status TEXT CHECK(status IN ('claimed','running','completed','failed','unknown')))")
        paths=[state,executions]
        schemas={str(path):hermes_executor.common.db_schema(path) for path in paths}
        archive=backup/'sqlite-backup.zip'
        with zipfile.ZipFile(archive,'w') as saved:
            for path in paths:saved.write(path,str(path.relative_to(home)))
        tx={'candidate_sha':'a'*40,'run_id':1,'backup':str(backup),'schemas':schemas,
            'sqlite_archive_sha256':hermes_executor.common.digest(archive)}
        with closing(sqlite3.connect(state)) as connection,connection:
            connection.execute(hermes_executor.HOSTED_ROOM_CAPACITY_SCHEMA[3])
        with closing(sqlite3.connect(executions)) as connection,connection:
            old=connection.execute("SELECT sql FROM sqlite_master WHERE name='executions'").fetchone()[0]
            connection.execute('ALTER TABLE executions RENAME TO old_executions')
            connection.execute(old.replace(hermes_executor.OLD_EXECUTION_STATUSES,hermes_executor.NEW_EXECUTION_STATUSES))
            connection.execute('INSERT INTO executions SELECT * FROM old_executions')
            connection.execute('DROP TABLE old_executions')
        return home,base,tx,state,executions

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

    def test_forward_schema_gate_accepts_only_the_observed_deltas(self):
        with tempfile.TemporaryDirectory() as temporary:
            home,base,tx,state,executions=self.forward_fixture(Path(temporary))
            with patch.object(hermes_executor,'HOME',home),patch.object(hermes_executor,'BASE',base):
                expected=hermes_executor.forward_schema_state(tx)
                self.assertEqual(expected,hermes_executor.live_schemas())
                with closing(sqlite3.connect(executions)) as connection,connection:
                    connection.execute('CREATE INDEX unapproved ON executions(id)')
                with self.assertRaisesRegex(ValueError,'unapproved forward schema delta'):
                    hermes_executor.forward_schema_state(tx)

    def test_forward_schema_gate_requires_schema_version_32(self):
        with tempfile.TemporaryDirectory() as temporary:
            home,base,tx,state,_=self.forward_fixture(Path(temporary))
            with closing(sqlite3.connect(state)) as connection,connection:
                connection.execute('UPDATE schema_version SET version=33')
            with patch.object(hermes_executor,'HOME',home),patch.object(hermes_executor,'BASE',base):
                with self.assertRaisesRegex(ValueError,'schema_version'):
                    hermes_executor.forward_schema_state(tx)

    def test_pending_request_is_bound_to_original_successful_run_and_receipt(self):
        run={'id':7,'path':hermes_executor.WORKFLOW,'repository':{'full_name':hermes_executor.REPOSITORY},
             'head_repository':{'full_name':hermes_executor.REPOSITORY},'head_branch':'main',
             'event':'workflow_dispatch','status':'completed','conclusion':'success'}
        request={'action':'promote','candidate_sha':'a'*40};root=Path('/candidate')
        with patch.object(hermes_executor.common,'api',return_value=run),\
             patch.object(hermes_executor,'request_artifact',return_value=request) as artifact,\
             patch.object(hermes_executor,'validate_local',return_value=root) as local:
            self.assertEqual(hermes_executor.pending_request({'candidate_sha':'a'*40,'run_id':7}),(request,root))
        artifact.assert_called_once_with(run);local.assert_called_once_with(request)
        with patch.object(hermes_executor.common,'api',return_value=dict(run,conclusion='failure')),\
             patch.object(hermes_executor,'request_artifact') as artifact:
            with self.assertRaisesRegex(ValueError,'provenance'):
                hermes_executor.pending_request({'candidate_sha':'a'*40,'run_id':7})
            artifact.assert_not_called()

    def test_installer_uses_only_official_product_complete_and_gateway_stages(self):
        root=Path('/candidate');destination=Path('/run');request={'candidate_sha':'a'*40}
        with patch.object(hermes_executor,'command',side_effect=['branch',None,None,None]) as command,\
             patch.object(hermes_executor,'validate_local',return_value=root):
            hermes_executor.install_candidate(request,root,destination)
        calls=[item.args[0] for item in command.call_args_list]
        self.assertEqual([args[args.index('--stage')+1] for args in calls if '--stage' in args],['products','complete'])
        self.assertFalse(any('plugin' in str(argument).lower() for args in calls for argument in args))
        self.assertEqual(calls[-1][1:3],['gateway','install'])

    def test_installer_revalidates_candidate_immediately_before_official_install(self):
        root=Path('/candidate');destination=Path('/run');request={'candidate_sha':'a'*40};events=[]
        def command(args,*_,**__):
            events.append('install' if args[0]=='bash' else 'branch')
            return 'branch'
        def validate(_):events.append('validate');return root
        with patch.object(hermes_executor,'command',side_effect=command),\
             patch.object(hermes_executor,'validate_local',side_effect=validate) as local:
            hermes_executor.install_candidate(request,root,destination)
        self.assertEqual(events[:3],['branch','validate','install'])
        local.assert_called_once_with(request)

    def test_installer_does_not_run_if_last_moment_validation_fails(self):
        root=Path('/candidate');request={'candidate_sha':'a'*40}
        with patch.object(hermes_executor,'command',return_value='branch') as command,\
             patch.object(hermes_executor,'validate_local',side_effect=ValueError('candidate changed')):
            with self.assertRaisesRegex(ValueError,'candidate changed'):
                hermes_executor.install_candidate(request,root,Path('/run'))
        command.assert_called_once_with(['git','-C',str(root),'branch','--show-current'])

    def test_recover_completes_valid_forward_install_and_clears_journal(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';home=root/'home';base.mkdir();home.mkdir()
            tx={'candidate_sha':'a'*40,'run_id':1,'schemas':{'db':'old'},'units':{'hermes-gateway.service':True}}
            (base/'pending.json').write_text(json.dumps(tx));(home/'gateway_state.json').write_text(json.dumps({
                'gateway_state':'running','code_sha':'a'*40,'platforms':{'telegram':{'state':'connected'}}}))
            request={'candidate_sha':'a'*40};expected={'db':'forward'};candidate=root/'candidate'
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'HOME',home),\
                 patch.object(hermes_executor,'CONSUMERS',['hermes-gateway.service']),\
                 patch.object(hermes_executor,'live_schemas',side_effect=[expected,expected,expected]),\
                 patch.object(hermes_executor,'pending_request',return_value=(request,candidate)),\
                 patch.object(hermes_executor,'active',return_value=True),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'ctl') as ctl,patch.object(hermes_executor,'install_candidate') as install,\
                 patch.object(hermes_executor,'command'),patch.object(hermes_executor,'resume') as resume,\
                 patch.object(hermes_executor,'healthy') as healthy,patch.object(hermes_executor.common,'notify'):
                hermes_executor.recover()
            install.assert_called_once_with(request,candidate,base/'runs/1')
            ctl.assert_called_once_with('stop','hermes-gateway.service')
            resume.assert_called_once_with(tx['units']);healthy.assert_called_once_with(tx['units'],'a'*40)
            self.assertFalse((base/'pending.json').exists())
            self.assertEqual(json.loads((base/'last-success.json').read_text())['result'],'forward_reconciled')

    def test_forward_recovery_stops_all_consumers_before_remote_or_installer_work(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';home=root/'home';base.mkdir();home.mkdir()
            consumers=['first.service','hermes-gateway.service','last.service'];events=[]
            tx={'candidate_sha':'a'*40,'run_id':1,'schemas':{'db':'old'},'units':{'hermes-gateway.service':True}}
            (base/'pending.json').write_text(json.dumps(tx));(home/'gateway_state.json').write_text(json.dumps({
                'gateway_state':'running','code_sha':'a'*40,'platforms':{'telegram':{'state':'connected'}}}))
            request={'candidate_sha':'a'*40};expected={'db':'forward'};candidate=root/'candidate'
            def stop(action,unit):events.append(('stop',unit))
            def remote(_):events.append(('remote',None));return request,candidate
            def install(*_):events.append(('install',None))
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'HOME',home),\
                 patch.object(hermes_executor,'CONSUMERS',consumers),\
                 patch.object(hermes_executor,'live_schemas',side_effect=[expected,expected,expected]),\
                 patch.object(hermes_executor,'pending_request',side_effect=remote),\
                 patch.object(hermes_executor,'active',return_value=True),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'ctl',side_effect=stop),\
                 patch.object(hermes_executor,'install_candidate',side_effect=install),\
                 patch.object(hermes_executor,'command'),patch.object(hermes_executor,'resume'),\
                 patch.object(hermes_executor,'healthy'),patch.object(hermes_executor.common,'notify'):
                hermes_executor.recover()
            self.assertEqual(events[:3],[('stop',unit) for unit in consumers])
            self.assertEqual(events[3:],[('remote',None),('install',None)])

    def test_forward_recovery_attempts_every_stop_before_reporting_a_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';home=root/'home';base.mkdir();home.mkdir()
            consumers=['first.service','hermes-gateway.service','last.service'];events=[]
            tx={'candidate_sha':'a'*40,'run_id':1,'schemas':{'db':'old'},'units':{'hermes-gateway.service':True}}
            (base/'pending.json').write_text(json.dumps(tx));(home/'gateway_state.json').write_text(json.dumps({
                'gateway_state':'running','code_sha':'a'*40,'platforms':{'telegram':{'state':'connected'}}}))
            def stop(action,unit):
                events.append(('stop',unit))
                if unit=='first.service':raise OSError('synthetic stop failure')
            def notify(*_):events.append(('notify',None))
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'HOME',home),\
                 patch.object(hermes_executor,'CONSUMERS',consumers),\
                 patch.object(hermes_executor,'live_schemas',return_value={'db':'forward'}),\
                 patch.object(hermes_executor,'active',return_value=True),\
                 patch.object(hermes_executor,'ctl',side_effect=stop),\
                 patch.object(hermes_executor,'pending_request') as remote,\
                 patch.object(hermes_executor,'install_candidate') as install,\
                 patch.object(hermes_executor.common,'notify',side_effect=notify):
                with self.assertRaisesRegex(ValueError,'compatible current schemas'):hermes_executor.recover()
            self.assertEqual(events,[*[('stop',unit) for unit in consumers],('notify',None)])
            remote.assert_not_called();install.assert_not_called()
            self.assertTrue((base/'pending.json').exists())

    def test_forward_recovery_fails_closed_before_remote_work_if_gateway_is_not_connected(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';home=root/'home';base.mkdir();home.mkdir()
            tx={'candidate_sha':'a'*40,'run_id':1,'schemas':{'db':'old'},'units':{'hermes-gateway.service':True}}
            (base/'pending.json').write_text(json.dumps(tx));(home/'gateway_state.json').write_text(json.dumps({
                'gateway_state':'running','code_sha':'a'*40,'platforms':{'telegram':{'state':'disconnected'}}}))
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'HOME',home),\
                 patch.object(hermes_executor,'CONSUMERS',['hermes-gateway.service']),\
                 patch.object(hermes_executor,'live_schemas',return_value={'db':'forward'}),\
                 patch.object(hermes_executor,'pending_request') as remote,\
                 patch.object(hermes_executor,'active',return_value=True),patch.object(hermes_executor,'ctl') as ctl,\
                 patch.object(hermes_executor,'install_candidate') as install,patch.object(hermes_executor.common,'notify'):
                with self.assertRaisesRegex(ValueError,'compatible current schemas'):hermes_executor.recover()
            remote.assert_not_called();install.assert_not_called();ctl.assert_called_once_with('stop','hermes-gateway.service')
            self.assertTrue((base/'pending.json').exists())

    def test_forward_recovery_restops_consumers_if_post_resume_health_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';home=root/'home';base.mkdir();home.mkdir()
            consumers=['first.service','hermes-gateway.service'];events=[]
            tx={'candidate_sha':'a'*40,'run_id':1,'schemas':{'db':'old'},'units':{'hermes-gateway.service':True}}
            (base/'pending.json').write_text(json.dumps(tx));(home/'gateway_state.json').write_text(json.dumps({
                'gateway_state':'running','code_sha':'a'*40,'platforms':{'telegram':{'state':'connected'}}}))
            expected={'db':'forward'};request={'candidate_sha':'a'*40}
            def ctl(action,unit):events.append((action,unit))
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'HOME',home),\
                 patch.object(hermes_executor,'CONSUMERS',consumers),\
                 patch.object(hermes_executor,'live_schemas',side_effect=[expected,expected]),\
                 patch.object(hermes_executor,'pending_request',return_value=(request,root/'candidate')),\
                 patch.object(hermes_executor,'active',return_value=True),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'ctl',side_effect=ctl),\
                 patch.object(hermes_executor,'install_candidate'),patch.object(hermes_executor,'command'),\
                 patch.object(hermes_executor,'resume'),\
                 patch.object(hermes_executor,'healthy',side_effect=ValueError('unhealthy')),\
                 patch.object(hermes_executor.common,'notify'):
                with self.assertRaisesRegex(ValueError,'compatible current schemas'):
                    hermes_executor.recover()
            self.assertEqual(events,[('stop',unit) for unit in consumers]*2)
            self.assertTrue((base/'pending.json').exists())

    def test_forward_recovery_restops_consumers_if_resume_partially_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';base.mkdir();(base/'pending.json').write_text('{}')
            consumers=['first.service','hermes-gateway.service'];tx={'candidate_sha':'a'*40,'run_id':1,'units':{}}
            expected={'db':'forward'}
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'CONSUMERS',consumers),\
                 patch.object(hermes_executor,'pending_request',return_value=({'candidate_sha':'a'*40},root/'candidate')),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'live_schemas',return_value=expected),\
                 patch.object(hermes_executor,'install_candidate'),patch.object(hermes_executor,'command'),\
                 patch.object(hermes_executor,'resume',side_effect=RuntimeError('partial resume')),\
                 patch.object(hermes_executor,'healthy') as healthy,patch.object(hermes_executor,'ctl') as ctl:
                with self.assertRaisesRegex(RuntimeError,'partial resume'):
                    hermes_executor.reconcile_forward(tx,'a'*40)
            healthy.assert_not_called()
            self.assertEqual(ctl.call_args_list,[call('stop',unit) for unit in consumers])
            self.assertTrue((base/'pending.json').exists())

    def test_forward_recovery_restops_consumers_if_final_schema_check_fails(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';base.mkdir();(base/'pending.json').write_text('{}')
            consumers=['first.service','hermes-gateway.service'];tx={'candidate_sha':'a'*40,'run_id':1,'units':{}}
            expected={'db':'forward'}
            with patch.object(hermes_executor,'BASE',base),patch.object(hermes_executor,'CONSUMERS',consumers),\
                 patch.object(hermes_executor,'pending_request',return_value=({'candidate_sha':'a'*40},root/'candidate')),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'live_schemas',side_effect=[expected,{'db':'unexpected'}]),\
                 patch.object(hermes_executor,'install_candidate'),patch.object(hermes_executor,'command'),\
                 patch.object(hermes_executor,'resume'),patch.object(hermes_executor,'healthy'),\
                 patch.object(hermes_executor,'ctl') as ctl:
                with self.assertRaisesRegex(ValueError,'schemas changed during reconciliation'):
                    hermes_executor.reconcile_forward(tx,'a'*40)
            self.assertEqual(ctl.call_args_list,[call('stop',unit) for unit in consumers])
            self.assertTrue((base/'pending.json').exists())

    def test_durable_unlink_fsyncs_parent_directory(self):
        with tempfile.TemporaryDirectory() as temporary:
            journal=Path(temporary)/'pending.json';journal.write_text('{}')
            with patch.object(hermes_executor.os,'open',return_value=73) as opened,\
                 patch.object(hermes_executor.os,'fsync') as fsync,\
                 patch.object(hermes_executor.os,'close') as close:
                hermes_executor.durable_unlink(journal)
            self.assertFalse(journal.exists())
            opened.assert_called_once_with(journal.parent,hermes_executor.os.O_RDONLY|hermes_executor.os.O_DIRECTORY)
            fsync.assert_called_once_with(73);close.assert_called_once_with(73)

    def test_forward_notification_failure_does_not_fail_committed_reconciliation(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);base=root/'deploy';base.mkdir();(base/'pending.json').write_text('{}')
            tx={'candidate_sha':'a'*40,'run_id':1,'units':{}};expected={'db':'forward'}
            with patch.object(hermes_executor,'BASE',base),\
                 patch.object(hermes_executor,'pending_request',return_value=({'candidate_sha':'a'*40},root/'candidate')),\
                 patch.object(hermes_executor,'forward_schema_state',return_value=expected),\
                 patch.object(hermes_executor,'state_files',return_value=[]),\
                 patch.object(hermes_executor,'live_schemas',side_effect=[expected,expected]),\
                 patch.object(hermes_executor,'install_candidate'),patch.object(hermes_executor,'command'),\
                 patch.object(hermes_executor,'resume'),patch.object(hermes_executor,'healthy'),\
                 patch.object(hermes_executor.common,'notify',side_effect=RuntimeError('notification unavailable')):
                hermes_executor.reconcile_forward(tx,'a'*40)
            self.assertFalse((base/'pending.json').exists())
            self.assertEqual(json.loads((base/'last-success.json').read_text())['result'],'forward_reconciled')


if __name__=='__main__':unittest.main()
