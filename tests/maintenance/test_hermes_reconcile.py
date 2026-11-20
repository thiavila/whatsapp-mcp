from contextlib import closing, contextmanager
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import ANY, patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/maintenance'))
import executor as common
import hermes_executor as hermes
import hermes_reconcile as reconcile


def ledger(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE executions(id TEXT PRIMARY KEY,state TEXT)')
        db.execute("INSERT INTO executions VALUES('one','done')")
    path.chmod(0o600)


def migrate(path):
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE cron_incidents (id TEXT PRIMARY KEY, job_id TEXT, state TEXT)')
        db.execute('CREATE INDEX idx_cron_incidents_job ON cron_incidents(job_id)')
        db.execute('CREATE INDEX idx_cron_incidents_state ON cron_incidents(state)')


class PartialCutoverReconcileTests(unittest.TestCase):
    def spec(self,base,sha=None):
        return {'pending_sha256':reconcile.INCIDENT_PENDING_SHA256,
                'intervention_sha256':reconcile.INCIDENT_INTERVENTION_SHA256,
                'candidate_sha':reconcile.INCIDENT_CANDIDATE_SHA,
                'run_id':reconcile.INCIDENT_RUN_ID,
                'backup':str(base/'runs'/str(reconcile.INCIDENT_RUN_ID)/'backup')}

    def archive_tx(self,base,kind='pending'):
        marker=base/(kind+'.json');marker.write_text('{"owned": true}');marker.chmod(0o600)
        identity=hermes.file_identity(marker);digest=common.digest(marker)
        spec={kind+'_sha256':digest,'candidate_sha':'a'*40,'run_id':42}
        data=(json.dumps(reconcile.tombstone(kind,digest,spec['candidate_sha'],spec['run_id']),
                         sort_keys=True,indent=2)+'\n').encode()
        return {'spec':spec,kind+'_identity':identity,
                'archive_plan':{kind:{'holder':str(base/f'.{kind}.archive.{digest}'),
                                     'destination':str(base/reconcile.ARCHIVE/f'{kind}-{digest}.json'),
                                     'marker_identity':identity,
                                     'tombstone_sha256':hashlib.sha256(data).hexdigest()}}}

    def complete_receipt(self,base):
        spec=self.spec(base);archive=base/reconcile.ARCHIVE;archive.mkdir(mode=0o700)
        evidence_file=base/'identity';evidence_file.write_text('identity');evidence_file.chmod(0o600)
        identity=hermes.file_identity(evidence_file);plans={}
        for kind,original in [('pending',b'pending'),('intervention',b'intervention')]:
            digest=hashlib.sha256(original).hexdigest();spec[kind+'_sha256']=digest
            destination=archive/f'{kind}-{digest}.json';destination.write_bytes(original);destination.chmod(0o600)
            tomb=reconcile.tombstone(kind,digest,spec['candidate_sha'],spec['run_id'])
            data=(json.dumps(tomb,sort_keys=True,indent=2)+'\n').encode()
            (base/f'{kind}.json').write_bytes(data);(base/f'{kind}.json').chmod(0o600)
            plans[kind]={'holder':str(base/f'.{kind}.archive.{digest}'),
                         'destination':str(destination),'marker_identity':identity,
                         'tombstone_sha256':hashlib.sha256(data).hexdigest()}
        def identified(digest):return dict(identity,sha256=digest)
        for kind in ['pending','intervention']:
            plans[kind]['marker_identity']=identified(spec[kind+'_sha256'])
        request={'schema':1,'component':'hermes','candidate_sha':spec['candidate_sha'],
                 'upstream_sha':reconcile.INCIDENT_UPSTREAM_SHA,'tag':reconcile.INCIDENT_TAG,
                 'receipt_sha256':reconcile.INCIDENT_RECEIPT_SHA256,'action':'promote'}
        backup_sources={str(Path(spec['backup'])/name):identity
                        for name in ['hermes-shim','hermes-dashboard.service']}
        installed={path:identity for path in hermes.expected_installation_paths()}
        inventories={profile:{'objects':{},'counts':{},'incidents':None}
                     for profile in reconcile.EXPECTED_PROFILES}
        units={unit:{'active_state':'active','unit_file_state':'enabled'}
               for unit in [*hermes.CONSUMERS,reconcile.DEPLOY_TIMER]}
        install_key=hashlib.sha256(str(Path(reconcile.INCIDENT_CANDIDATE_ROOT).resolve()).encode()).hexdigest()[:16]
        generation_root=base/'partial-cutover-runtime-generations'/reconcile.INCIDENT_CANDIDATE_SHA
        payload_key=hashlib.sha256(str(generation_root.resolve()).encode()).hexdigest()[:16]
        entry='python-'+reconcile.INCIDENT_PYTHON_VERSION+'-'+reconcile.INCIDENT_PYTHON_TARGET
        environment=hermes.HOME/'installs'/install_key/'environments/generation/venv'
        dependency={'schema':1,'install_key':install_key,'payload_install_key':payload_key,
                    'facts_identity':identity,'resolved_lock_identity':identity,
                    'environment':str(environment),'environment_digest':'3'*64,
                    'activation_inputs':{name:{'sha256':'4'*64,'mtime_ns':1}
                                         for name in ['uv.lock','pyproject.toml','pm/lock.json']},
                    'extras':['all'],'stamp':'5'*64,
                    'site_packages':str(environment/'lib/python3.14/site-packages'),
                    'python':{'entry':entry,'artifact_sha256':'6'*64,
                              'tree_digest':'7'*64,'python':'/store/python3'},
                    'selected_python':str(hermes.HOME/'tools'/entry),
                    'install_stamp_identity':identified(reconcile.INCIDENT_INSTALL_STAMP_SHA256),
                    'payload_environment_digest':'8'*64,'payload_python_digest':'9'*64,
                    'manifest_sha256':'a'*64}
        return {'schema':1,'spec':spec,
                'candidate_root':reconcile.INCIDENT_CANDIDATE_ROOT,
                'source_candidate_identity':identity,
                'runtime_generation':str(base/'partial-cutover-runtime-generations'/reconcile.INCIDENT_CANDIDATE_SHA),
                'runtime_generation_digest':'d'*64,'dependency_closure':dependency,
                'candidate_identity':identity,'shim_identity':identity,
                'request':request,'receipt_identity':identified(reconcile.INCIDENT_RECEIPT_SHA256),
                'external_evidence':{'run_id':spec['run_id'],'workflow_id':reconcile.INCIDENT_WORKFLOW_ID,
                                     'check_suite_id':reconcile.INCIDENT_CHECK_SUITE_ID,
                                     'control_sha':reconcile.INCIDENT_CONTROL_SHA,'request':request},
                'pending_identity':identified(spec['pending_sha256']),
                'intervention_identity':identified(spec['intervention_sha256']),
                'sqlite_archive_identity':identified(reconcile.INCIDENT_SQLITE_ARCHIVE_SHA256),
                'backup_sources':backup_sources,
                'database_before':inventories,
                'database_content_before':{name:'b'*64 for name in reconcile.EXPECTED_PROFILES},
                'reconciled_schemas':{},
                'installed_before':installed,'config_hashes':{},
                'telegram_adapters':['telegram'],'gateway_pid':123,'archive_plan':plans,'units':units,
                'phase':'complete','migrated_profiles':list(reconcile.EXPECTED_PROFILES),
                'aligned':['launcher','gateway','dashboard'],'archived':['pending','intervention'],
                'started_at':1.0}

    @contextmanager
    def receipt_incident(self,receipt):
        with patch.object(reconcile,'INCIDENT_PENDING_SHA256',receipt['spec']['pending_sha256']), \
             patch.object(reconcile,'INCIDENT_INTERVENTION_SHA256',receipt['spec']['intervention_sha256']):
            yield

    def journal_tx(self,base,phase='prepared'):
        tx=self.complete_receipt(base);tx['phase']=phase
        rank=reconcile.PHASES.index(phase)
        tx['migrated_profiles']=(list(reconcile.EXPECTED_PROFILES)
                                 if rank>=reconcile.PHASES.index('migrated') else [])
        tx['aligned']=(['launcher','gateway','dashboard'] if rank>=reconcile.PHASES.index('aligned') else [])
        tx['archived']=(['pending','intervention'] if rank>=reconcile.PHASES.index('archived') else [])
        return tx

    def test_only_reviewed_incident_schema_delta_and_empty_table_are_accepted(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'executions.db';ledger(db);before=reconcile.db_inventory(db);migrate(db)
            after=reconcile.db_inventory(db);reconcile.validate_schema_delta(before,after)
            self.assertEqual(after['counts']['executions'],1)
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute("INSERT INTO cron_incidents VALUES('x','job','open')")
            with self.assertRaisesRegex(ValueError,'empty'):
                reconcile.validate_schema_delta(before,reconcile.db_inventory(db))

    def test_extra_schema_change_or_row_change_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'executions.db';ledger(db);before=reconcile.db_inventory(db);migrate(db)
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute('ALTER TABLE executions ADD COLUMN foreign_value TEXT')
            with self.assertRaises(ValueError):reconcile.validate_schema_delta(before,reconcile.db_inventory(db))
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'executions.db';ledger(db);before=reconcile.db_inventory(db);migrate(db)
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute("INSERT INTO executions(id,state) VALUES('two','done')")
            with self.assertRaisesRegex(ValueError,'row counts'):
                reconcile.validate_schema_delta(before,reconcile.db_inventory(db))

    def test_logical_content_digest_detects_same_count_row_mutation(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'executions.db';ledger(db);before=reconcile.db_inventory(db)
            digest=reconcile.db_content_digest(db,before['counts'])
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute("UPDATE executions SET state='mutated' WHERE id='one'")
            self.assertNotEqual(digest,reconcile.db_content_digest(db,before['counts']))

    def test_receipt_hash_mismatch_has_no_journal_service_or_database_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);backup=base/'runs/42/backup';backup.mkdir(parents=True)
            pending=base/'pending.json';pending.write_text('{}');pending.chmod(0o600)
            intervention=base/'intervention.json';intervention.write_text('{}');intervention.chmod(0o600)
            spec=self.spec(base);spec['pending_sha256']='0'*64
            with patch.object(hermes,'BASE',base),patch.object(common,'atomic') as atomic,patch.object(hermes,'ctl') as ctl,patch.object(reconcile,'official_initialize') as initialize:
                with self.assertRaisesRegex(ValueError,'pinned'):
                    reconcile.reconcile(spec)
            atomic.assert_not_called();ctl.assert_not_called();initialize.assert_not_called()
            self.assertEqual(pending.read_text(),'{}');self.assertEqual(intervention.read_text(),'{}')

    def test_argument_coordinates_are_literal_and_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);spec=self.spec(base)
            with patch.object(hermes,'BASE',base):reconcile.validate_spec(spec)
            for key,value in [('candidate_sha','bad'),('run_id',0),('backup','relative')]:
                changed=dict(spec);changed[key]=value
                with self.subTest(key=key),patch.object(hermes,'BASE',base),self.assertRaises(ValueError):
                    reconcile.validate_spec(changed)
            changed=dict(spec);changed['extra']='no'
            with self.assertRaises(ValueError):reconcile.validate_spec(changed)

    def test_alternate_incident_run_and_candidate_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);spec=self.spec(base)
            for key,value in [('run_id',reconcile.INCIDENT_RUN_ID+1),('candidate_sha','a'*40)]:
                changed=dict(spec);changed[key]=value
                with self.subTest(key=key),patch.object(hermes,'BASE',base),self.assertRaisesRegex(ValueError,'pinned'):
                    reconcile.validate_spec(changed)

    def test_crash_after_database_commit_retries_without_second_initializer(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'executions.db';ledger(db);before=reconcile.db_inventory(db)
            with patch.object(reconcile,'EXPECTED_PROFILES',('profile',)):
                tx=self.journal_tx(base,'migrating')
                tx['database_before']={'profile':before}
                tx['database_content_before']={'profile':reconcile.db_content_digest(db,before['counts'])}
                common.atomic(base/reconcile.JOURNAL,tx)
                def initialize(root,path):migrate(path)
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base),patch.object(reconcile,'expected_databases',return_value={'profile':db}),patch.object(reconcile,'verify_candidate',return_value=(base,base/'candidate')),patch.object(reconcile,'enforce_offline'),patch.object(reconcile,'official_initialize',side_effect=initialize) as official,patch.object(reconcile,'persist',side_effect=OSError('crash')):
                    with self.assertRaisesRegex(OSError,'crash'):reconcile.reconcile(tx['spec'])
                    official.assert_called_once()
                self.assertEqual(reconcile.db_inventory(db)['incidents'],0)
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base),patch.object(reconcile,'expected_databases',return_value={'profile':db}),patch.object(reconcile,'verify_candidate',return_value=(base,base/'candidate')),patch.object(reconcile,'enforce_offline'),patch.object(reconcile,'official_initialize') as official,patch.object(reconcile,'persist',side_effect=OSError('later crash')):
                    with self.assertRaisesRegex(OSError,'later crash'):reconcile.reconcile(tx['spec'])
                    official.assert_not_called()

    def test_same_count_initializer_mutation_is_rejected_before_journaling_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'executions.db';ledger(db);before=reconcile.db_inventory(db)
            with patch.object(reconcile,'EXPECTED_PROFILES',('profile',)):
                tx=self.journal_tx(base,'migrating');tx['database_before']={'profile':before}
                tx['database_content_before']={'profile':reconcile.db_content_digest(db,before['counts'])}
                common.atomic(base/reconcile.JOURNAL,tx)
                def corrupt(root,path):
                    migrate(path)
                    with closing(sqlite3.connect(path)) as connection,connection:
                        connection.execute("UPDATE executions SET state='corrupt' WHERE id='one'")
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base), \
                     patch.object(reconcile,'expected_databases',return_value={'profile':db}), \
                     patch.object(reconcile,'verify_candidate',return_value=(base,base/'candidate')), \
                     patch.object(reconcile,'enforce_offline'), \
                     patch.object(reconcile,'official_initialize',side_effect=corrupt), \
                     patch.object(reconcile,'persist') as persist, \
                     self.assertRaisesRegex(ValueError,'contents changed during migration'):
                    reconcile.reconcile(tx['spec'])
                persist.assert_not_called()

    def test_resume_rechecks_already_journaled_profile_contents(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'executions.db';ledger(db);before=reconcile.db_inventory(db)
            with patch.object(reconcile,'EXPECTED_PROFILES',('profile',)):
                tx=self.journal_tx(base,'migrating');tx['database_before']={'profile':before}
                tx['database_content_before']={'profile':reconcile.db_content_digest(db,before['counts'])}
                tx['migrated_profiles']=['profile'];common.atomic(base/reconcile.JOURNAL,tx)
                with closing(sqlite3.connect(db)) as connection,connection:
                    connection.execute("UPDATE executions SET state='corrupt' WHERE id='one'")
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base), \
                     patch.object(reconcile,'expected_databases',return_value={'profile':db}), \
                     patch.object(reconcile,'verify_candidate',return_value=(base,base/'candidate')), \
                     patch.object(reconcile,'enforce_offline'), \
                     patch.object(reconcile,'official_initialize') as initialize, \
                     self.assertRaisesRegex(ValueError,'contents changed before migration'):
                    reconcile.reconcile(tx['spec'])
                initialize.assert_not_called()

    def test_marker_archive_is_hash_bound_and_foreign_race_is_retained(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);marker=base/'pending.json';tx=self.archive_tx(base)
            real=hermes.rename_exchange
            def race(left,right):
                marker.write_text('{"foreign": true}')
                real(left,right)
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange',side_effect=race),self.assertRaisesRegex(ValueError,'foreign marker'):
                reconcile.archive_marker(tx,'pending')
            # The raced entry is preserved at the controller-owned unique name.
            retained=[p for p in base.glob('.pending.archive.*')]
            self.assertEqual(len(retained),1);self.assertEqual(retained[0].read_text(),'{"foreign": true}')
            self.assertTrue(marker.is_file())

    def test_valid_archived_tombstones_clear_only_the_logical_fence(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);last=self.complete_receipt(base)
            common.atomic(base/reconcile.LAST,last)
            with self.receipt_incident(last),patch.object(hermes,'BASE',base):
                self.assertTrue(hermes.reconciled_marker_tombstones())
                self.assertTrue(hermes.archived_reconciliation_marker('intervention'))
                with patch.object(hermes,'recover',return_value=True),patch.object(hermes.common,'api',side_effect=RuntimeError('poll continued')):
                    with self.assertRaisesRegex(RuntimeError,'poll continued'):hermes.poll()
                (base/'pending.json').write_text('{"foreign":true}')
                self.assertFalse(hermes.reconciled_marker_tombstones())
                self.assertFalse(hermes.archived_reconciliation_marker('intervention'))

    def test_tombstones_require_private_complete_exact_receipt(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);receipt=self.complete_receipt(base);path=base/reconcile.LAST
            common.atomic(path,receipt)
            with self.receipt_incident(receipt),patch.object(hermes,'BASE',base):
                self.assertTrue(hermes.reconciled_marker_tombstones())
            for mutation in [lambda value:value.pop('external_evidence'),
                             lambda value:value.update(phase='last-published')]:
                changed=json.loads(json.dumps(receipt));mutation(changed);common.atomic(path,changed)
                with self.receipt_incident(receipt),patch.object(hermes,'BASE',base):
                    self.assertFalse(hermes.reconciled_marker_tombstones())
            common.atomic(path,receipt);path.chmod(0o644)
            with self.receipt_incident(receipt),patch.object(hermes,'BASE',base):
                self.assertFalse(hermes.reconciled_marker_tombstones())

    def test_exact_production_legacy_marker_schema_is_accepted(self):
        spec=self.spec(Path('/private'))
        pending={'backup':spec['backup'],'candidate_sha':spec['candidate_sha'],
                 'previous_sha':reconcile.INCIDENT_PREVIOUS_SHA,'run_id':spec['run_id'],'schemas':{},
                 'sqlite_archive_sha256':reconcile.INCIDENT_SQLITE_ARCHIVE_SHA256,'units':{}}
        intervention={'candidate_sha':spec['candidate_sha'],'reason':'schema-incompatible','run_id':spec['run_id']}
        reconcile.validate_legacy_marker_payloads(pending,intervention,spec)
        self.assertNotIn('phase',pending)
        pending['phase']='starting'
        with self.assertRaisesRegex(ValueError,'exact reviewed legacy schema'):
            reconcile.validate_legacy_marker_payloads(pending,intervention,spec)

    def test_exact_legacy_receipt_builds_candidate_bound_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);sha=reconcile.INCIDENT_CANDIDATE_SHA;root=base/'candidate';(root/'scripts').mkdir(parents=True)
            (root/'scripts/install.sh').write_text('installer');(root/'.hermes/bin').mkdir(parents=True)
            launcher=root/'.hermes/bin/hermes';launcher.write_text('launcher');launcher.chmod(0o755)
            receipt_dir=base/'receipts'/sha;receipt_dir.mkdir(parents=True)
            tests=receipt_dir/'tests.log';tests.write_text('passed');tests.chmod(0o600)
            receipt={'schema':1,'candidate_sha':sha,'upstream_sha':reconcile.INCIDENT_UPSTREAM_SHA,
                     'tag':reconcile.INCIDENT_TAG,'root':str(root),'tests':['fixture'],
                     'tests_log_sha256':common.digest(tests),'result':'passed',
                     'runner':'official-per-file-private-home',
                     'installer_sha256':common.digest(root/'scripts/install.sh')}
            path=receipt_dir/'receipt.json';path.write_text(json.dumps(receipt));path.chmod(0o600)
            answers=[sha,'',''];evidence={'run_id':reconcile.INCIDENT_RUN_ID}
            runtime={'tree_digest':'trusted'}
            with patch.object(hermes,'BASE',base),patch.object(hermes,'candidate_root',return_value=root),patch.object(hermes,'command',side_effect=answers),patch.object(reconcile,'INCIDENT_RECEIPT_SHA256',common.digest(path)),patch.object(reconcile,'INCIDENT_CANDIDATE_ROOT',str(root)),patch.object(reconcile,'official_launcher_identity',return_value=(hermes.file_identity(launcher),runtime)),patch.object(reconcile,'official_python_closure',return_value=runtime),patch.object(reconcile,'external_request',return_value=evidence) as external:
                request,actual,identity,actual_evidence=reconcile.legacy_request(sha,reconcile.INCIDENT_RUN_ID)
            self.assertEqual(actual,root);self.assertEqual(request['candidate_sha'],sha)
            self.assertEqual(request['receipt_sha256'],identity['sha256'])
            self.assertEqual(actual_evidence,evidence);external.assert_called_once_with(reconcile.INCIDENT_RUN_ID,request)

    def test_launcher_mutation_is_rejected_despite_matching_receipt_and_request(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);sha=reconcile.INCIDENT_CANDIDATE_SHA;root=base/'candidate'
            (root/'scripts').mkdir(parents=True);(root/'.hermes/bin').mkdir(parents=True)
            (root/'scripts/install.sh').write_text('installer')
            launcher=root/'.hermes/bin/hermes';launcher.write_text('trusted launcher');launcher.chmod(0o755)
            receipt_dir=base/'receipts'/sha;receipt_dir.mkdir(parents=True)
            tests=receipt_dir/'tests.log';tests.write_text('passed');tests.chmod(0o600)
            receipt={'schema':1,'candidate_sha':sha,'upstream_sha':reconcile.INCIDENT_UPSTREAM_SHA,
                     'tag':reconcile.INCIDENT_TAG,'root':str(root),'tests':['fixture'],
                     'tests_log_sha256':common.digest(tests),'result':'passed',
                     'runner':'official-per-file-private-home',
                     'installer_sha256':common.digest(root/'scripts/install.sh')}
            path=receipt_dir/'receipt.json';path.write_text(json.dumps(receipt));path.chmod(0o600)
            def matched_then_mutated(run_id,request):
                launcher.write_text('mutated launcher');launcher.chmod(0o755)
                return {'run_id':run_id,'request':request}
            runtime={'tree_digest':'trusted'}
            with patch.object(hermes,'BASE',base),patch.object(hermes,'candidate_root',return_value=root),patch.object(hermes,'command',side_effect=[sha,'','']),patch.object(reconcile,'INCIDENT_RECEIPT_SHA256',common.digest(path)),patch.object(reconcile,'INCIDENT_CANDIDATE_ROOT',str(root)),patch.object(reconcile,'official_launcher_identity',return_value=(hermes.file_identity(launcher),runtime)),patch.object(reconcile,'official_python_closure',return_value=runtime),patch.object(reconcile,'external_request',side_effect=matched_then_mutated):
                with self.assertRaisesRegex(ValueError,'launcher changed'):
                    reconcile.legacy_request(sha,reconcile.INCIDENT_RUN_ID)

    def test_launcher_is_reminted_from_immutable_pinned_official_generator(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'official';root.mkdir()
            (root/'hermes_cli').mkdir();(root/'pm').mkdir()
            (root/'hermes_cli/__init__.py').write_text('')
            (root/'pm/__init__.py').write_text('')
            (root/'pm/filesystem.py').write_text('def is_junction(path): return False\n')
            (root/'pm/store.py').write_text(
                "import hashlib, os\nfrom pathlib import Path\nfrom pm.filesystem import is_junction\n"
                "def tree_digest(root):\n"
                " files=[]\n"
                " for d,ds,fs in os.walk(root):\n"
                "  keep=[]\n"
                "  for n in sorted(ds):\n"
                "   p=Path(d)/n\n"
                "   if p.is_symlink() or is_junction(p): files.append((p.relative_to(root).as_posix(),p))\n"
                "   elif n!='__pycache__': keep.append(n)\n"
                "  ds[:]=keep\n"
                "  for n in fs:\n"
                "   p=Path(d)/n; files.append((p.relative_to(root).as_posix(),p))\n"
                " h=hashlib.sha256()\n"
                " for rel,p in sorted(files):\n"
                "  h.update(rel.encode()); h.update(b'\\0')\n"
                "  h.update(os.readlink(p).encode() if p.is_symlink() else p.read_bytes())\n"
                " return h.hexdigest()\n")
            generator=("from pathlib import Path\n"
                       "def mint_launcher(name,repo_root,out_dir,python_exe,site_packages):\n"
                       " p=Path(out_dir)/name\n"
                       " body='#!/bin/sh\\nexec %s --official %s $@\\n' % (python_exe,Path(repo_root))\n"
                       " p.write_text(body)\n"
                       " p.chmod(0o755)\n"
                       " return p\n")
            (root/'hermes_cli/_launchers.py').write_text(generator)
            (root/'hermes_constants.py').write_text('')
            subprocess.run(['git','init','-q'],cwd=root,check=True)
            subprocess.run(['git','config','user.email','test@example.invalid'],cwd=root,check=True)
            subprocess.run(['git','config','user.name','test'],cwd=root,check=True)
            version='fixture';target='linux-x64';entry='python-'+version+'-'+target
            python=root/'.hermes/tools'/entry/'bin/python3';real=python.with_name('python3.14')
            python.parent.mkdir(parents=True);real.write_text('#!/bin/sh\n');real.chmod(0o755);python.symlink_to(real.name)
            artifact={'url':'https://example.invalid/python.tar.gz','sha256':'a'*64}
            (root/'pm/lock.json').write_text(json.dumps({'schema':1,'packages':{'python':{
                'version':version,'artifacts':{target:artifact}}}}))
            subprocess.run(['git','add','.'],cwd=root,check=True)
            subprocess.run(['git','commit','-qm','official fixture'],cwd=root,check=True)
            sha=subprocess.run(['git','rev-parse','HEAD'],cwd=root,check=True,capture_output=True,text=True).stdout.strip()
            launcher=root/'.hermes/bin/hermes';launcher.parent.mkdir(parents=True)
            launcher.write_text('#!/bin/sh\nexec '+str(python)+' --official '+str(root)+' $@\n');launcher.chmod(0o755)
            with patch.object(reconcile,'INCIDENT_CANDIDATE_SHA',sha), \
                 patch.object(reconcile,'INCIDENT_PYTHON_VERSION',version), \
                 patch.object(reconcile,'INCIDENT_PYTHON_TARGET',target), \
                 patch.object(reconcile,'INCIDENT_PYTHON_ARTIFACT_URL',artifact['url']), \
                 patch.object(reconcile,'INCIDENT_PYTHON_ARTIFACT_SHA256',artifact['sha256']), \
                 patch.object(reconcile,'INCIDENT_PYTHON_TREE_DIGEST',reconcile.official_tree_digest(root,root/'.hermes/tools'/entry)):
                identity,proof=reconcile.official_launcher_identity(root,launcher)
                self.assertEqual(identity['sha256'],common.digest(launcher))
                self.assertEqual(proof['artifact_sha256'],artifact['sha256'])
                launcher.write_bytes(launcher.read_bytes()+b'# mutation\n');launcher.chmod(0o755)
                with self.assertRaisesRegex(ValueError,'differs from immutable official install logic'):
                    reconcile.official_launcher_identity(root,launcher)

    def test_runtime_closure_rejects_dangling_external_and_special_entries(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);runtime=root/'runtime';runtime.mkdir();inside=runtime/'inside';inside.write_text('ok')
            (runtime/'internal').symlink_to('inside');reconcile.validate_runtime_tree(runtime)
            for name,target in [('dangling','missing'),('external',str(root/'outside'))]:
                link=runtime/name;link.symlink_to(target)
                with self.subTest(name=name),self.assertRaises(ValueError):reconcile.validate_runtime_tree(runtime)
                link.unlink()
            fifo=runtime/'fifo';os.mkfifo(fifo)
            with self.assertRaisesRegex(ValueError,'special'):reconcile.validate_runtime_tree(runtime)

    def test_marker_digest_rejects_fifo_before_open_and_never_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            fifo=Path(tmp)/'marker';os.mkfifo(fifo)
            with patch.object(os,'open',side_effect=AssertionError('FIFO must not be opened')) as opened:
                with self.assertRaisesRegex(ValueError,'regular file'):reconcile.marker_digest(fifo)
            opened.assert_not_called()

    def test_protected_generation_is_non_writable_and_digest_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);container=base/'partial-cutover-runtime-generations';container.mkdir(mode=0o700)
            generation=container/reconcile.INCIDENT_CANDIDATE_SHA;generation.mkdir()
            executable=generation/'python';executable.write_text('#!/bin/sh\n');executable.chmod(0o755)
            data=generation/'venv/lib/python3.14/site-packages/module.py';data.parent.mkdir(parents=True)
            data.write_text('trusted = True\n')
            reconcile.lock_runtime_generation(generation)
            digest=reconcile.validate_runtime_generation(generation)
            self.assertEqual(container.stat().st_mode&0o777,0o500)
            self.assertFalse((generation.stat().st_mode|data.stat().st_mode|executable.stat().st_mode)&0o222)
            self.assertEqual(reconcile.validate_runtime_generation(generation,digest),digest)
            data.chmod(0o600);data.write_text('trusted = False\n')
            with self.assertRaisesRegex(ValueError,'writable|digest'):
                reconcile.validate_runtime_generation(generation,digest)

    def test_official_assembler_builds_root_payload_and_internalizes_venv_links(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'payload';out.mkdir()
            (out/'pyproject.toml').write_text(
                '[project]\nname="hermes-agent"\nversion="0.0.0"\n'
                '[project.scripts]\nhermes="hermes_cli.main:main"\n'
                'hermes-agent="agent.legacy_cli:main"\nhermes-acp="acp_adapter.entry:main"\n')
            python=out/'tools/python-fixture/bin/python3';python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\n');python.chmod(0o755)
            site=out/'venv/lib/python3.14/site-packages';site.mkdir(parents=True)
            bindir=out/'venv/bin';bindir.mkdir();(out/'venv/pyvenv.cfg').write_text('version = 3.14.9\n')
            (bindir/'python').symlink_to(python)
            with patch.object(reconcile,'INCIDENT_PYTHON_TARGET','linux-x64'):
                manifest=reconcile.run_official_assembler(
                    Path(reconcile.INCIDENT_CANDIDATE_ROOT),out,python,site)
            self.assertEqual(manifest['repo'],'.');self.assertEqual(manifest['venv'],'venv')
            self.assertEqual(manifest['store'],'tools');self.assertEqual(manifest['runtime']['commands']['hermes'],'bin/hermes')
            self.assertFalse(os.path.isabs(os.readlink(bindir/'python')))
            self.assertEqual((bindir/'python').resolve(),python.resolve())

    def test_official_payload_relocator_rejects_external_venv_link(self):
        with tempfile.TemporaryDirectory() as tmp:
            out=Path(tmp)/'payload';out.mkdir()
            (out/'pyproject.toml').write_text(
                '[project]\nname="hermes-agent"\nversion="0.0.0"\n'
                '[project.scripts]\nhermes="hermes_cli.main:main"\n'
                'hermes-agent="agent.legacy_cli:main"\nhermes-acp="acp_adapter.entry:main"\n')
            python=out/'tools/python-fixture/bin/python3';python.parent.mkdir(parents=True)
            python.write_text('#!/bin/sh\n');python.chmod(0o755)
            site=out/'venv/lib/python3.14/site-packages';site.mkdir(parents=True)
            bindir=out/'venv/bin';bindir.mkdir();(out/'venv/pyvenv.cfg').write_text('version = 3.14.9\n')
            (bindir/'bad').symlink_to('/etc/passwd')
            with patch.object(reconcile,'INCIDENT_PYTHON_TARGET','linux-x64'),self.assertRaisesRegex(ValueError,'assembly'):
                reconcile.run_official_assembler(Path(reconcile.INCIDENT_CANDIDATE_ROOT),out,python,site)

    def test_dependency_plan_requires_current_facts_generation_lock_and_pinned_inputs(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'.hermes';candidate=base/'candidate';source=base/'source'
            key='1'*16;state=home/'installs'/key;generation=state/'environments/generation'
            environment=generation/'venv';site=environment/'lib/python3.14/site-packages';site.mkdir(parents=True)
            (environment/'pyvenv.cfg').write_text('version = 3.14.9\n')
            runtime=candidate/'.hermes/tools/python-fixture';real=runtime/'bin/python3.14'
            real.parent.mkdir(parents=True);real.write_text('#!/bin/sh\n');real.chmod(0o755)
            selected_runtime=home/'tools/python-fixture';selected_real=selected_runtime/'bin/python3.14'
            selected_real.parent.mkdir(parents=True);selected_real.write_text('#!/bin/sh\n');selected_real.chmod(0o755)
            (environment/'bin').mkdir();(environment/'bin/python').symlink_to(selected_real)
            stamps=state/'inputs';stamps.mkdir(parents=True);(stamps/'.project-root').write_text(str(candidate.resolve()))
            inputs={}
            for name,data in [('uv.lock',b'lock'),('pyproject.toml',b'project'),('pm/lock.json',b'pm')]:
                live=candidate/name;pinned=source/name;live.parent.mkdir(parents=True,exist_ok=True)
                pinned.parent.mkdir(parents=True,exist_ok=True);live.write_bytes(data);pinned.write_bytes(data)
                stamp=stamps/name;stamp.parent.mkdir(parents=True,exist_ok=True);stamp.touch()
                os.utime(stamp,ns=(live.stat().st_mtime_ns,live.stat().st_mtime_ns));inputs[name]=live
            resolved=generation/'workspace/uv.lock';resolved.parent.mkdir();resolved.write_bytes(b'lock')
            fact={'environment':str(environment.resolve()),'resolved_lock':str(resolved.resolve()),
                  'stamp':'2'*64,'extras':['all']}
            facts=state/'facts.json';facts.write_text(json.dumps({'schema':1,'packages':{'venv':fact}}))
            selection={'install_key':key,'state':str(state),'facts':str(facts),
                       'environment':str(environment),'site_packages':str(site)}
            install_stamp=candidate/'install-stamp.json';install_stamp.write_text('stamp')
            proof={'entry':'python-fixture','artifact_sha256':'3'*64,
                   'tree_digest':'5'*64,'python':str(runtime/'bin/python3')}
            with patch.object(hermes,'HOME',home),patch.object(reconcile,'official_dependency_selection',return_value=selection), \
                 patch.object(reconcile,'official_tree_digest',return_value='5'*64), \
                 patch.object(reconcile,'INCIDENT_ENVIRONMENT_TREE_DIGEST','5'*64), \
                 patch.object(reconcile,'INCIDENT_INSTALL_STAMP_SHA256',common.digest(install_stamp)):
                value=reconcile.dependency_closure_plan(candidate,source,proof)
            self.assertEqual(value['install_key'],key);self.assertEqual(value['resolved_lock_identity']['sha256'],value['activation_inputs']['uv.lock']['sha256'])
            foreign=dict(selection,environment=str(base/'foreign/venv'))
            with patch.object(hermes,'HOME',home),patch.object(reconcile,'official_dependency_selection',return_value=foreign),self.assertRaisesRegex(ValueError,'outside'):
                reconcile.dependency_closure_plan(candidate,source,proof)

    def test_initializer_uses_payload_python_for_multiple_homes_without_install_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);root=base/'payload';root.mkdir();python=root/'tools/python/bin/python3'
            site=root/'venv/lib/python3.14/site-packages';site.mkdir(parents=True);python.parent.mkdir(parents=True)
            python.symlink_to(Path(sys.executable).resolve())
            cron=root/'cron';cron.mkdir();(cron/'__init__.py').write_text('')
            module=("import os,sqlite3\nfrom pathlib import Path\n"
                    "def _connect():\n"
                    " p=Path(os.environ['HERMES_HOME'])/'cron/executions.db'\n"
                    " db=sqlite3.connect(p); db.execute('CREATE TABLE IF NOT EXISTS initialized(value TEXT)'); return db\n")
            (cron/'executions.py').write_text(module);(cron/'incidents.py').write_text(module)
            (root/'manifest.json').write_text(json.dumps(
                {'runtime':{'storePython':'tools/python/bin/python3','sitePackages':'venv/lib/python3.14/site-packages'}}))
            calls=[];real_run=subprocess.run
            def run(argv,**kwargs):calls.append((argv,kwargs));return real_run(argv,**kwargs)
            with patch.object(hermes,'HOME',base/'default'),patch.object(reconcile,'payload_install_key',return_value='1'*16), \
                 patch.object(subprocess,'run',side_effect=run):
                for name in ['one','two']:
                    db=base/name/'cron/executions.db';db.parent.mkdir(parents=True)
                    reconcile.official_initialize(root,db)
                    self.assertFalse((base/name/'installs'/('1'*16)).exists())
                    with closing(sqlite3.connect(db)) as connection:
                        self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master WHERE name='initialized'").fetchone()[0],1)
            self.assertEqual(len(calls),2)
            self.assertTrue(all(call[0][0]==str(python) and 'run-in-hermes-env' not in ' '.join(call[0]) for call in calls))
            self.assertTrue(all(call[1]['env']['HERMES_HOME'] in {str(base/'one'),str(base/'two')} for call in calls))

    def test_alternate_release_tag_is_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);sha=reconcile.INCIDENT_CANDIDATE_SHA;root=base/'candidate'
            (root/'scripts').mkdir(parents=True);(root/'.hermes/bin').mkdir(parents=True)
            (root/'scripts/install.sh').write_text('installer')
            launcher=root/'.hermes/bin/hermes';launcher.write_text('launcher');launcher.chmod(0o755)
            receipt_dir=base/'receipts'/sha;receipt_dir.mkdir(parents=True)
            tests=receipt_dir/'tests.log';tests.write_text('passed');tests.chmod(0o600)
            receipt={'schema':1,'candidate_sha':sha,'upstream_sha':reconcile.INCIDENT_UPSTREAM_SHA,
                     'tag':'v0.22.7','root':str(root),'tests':['fixture'],
                     'tests_log_sha256':common.digest(tests),'result':'passed',
                     'runner':'official-per-file-private-home',
                     'installer_sha256':common.digest(root/'scripts/install.sh')}
            path=receipt_dir/'receipt.json';path.write_text(json.dumps(receipt));path.chmod(0o600)
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'INCIDENT_RECEIPT_SHA256',common.digest(path)),patch.object(reconcile,'external_request') as external:
                with self.assertRaisesRegex(ValueError,'retained Hermes incident'):
                    reconcile.legacy_request(sha,reconcile.INCIDENT_RUN_ID)
            external.assert_not_called()

    def test_legacy_run_42478485171_requires_matching_published_artifact(self):
        request={'schema':1,'component':'hermes','candidate_sha':'a'*40,'upstream_sha':'b'*40,
                 'tag':'v0.22.6','receipt_sha256':'c'*64,'action':'promote'}
        workflow={'id':reconcile.INCIDENT_WORKFLOW_ID,'path':hermes.WORKFLOW}
        run={'id':reconcile.INCIDENT_RUN_ID,'path':hermes.WORKFLOW,'workflow_id':workflow['id'],
             'repository':{'full_name':common.REPOSITORY},
             'head_repository':{'full_name':common.REPOSITORY},'head_branch':'main',
             'event':'workflow_dispatch','status':'completed','conclusion':'success',
             'check_suite_id':reconcile.INCIDENT_CHECK_SUITE_ID,'head_sha':reconcile.INCIDENT_CONTROL_SHA}
        with patch.object(common,'api',side_effect=[workflow,run]),patch.object(hermes,'request_artifact',return_value=request):
            evidence=reconcile.external_request(reconcile.INCIDENT_RUN_ID,request)
        self.assertEqual(evidence['run_id'],reconcile.INCIDENT_RUN_ID)
        with patch.object(common,'api',side_effect=[workflow,run]),patch.object(hermes,'request_artifact',return_value=dict(request,action='check')):
            with self.assertRaisesRegex(ValueError,'published Hermes request'):
                reconcile.external_request(reconcile.INCIDENT_RUN_ID,request)

    def test_backup_archive_hash_private_file_and_symlink_are_enforced(self):
        with tempfile.TemporaryDirectory() as tmp:
            backup=Path(tmp);archive=backup/'sqlite-backup.zip';archive.write_bytes(b'archive');archive.chmod(0o600)
            pending={'sqlite_archive_sha256':common.digest(archive)}
            with patch.object(reconcile,'INCIDENT_SQLITE_ARCHIVE_SHA256',pending['sqlite_archive_sha256']):
                self.assertEqual(reconcile.validate_legacy_archive(backup,pending)['sha256'],pending['sqlite_archive_sha256'])
            pending['sqlite_archive_sha256']='0'*64
            with self.assertRaisesRegex(ValueError,'hash mismatch'):reconcile.validate_legacy_archive(backup,pending)
            archive.unlink();target=backup/'target';target.write_bytes(b'archive');target.chmod(0o600);archive.symlink_to(target)
            pending['sqlite_archive_sha256']=common.digest(target)
            with self.assertRaisesRegex(ValueError,'private regular file'):
                reconcile.validate_legacy_archive(backup,pending)

    def test_wal_backed_database_inventory_reads_committed_wal(self):
        with tempfile.TemporaryDirectory() as tmp:
            db=Path(tmp)/'executions.db';ledger(db)
            connection=sqlite3.connect(db)
            try:
                self.assertEqual(connection.execute('PRAGMA journal_mode=WAL').fetchone()[0],'wal')
                connection.execute("INSERT INTO executions VALUES('wal-row','done')");connection.commit()
                self.assertEqual(reconcile.db_inventory(db)['counts']['executions'],2)
            finally:connection.close()

    def test_executor_verb_refuses_gateway_cgroup_before_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)/'absent'
            args=SimpleNamespace(pending_sha256='a',intervention_sha256='b',candidate_sha='c',run_id=1,backup='/x')
            original=Path.read_text
            def read(path,*a,**kw):
                if str(path)=='/proc/self/cgroup':return '0::/user.slice/hermes-gateway.service'
                return original(path,*a,**kw)
            with patch.object(common,'BASE',base),patch.object(Path,'read_text',read),self.assertRaisesRegex(ValueError,'outside'):
                common.reconcile_hermes(args)
            self.assertFalse(base.exists())

    def test_resume_reasserts_active_services_offline(self):
        units={*common.WATCH_TIMERS,*common.WATCH_SERVICES,
               'hermes-gateway.service','hermes-dashboard.service'}
        states={unit:('inactive' if unit in common.WATCH_SERVICES else 'active') for unit in units}
        calls=[]
        def stop(action,unit):
            calls.append((action,unit));states[unit]='inactive'
        def state(unit):return {'active_state':states[unit],'unit_file_state':'enabled'}
        with patch.object(reconcile,'unit_state',side_effect=state),patch.object(hermes,'ctl',side_effect=stop),patch.object(common,'gateway_quiet',return_value=True):
            reconcile.enforce_offline({})
        self.assertTrue(all(value=='inactive' for value in states.values()))
        self.assertEqual({unit for _,unit in calls},units)

    def test_offline_gate_stops_transitional_states_and_rejects_any_that_remain(self):
        units=[*common.WATCH_TIMERS,*common.WATCH_SERVICES,
               'hermes-gateway.service','hermes-dashboard.service']
        targets=[common.WATCH_TIMERS[0],common.WATCH_SERVICES[0],
                 'hermes-gateway.service','hermes-dashboard.service']
        for transitional in ['activating','reloading','deactivating','mystery-transition']:
            for target in targets:
                with self.subTest(active_state=transitional,target=target):
                    reads={unit:0 for unit in units}
                    def state(unit):
                        reads[unit]+=1
                        value=transitional if unit==target else 'failed'
                        return {'active_state':value,'unit_file_state':'enabled'}
                    with patch.object(reconcile,'unit_state',side_effect=state),patch.object(hermes,'ctl'),patch.object(common,'gateway_quiet',return_value=True):
                        with self.assertRaisesRegex(ValueError,'active|did not remain offline'):
                            reconcile.enforce_offline({})
                    if target not in common.WATCH_SERVICES:
                        self.assertGreaterEqual(reads[target],2)

    def test_unit_state_preserves_unknown_transitional_state_for_fail_closed_gate(self):
        with patch.object(hermes,'command',return_value='ActiveState=activating\nUnitFileState=enabled'):
            self.assertEqual(reconcile.unit_state('watch.timer')['active_state'],'activating')

    def test_preflight_rejects_every_transitional_active_state(self):
        terminal={'other.service':{'active_state':'active','unit_file_state':'enabled'}}
        for state in ['activating','deactivating','reloading','maintenance','refreshing','mystery']:
            units=dict(terminal,**{'target.service':{'active_state':state,'unit_file_state':'enabled'}})
            with self.subTest(state=state),self.assertRaisesRegex(ValueError,'transitional systemd'):
                reconcile.validate_preflight_unit_states(units)

    def test_partial_reconciliation_journal_fences_all_ordinary_recovery_and_polling(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);(base/reconcile.JOURNAL).write_text('{}')
            with patch.object(hermes,'BASE',base),patch.object(hermes,'reconciled_marker_tombstones') as tombs,patch.object(hermes,'common') as mocked_common:
                self.assertFalse(hermes.recovery_barrier())
                self.assertFalse(hermes.recover())
                self.assertFalse(hermes.poll())
            tombs.assert_not_called();mocked_common.api.assert_not_called()

    def test_dangling_fence_symlinks_never_bypass_or_get_followed(self):
        for name in [reconcile.JOURNAL,'pending.json','intervention.json']:
            with self.subTest(name=name),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);(base/name).symlink_to(base/'missing')
                with patch.object(hermes,'BASE',base),patch.object(hermes,'json_file') as read, \
                     patch.object(hermes,'reconciled_marker_tombstones',return_value=False), \
                     patch.object(hermes.common,'api') as api:
                    self.assertFalse(hermes.recovery_barrier())
                    self.assertFalse(hermes.recover())
                    self.assertFalse(hermes.poll())
                read.assert_not_called();api.assert_not_called()

    def test_resume_journal_rejects_symlink_public_mode_schema_types_phases_and_invariants(self):
        mutations=[
            ('extra-key',lambda tx:tx.update(extra=True)),
            ('missing-key',lambda tx:tx.pop('external_evidence')),
            ('missing-dependency-provenance',lambda tx:tx['dependency_closure'].pop('resolved_lock_identity')),
            ('wrong-type',lambda tx:tx.update(gateway_pid='123')),
            ('wrong-phase',lambda tx:tx.update(phase='complete')),
            ('unknown-phase',lambda tx:tx.update(phase='mystery')),
            ('migration-ahead',lambda tx:tx.update(migrated_profiles=[reconcile.EXPECTED_PROFILES[0]])),
            ('alignment-ahead',lambda tx:tx.update(aligned=['launcher'])),
            ('archive-ahead',lambda tx:tx.update(archived=['pending'])),
        ]
        for name,mutate in mutations:
            with self.subTest(case=name),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);tx=self.journal_tx(base);mutate(tx);journal=base/reconcile.JOURNAL
                common.atomic(journal,tx)
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base), \
                     patch.object(reconcile,'verify_candidate') as candidate, \
                     patch.object(reconcile,'enforce_offline') as offline, \
                     patch.object(reconcile,'db_inventory') as database, \
                     patch.object(hermes,'ctl') as ctl:
                    with self.assertRaisesRegex(ValueError,'journal is missing, unsafe, or invalid'):
                        reconcile.reconcile(tx['spec'])
                candidate.assert_not_called();offline.assert_not_called();database.assert_not_called();ctl.assert_not_called()
        for case in ['public','symlink']:
            with self.subTest(case=case),tempfile.TemporaryDirectory() as tmp:
                base=Path(tmp);tx=self.journal_tx(base);journal=base/reconcile.JOURNAL
                if case=='public':
                    common.atomic(journal,tx);journal.chmod(0o644)
                else:
                    target=base/'target.json';common.atomic(target,tx);journal.symlink_to(target)
                with self.receipt_incident(tx),patch.object(hermes,'BASE',base), \
                     patch.object(reconcile,'verify_candidate') as candidate, \
                     patch.object(reconcile,'db_inventory') as database,patch.object(hermes,'ctl') as ctl:
                    with self.assertRaisesRegex(ValueError,'journal is missing, unsafe, or invalid'):
                        reconcile.reconcile(tx['spec'])
                candidate.assert_not_called();database.assert_not_called();ctl.assert_not_called()

    def test_completion_receipt_rejects_every_substituted_incident_coordinate(self):
        mutations={
            'candidate':lambda tx:tx['spec'].update(candidate_sha='a'*40),
            'run':lambda tx:tx['spec'].update(run_id=reconcile.INCIDENT_RUN_ID+1),
            'backup':lambda tx:tx['spec'].update(backup='/foreign/backup'),
            'upstream':lambda tx:tx['request'].update(upstream_sha='a'*40),
            'tag':lambda tx:tx['request'].update(tag='v9.9.9'),
            'receipt':lambda tx:tx['request'].update(receipt_sha256='a'*64),
            'workflow':lambda tx:tx['external_evidence'].update(workflow_id=1),
            'check-suite':lambda tx:tx['external_evidence'].update(check_suite_id=2),
            'control':lambda tx:tx['external_evidence'].update(control_sha='a'*40),
            'candidate-root':lambda tx:tx.update(candidate_root='/foreign/root'),
            'runtime-generation':lambda tx:tx.update(runtime_generation='/foreign/runtime'),
            'dependency-environment':lambda tx:tx['dependency_closure'].update(environment='/foreign/venv'),
            'dependency-python':lambda tx:tx['dependency_closure'].update(selected_python='/foreign/python'),
            'pending':lambda tx:tx['spec'].update(pending_sha256='a'*64),
            'archive':lambda tx:tx['sqlite_archive_identity'].update(sha256='a'*64),
        }
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);receipt=self.complete_receipt(base);path=base/reconcile.LAST
            for name,mutate in mutations.items():
                changed=json.loads(json.dumps(receipt));mutate(changed);common.atomic(path,changed)
                with self.subTest(field=name),self.receipt_incident(receipt),patch.object(hermes,'BASE',base):
                    self.assertFalse(hermes.reconciled_marker_tombstones())

    def final_tx(self,base,phase='resuming-timer'):
        tx=self.journal_tx(base,phase)
        tx['units'][reconcile.DEPLOY_TIMER]['active_state']='inactive'
        return tx

    def test_finalization_resumes_crash_after_timer_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);journal=base/reconcile.JOURNAL;tx=self.final_tx(base)
            common.atomic(journal,tx);timer={'active_state':'inactive','unit_file_state':'enabled'}
            def command(args):timer['active_state']='active';return ''
            real_persist=reconcile.persist
            def crash_persist(value,phase=None):
                if phase=='timer-started':
                    value['phase']=phase;raise OSError('crash after timer start')
                return real_persist(value,phase)
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'unit_state',side_effect=lambda unit:dict(timer)),patch.object(hermes,'command',side_effect=command),patch.object(reconcile,'verify_final') as verify,patch.object(reconcile,'verify_completion_receipt'),patch.object(reconcile,'persist',side_effect=crash_persist):
                with self.assertRaisesRegex(OSError,'after timer start'):
                    reconcile.finalize(tx,base,journal)
            resumed=json.loads(journal.read_text());self.assertEqual(resumed['phase'],'resuming-timer')
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'unit_state',return_value=dict(timer)),patch.object(hermes,'command') as start,patch.object(reconcile,'verify_final') as verify,patch.object(reconcile,'verify_completion_receipt'),patch.object(hermes,'fsync_directory'):
                self.assertTrue(reconcile.finalize(resumed,base,journal))
            start.assert_not_called()
            self.assertTrue(any(call.kwargs=={'timer_started':True,'require_fence':True}
                                for call in verify.call_args_list))

    def test_finalization_resumes_crash_after_last_publication(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);journal=base/reconcile.JOURNAL;tx=self.final_tx(base,'timer-started')
            common.atomic(journal,tx);real_atomic=common.atomic
            def publish_then_crash(path,value):
                real_atomic(path,value)
                if Path(path)==base/reconcile.LAST:raise OSError('crash after LAST')
            active={'active_state':'active','unit_file_state':'enabled'}
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'unit_state',return_value=active),patch.object(reconcile,'verify_final'),patch.object(reconcile,'verify_completion_receipt'),patch.object(common,'atomic',side_effect=publish_then_crash):
                with self.assertRaisesRegex(OSError,'after LAST'):
                    reconcile.finalize(tx,base,journal)
            resumed=json.loads(journal.read_text());self.assertEqual(resumed['phase'],'timer-started')
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'unit_state',return_value=active),patch.object(reconcile,'verify_final') as verify,patch.object(reconcile,'verify_completion_receipt'),patch.object(hermes,'fsync_directory'):
                self.assertTrue(reconcile.finalize(resumed,base,journal))
            self.assertFalse(journal.exists());self.assertEqual(json.loads((base/reconcile.LAST).read_text())['phase'],'complete')
            self.assertTrue(all(call.kwargs=={'timer_started':True,'require_fence':True}
                                for call in verify.call_args_list))

    def test_finalization_resumes_after_journal_unlink_before_directory_fsync(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);journal=base/reconcile.JOURNAL;tx=self.final_tx(base,'last-published')
            common.atomic(journal,tx);common.atomic(base/reconcile.LAST,reconcile.completion_receipt(tx))
            active={'active_state':'active','unit_file_state':'enabled'}
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'unit_state',return_value=active),patch.object(reconcile,'verify_final'),patch.object(reconcile,'verify_completion_receipt'),patch.object(hermes,'fsync_directory',side_effect=OSError('crash before fsync')):
                with self.assertRaisesRegex(OSError,'before fsync'):
                    reconcile.finalize(tx,base,journal)
            self.assertFalse(journal.exists())
            with self.receipt_incident(tx),patch.object(hermes,'BASE',base),patch.object(reconcile,'verify_candidate',return_value=(base,base/'candidate')),patch.object(reconcile,'verify_final') as verify,patch.object(reconcile,'verify_completion_receipt'),patch.object(hermes,'fsync_directory') as fsync,patch.object(reconcile,'preflight') as preflight:
                self.assertTrue(reconcile.reconcile(tx['spec']))
            preflight.assert_not_called();fsync.assert_called_once_with(base)
            verify.assert_called_once_with(ANY,base,timer_started=True,require_fence=True)

    def test_runtime_rejects_stale_state_and_missing_adapter(self):
        now=dt.datetime.now(dt.timezone.utc)
        tx={'spec':{'candidate_sha':'a'*40},'telegram_adapters':['telegram','worker:telegram'],
            'candidate_identity':{'sha256':'b'*64},'request':{'tag':'v1.2.3'}}
        base={'gateway_state':'running','code_sha':'a'*40,'pid':123,
              'updated_at':now.isoformat(),'platforms':{'telegram':{'state':'connected'},
                                                       'worker:telegram':{'state':'connected'}}}
        patches=(patch.object(hermes,'command',return_value='123'),patch.object(hermes,'prove_launcher'),
                 patch.object(hermes,'validate_gateway_unit'),patch.object(hermes,'validate_dashboard_unit'))
        with patches[0],patches[1],patches[2],patches[3],patch.object(hermes,'json_file',return_value=dict(base,updated_at=(now-dt.timedelta(minutes=10)).isoformat())):
            with self.assertRaisesRegex(ValueError,'post-restart'):reconcile.verify_runtime(tx,Path('/candidate'))
        missing=dict(base);missing['platforms']={'telegram':{'state':'connected'}}
        with patch.object(hermes,'command',return_value='123'),patch.object(hermes,'prove_launcher'),patch.object(hermes,'validate_gateway_unit'),patch.object(hermes,'validate_dashboard_unit'),patch.object(hermes,'json_file',return_value=missing):
            with self.assertRaisesRegex(ValueError,'post-restart'):reconcile.verify_runtime(tx,Path('/candidate'))

    def test_archive_resume_after_exchange_never_exchanges_twice(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);tx=self.archive_tx(base);real=hermes.rename_exchange;calls=[]
            def exchanged_then_crashed(left,right):
                calls.append(1);real(left,right);raise OSError('crash after exchange')
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange',side_effect=exchanged_then_crashed):
                with self.assertRaisesRegex(OSError,'after exchange'):reconcile.archive_marker(tx,'pending')
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange') as exchange:
                reconcile.archive_marker(tx,'pending');exchange.assert_not_called()
            destination=Path(tx['archive_plan']['pending']['destination'])
            self.assertEqual(common.digest(destination),tx['spec']['pending_sha256']);self.assertEqual(len(calls),1)

    def test_archive_resume_before_exchange_uses_journaled_holder_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);tx=self.archive_tx(base)
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange',side_effect=OSError('before exchange')):
                with self.assertRaisesRegex(OSError,'before exchange'):reconcile.archive_marker(tx,'pending')
            holder=Path(tx['archive_plan']['pending']['holder']);self.assertTrue(holder.is_file())
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange',wraps=hermes.rename_exchange) as exchange:
                reconcile.archive_marker(tx,'pending');exchange.assert_called_once()

    def test_archive_resume_after_hardlink_does_not_exchange_or_link_again(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);tx=self.archive_tx(base);real_link=os.link
            def linked_then_crashed(source,destination,**kwargs):
                real_link(source,destination,**kwargs);raise OSError('after hardlink')
            with patch.object(hermes,'BASE',base),patch.object(os,'link',side_effect=linked_then_crashed):
                with self.assertRaisesRegex(OSError,'after hardlink'):reconcile.archive_marker(tx,'pending')
            with patch.object(hermes,'BASE',base),patch.object(hermes,'rename_exchange') as exchange,patch.object(os,'link') as link:
                reconcile.archive_marker(tx,'pending');exchange.assert_not_called();link.assert_not_called()

    def test_archived_marker_and_archive_symlinks_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);archive=base/reconcile.ARCHIVE;archive.mkdir()
            original=b'original';digest=hashlib.sha256(original).hexdigest()
            last={'spec':{'pending_sha256':digest,'candidate_sha':'a'*40,'run_id':42}}
            target=base/'target';target.write_text(json.dumps(reconcile.tombstone('pending',digest,'a'*40,42)))
            (base/'pending.json').symlink_to(target)
            archived=base/'archived';archived.write_bytes(original);(archive/f'pending-{digest}.json').symlink_to(archived)
            with patch.object(hermes,'BASE',base):self.assertFalse(reconcile.marker_is_archived(base/'pending.json',last))

    def test_archived_marker_requires_private_stable_entries_and_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);last=self.complete_receipt(base);marker=base/'pending.json'
            archive=base/reconcile.ARCHIVE;archived=next(archive.glob('pending-*.json'))
            with patch.object(hermes,'BASE',base):self.assertTrue(reconcile.marker_is_archived(marker,last))
            for target in [marker,archived]:
                target.chmod(0o644)
                with patch.object(hermes,'BASE',base):self.assertFalse(reconcile.marker_is_archived(marker,last))
                target.chmod(0o600)
            archive.chmod(0o755)
            with patch.object(hermes,'BASE',base):self.assertFalse(reconcile.marker_is_archived(marker,last))
            archive.chmod(0o700)
            real=reconcile.marker_digest;calls=0
            def mutate_after_first_read(path):
                nonlocal calls
                result=real(path);calls+=1
                if calls==1:
                    marker.write_text(marker.read_text()+' ');marker.chmod(0o600)
                return result
            with patch.object(hermes,'BASE',base),patch.object(reconcile,'marker_digest',side_effect=mutate_after_first_read):
                self.assertFalse(reconcile.marker_is_archived(marker,last))

    def test_bootstrap_packages_reconciliation_controller(self):
        import install_executor
        source=Path(__file__).resolve().parents[2]/'scripts/maintenance'
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);code=install_executor.stage_control(base,source,'a'*40)
            self.assertEqual((code/'hermes_reconcile.py').read_bytes(),(source/'hermes_reconcile.py').read_bytes())


if __name__=='__main__':unittest.main()
