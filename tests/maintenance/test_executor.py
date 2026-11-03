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
            tx=dict(sha='a'*40,previous_sha='b'*40,old_binary=str(base/'old'),schemas={str(store):schemas},consumers={},bridges={})
            e.atomic(base/'pending.json',tx)
            with patch.object(e,'BASE',base),patch.object(e,'BRIDGES',[('bridge',1,store)]),patch.object(e,'CONSUMERS',[]),patch.object(e,'ctl'),patch.object(e,'ensure_clean'),patch.object(e,'run',return_value='a'*40) as run,patch.object(e,'atomic_binary') as binary,patch.object(e,'notify'):
                e.recover()
                binary.assert_called_once_with(base/'old')
                self.assertFalse((base/'pending.json').exists())
                self.assertTrue((base/'blocked.json').exists())
            self.assertTrue((store/'whatsapp.db').exists())

    def test_recovery_refuses_old_binary_on_changed_schema(self):
        with tempfile.TemporaryDirectory() as temporary:
            base=Path(temporary);store=base/'store';store.mkdir()
            e.atomic(base/'pending.json',dict(sha='a'*40,schemas={str(store):{'whatsapp.db':'expected'}}))
            with patch.object(e,'BASE',base),patch.object(e,'BRIDGES',[('bridge',1,store)]),patch.object(e,'CONSUMERS',[]),patch.object(e,'ctl'),patch.object(e,'db_schema',return_value='changed'),patch.object(e,'notify'),patch.object(e,'atomic_binary') as binary,self.assertRaisesRegex(ValueError,'unsafe rollback'):
                e.recover()
            binary.assert_not_called()
            self.assertTrue((base/'pending.json').exists())


if __name__=='__main__': unittest.main()
