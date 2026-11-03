from contextlib import closing
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

CODE=Path(__file__).resolve().parents[2]/'scripts/maintenance'
sys.path.insert(0,str(CODE))
import executor as e


class ExecutorTests(unittest.TestCase):
    def test_online_snapshot_is_consistent_and_private(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);store=root/'store';store.mkdir()
            for name in ['whatsapp.db','messages.db']:
                with closing(sqlite3.connect(store/name)) as db, db:
                    db.execute('PRAGMA journal_mode=WAL')
                    db.execute('CREATE TABLE example(id INTEGER PRIMARY KEY, value TEXT)')
                    db.execute("INSERT INTO example VALUES (1,'private')")
            schemas=e.copy_databases(store,root/'backup')
            self.assertEqual(schemas,{n:e.db_schema(root/'backup'/n) for n in schemas})
            self.assertEqual((root/'backup/whatsapp.db').stat().st_mode&0o777,0o600)

    def test_canary_schema_migration_is_rejected_before_live_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            store=Path(temporary)
            for name in ['whatsapp.db','messages.db']:
                with closing(sqlite3.connect(store/name)) as db, db: db.execute('CREATE TABLE original(id INTEGER)')
            expected={n:e.db_schema(store/n) for n in ['whatsapp.db','messages.db']}
            def mutate(*args,**kwargs):
                with closing(sqlite3.connect(store/'whatsapp.db')) as db, db: db.execute('CREATE TABLE new_schema(id INTEGER)')
                return 'STORE_CANARY_OK'
            with patch.object(e,'run',side_effect=mutate),self.assertRaisesRegex(ValueError,'schema'):
                e.offline_canary(Path('/fake-binary'),store,expected)

    def test_artifact_digest_and_members_are_bound_to_release(self):
        with tempfile.TemporaryDirectory() as temporary:
            binary=b'ELF fixture';sha='a'*40
            manifest=dict(schema=1,repository=e.REPOSITORY,sha=sha,binary_sha256=hashlib.sha256(binary).hexdigest())
            def archive(extra=False):
                out=io.BytesIO()
                with zipfile.ZipFile(out,'w') as z:
                    z.writestr('manifest.json',json.dumps(manifest));z.writestr('whatsapp-bridge',binary)
                    if extra:z.writestr('../escape','bad')
                return out.getvalue()
            for extra in [False,True]:
                raw=archive(extra)
                record={'artifacts':[dict(name='mcp-vm-release',expired=False,size_in_bytes=len(raw),id=1,digest='sha256:'+hashlib.sha256(raw).hexdigest())]}
                with patch.object(e,'api',return_value=record),patch.object(e.subprocess,'check_output',return_value=raw):
                    if extra:
                        with self.assertRaises(ValueError):e.artifact(1,sha,Path(temporary)/'out')
                    else:e.artifact(1,sha,Path(temporary)/'out')
            self.assertEqual((Path(temporary)/'out').read_bytes(),binary)
            record['artifacts'][0]['digest']='sha256:'+'0'*64
            with patch.object(e,'api',return_value=record),patch.object(e.subprocess,'check_output',return_value=raw),self.assertRaises(ValueError):
                e.artifact(1,sha,Path(temporary)/'out')

    def test_crash_recovery_keeps_live_data_and_restores_code(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);store=base/'store';store.mkdir()
            for name in ['whatsapp.db','messages.db']:
                with closing(sqlite3.connect(store/name)) as db, db:db.execute('CREATE TABLE live(id INTEGER)')
            schemas={n:e.db_schema(store/n) for n in ['whatsapp.db','messages.db']}
            tx=dict(schema_fingerprint_version=2,sha='a'*40,previous_sha='b'*40,old_binary=str(base/'old'),schemas={str(store):schemas},consumers={},bridges={})
            e.atomic(base/'pending.json',tx)
            with patch.object(e,'BASE',base),patch.object(e,'finish_pending_if_healthy',return_value=False),patch.object(e,'BRIDGES',[('bridge',1,store)]),patch.object(e,'CONSUMERS',[]),patch.object(e,'ctl'),patch.object(e,'ensure_clean'),patch.object(e,'run',return_value='a'*40) as run,patch.object(e,'atomic_binary') as binary,patch.object(e,'notify'):
                e.recover()
                binary.assert_called_once_with(base/'old')
                self.assertFalse((base/'pending.json').exists())
                self.assertTrue((base/'blocked.json').exists())
            self.assertTrue((store/'whatsapp.db').exists())

    def test_recovery_refuses_old_binary_on_changed_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);store=base/'store';store.mkdir()
            e.atomic(base/'pending.json',dict(schema_fingerprint_version=2,sha='a'*40,schemas={str(store):{'whatsapp.db':'expected'}}))
            with patch.object(e,'BASE',base),patch.object(e,'finish_pending_if_healthy',return_value=False),patch.object(e,'BRIDGES',[('bridge',1,store)]),patch.object(e,'CONSUMERS',[]),patch.object(e,'ctl'),patch.object(e,'db_schema',return_value='changed'),patch.object(e,'notify'),patch.object(e,'atomic_binary') as binary,self.assertRaisesRegex(ValueError,'unsafe rollback'):
                e.recover()
            binary.assert_not_called()
            self.assertTrue((base/'pending.json').exists())

    def test_gateway_stop_keeps_official_live_work_refusal(self):
        with patch.object(e,'run',return_value='') as run:
            e.ctl('stop','hermes-gateway.service')
            args=run.call_args.args[0]
            self.assertEqual(args,[e.LAUNCHER,'gateway','stop'])
            self.assertNotIn('--yes',args)

    def test_oneshot_activating_is_busy_and_all_monitors_are_preserved(self):
        with patch.object(e,'run',return_value='activating'):
            self.assertTrue(e.busy_service('deploy-indexa-watch.service'))
        with patch.object(e,'run',return_value='inactive'):
            self.assertFalse(e.busy_service('deploy-indexa-watch.service'))
        self.assertEqual(len(e.WATCH_TIMERS),4)
        with patch.object(e,'ctl') as ctl:
            e.resume_consumers({u:True for u in e.CONSUMERS})
        self.assertEqual([call.args[1] for call in ctl.call_args_list],['hermes-gateway.service',*e.WATCH_TIMERS])

    def test_normal_app_sync_version_is_not_a_schema_migration(self):
        with tempfile.TemporaryDirectory() as temporary:
            database=Path(temporary)/'whatsapp.db'
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.execute('CREATE TABLE whatsmeow_app_state_version(name TEXT PRIMARY KEY,version INTEGER)')
                connection.execute('CREATE TABLE whatsmeow_version(version INTEGER)')
                connection.execute("INSERT INTO whatsmeow_app_state_version VALUES('regular',1)")
                connection.execute('INSERT INTO whatsmeow_version VALUES(10)')
            before=e.db_schema(database)
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.execute('UPDATE whatsmeow_app_state_version SET version=2')
            self.assertEqual(e.db_schema(database),before)
            with closing(sqlite3.connect(database)) as connection,connection:
                connection.execute('UPDATE whatsmeow_version SET version=11')
            self.assertNotEqual(e.db_schema(database),before)

    def test_legacy_journal_is_rebuilt_from_backups_without_stopping_services(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);store=base/'store';store.mkdir()
            backup=base/'releases'/('a'*40)/'backups/0';backup.mkdir(parents=True)
            for name in ['whatsapp.db','messages.db']:
                for directory in [store,backup]:
                    with closing(sqlite3.connect(directory/name)) as db:db.execute('CREATE TABLE stable(id INTEGER)')
            tx=dict(sha='a'*40,schemas={str(store):{'whatsapp.db':'old-hash','messages.db':'old-hash'}})
            with patch.object(e,'BASE',base),patch.object(e,'BRIDGES',[('unit',1,store)]),patch.object(e,'ctl') as ctl:
                upgraded=e.upgrade_journal(tx)
                ctl.assert_not_called()
            self.assertEqual(upgraded['schema_fingerprint_version'],2)
            self.assertEqual(upgraded['schemas'][str(store)]['whatsapp.db'],e.db_schema(backup/'whatsapp.db'))

    def test_healthy_pipeline_candidate_is_finalized_without_code_or_data_rollback(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);sha='a'*40
            candidate=base/'releases'/sha/'whatsapp-bridge';candidate.parent.mkdir(parents=True);candidate.write_bytes(b'verified binary')
            binary=base/'live-binary';binary.write_bytes(candidate.read_bytes())
            state=base/'gateway_state.json';state.write_text(json.dumps({'gateway_state':'running','platforms':{'telegram':{'state':'connected'}}}))
            store=base/'store';store.mkdir()
            tx=dict(sha=sha,previous_sha='b'*40,run_id=1,schemas={str(store):{'whatsapp.db':'schema'}},consumers={'hermes-gateway.service':True})
            e.atomic(base/'pending.json',tx)
            proof=dict(repository={'full_name':e.REPOSITORY},head_repository={'full_name':e.REPOSITORY},workflow_id=7,path=e.WORKFLOW,head_branch='main',head_sha=sha,event='push',status='completed',conclusion='success')
            with patch.object(e,'BASE',base),patch.object(e,'HERMES',base),patch.object(e,'BINARY',binary),patch.object(e,'BRIDGES',[('bridge',1,store)]),patch.object(e,'run',return_value=sha),patch.object(e,'active',return_value=True),patch.object(e,'health',return_value=True),patch.object(e,'db_schema',return_value='schema'),patch.object(e,'api',side_effect=[{'id':7},proof]),patch.object(e,'verify'),patch.object(e,'resume_consumers') as resume,patch.object(e,'ctl') as ctl,patch.object(e,'notify'):
                self.assertTrue(e.finish_pending_if_healthy(tx))
                ctl.assert_not_called();resume.assert_called_once_with(tx['consumers'])
            self.assertEqual(json.loads((base/'last-success.json').read_text())['sha'],sha)
            self.assertFalse((base/'pending.json').exists())
            self.assertEqual(binary.read_bytes(),b'verified binary')


if __name__=='__main__': unittest.main()
