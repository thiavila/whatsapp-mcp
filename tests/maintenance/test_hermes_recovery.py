from contextlib import closing
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/maintenance'))
import hermes_executor as h
import executor as e
import hermes_watchdog as w


def database(path):
    path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path)) as db,db:
        db.execute('CREATE TABLE messages(id INTEGER PRIMARY KEY,body TEXT)')
        db.execute("INSERT INTO messages VALUES(1,'before update')")


class RecoveryTests(unittest.TestCase):
    def transaction(self,base,db):
        tx=dict(candidate_sha='a'*40,previous_sha='b'*40,run_id=123,
                schemas={str(db):e.db_schema(db)},units={},backup=str(base/'backup'))
        e.atomic(base/'pending.json',tx)
        return tx

    def test_incident_migration_and_new_messages_survive_repeated_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'home';db=home/'state.db';database(db)
            tx=self.transaction(base,db)
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute('ALTER TABLE messages ADD COLUMN migrated INTEGER')
                connection.execute("INSERT INTO messages VALUES(2,'received on new version',1)")
            with patch.object(h,'BASE',base),patch.object(h,'HOME',home),patch.object(h,'ctl') as ctl,patch.object(h.common,'notify'):
                self.assertFalse(h.recover())
                self.assertFalse(h.recover())
                with patch.object(h.common,'api') as api:
                    self.assertFalse(h.poll());api.assert_not_called()
                ctl.assert_not_called()
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT body FROM messages ORDER BY id').fetchall(),
                                 [('before update',),('received on new version',)])
            self.assertEqual(json.loads((base/'intervention.json').read_text())['reason'],'schema-incompatible')
            self.assertEqual(json.loads((base/'pending.json').read_text()),tx)

    def test_notification_failure_does_not_rearm_service_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);self.transaction(base,db)
            with patch.object(h,'BASE',base),patch.object(h,'live_schemas',return_value={}),patch.object(h,'ctl') as ctl,patch.object(h.common,'notify',side_effect=OSError('offline')):
                self.assertFalse(h.recover());self.assertFalse(h.recover());ctl.assert_not_called()
            self.assertTrue((base/'intervention.json').exists())

    def test_corrupt_or_unreadable_schema_never_stops_running_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);self.transaction(base,db)
            for error in [ValueError('integrity failed'),sqlite3.DatabaseError('locked'),OSError('unreadable')]:
                (base/'intervention.json').unlink(missing_ok=True)
                with patch.object(h,'BASE',base),patch.object(h,'live_schemas',side_effect=error),patch.object(h,'ctl') as ctl,patch.object(h.common,'notify'):
                    self.assertFalse(h.recover());ctl.assert_not_called()

    def test_schema_changes_during_stop_never_restore_old_code(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);tx=self.transaction(base,db)
            with patch.object(h,'BASE',base),patch.object(h,'live_schemas',side_effect=[tx['schemas'],{}]),patch.object(h,'ctl') as ctl,patch.object(h.shutil,'copyfile') as copy,patch.object(h.common,'notify'):
                self.assertFalse(h.recover());copy.assert_not_called()
                calls=ctl.call_count
                self.assertFalse(h.recover());self.assertEqual(ctl.call_count,calls)

    def test_compatible_rollback_preserves_live_rows(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'home';db=home/'state.db';database(db)
            tx=self.transaction(base,db);backup=base/'backup';backup.mkdir();units=base/'units';units.mkdir()
            (backup/'hermes-shim').write_text('previous launcher')
            for name in ['hermes-gateway.service','hermes-dashboard.service']:
                (backup/name).write_text('previous unit')
            with closing(sqlite3.connect(db)) as connection,connection:
                connection.execute("INSERT INTO messages VALUES(2,'new row, unchanged schema')")
            with patch.object(h,'BASE',base),patch.object(h,'HOME',home),patch.object(h,'UNITS',units),patch.object(h,'SHIM',base/'shim'),patch.object(h,'ctl'),patch.object(h,'command'),patch.object(h,'resume'),patch.object(h,'healthy'),patch.object(h.common,'notify'):
                self.assertTrue(h.recover())
            self.assertFalse((base/'pending.json').exists())
            self.assertEqual((base/'shim').read_text(),'previous launcher')
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT count(*) FROM messages').fetchone()[0],2)

    def test_failed_compatible_rollback_never_repeats_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);tx=self.transaction(base,db)
            backup=base/'backup';backup.mkdir();(backup/'hermes-shim').write_text('previous')
            with patch.object(h,'BASE',base),patch.object(h,'SHIM',base/'shim'),patch.object(h,'live_schemas',return_value=tx['schemas']),patch.object(h,'installation',return_value={}),patch.object(h,'ctl') as ctl,patch.object(h,'command'),patch.object(h,'resume'),patch.object(h,'healthy',side_effect=ValueError('Telegram timeout')),patch.object(h.common,'notify'):
                self.assertFalse(h.recover());stops=ctl.call_count
                self.assertGreater(stops,0)
                self.assertFalse(h.recover());self.assertEqual(ctl.call_count,stops)
                self.assertEqual(json.loads((base/'intervention.json').read_text())['reason'],'rollback-unhealthy')

    def test_crash_after_rollback_armed_does_not_repeat_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);tx=self.transaction(base,db)
            tx['rollback_attempted']=True;e.atomic(base/'pending.json',tx)
            with patch.object(h,'BASE',base),patch.object(h,'ctl') as ctl,patch.object(h.common,'notify'):
                self.assertFalse(h.recover());ctl.assert_not_called()

    def test_healthy_receipt_bound_candidate_finishes_without_rollback(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);tx=self.transaction(base,db)
            with patch.object(h,'BASE',base),patch.object(h,'live_schemas',return_value=tx['schemas']),patch.object(h,'candidate_healthy',return_value=True),patch.object(h,'ctl') as ctl:
                self.assertTrue(h.recover());ctl.assert_not_called()
            self.assertFalse((base/'pending.json').exists())
            self.assertEqual(json.loads((base/'last-success.json').read_text())['candidate_sha'],tx['candidate_sha'])

    def test_changed_schema_after_stop_resumes_only_bound_unchanged_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);tx=self.transaction(base,db)
            tx.update(installed={'fixed launcher':'digest'},request={'candidate_sha':'a'*40})
            e.atomic(base/'pending.json',tx)
            with patch.object(h,'BASE',base),patch.object(h,'live_schemas',side_effect=[tx['schemas'],{}]),patch.object(h,'candidate_healthy',return_value=False),patch.object(h,'installation',return_value=tx['installed']),patch.object(h,'validate_local') as validate,patch.object(h,'ctl'),patch.object(h,'resume') as resume,patch.object(h,'healthy') as healthy,patch.object(h.shutil,'copyfile') as copy,patch.object(h.common,'notify'):
                self.assertFalse(h.recover());copy.assert_not_called()
                validate.assert_called_once_with(tx['request'])
                resume.assert_called_once_with(tx['units']);healthy.assert_called_once_with(tx['units'],tx['candidate_sha'])

    def test_barrier_precedes_auth_and_bridge_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);db=base/'state.db';database(db);self.transaction(base,db)
            import indexa_auth_executor as auth
            with patch.object(e,'BASE',base),patch.object(h,'BASE',base),patch.object(h,'live_schemas',return_value={}),patch.object(h.common,'notify'),patch.object(auth,'poll_safely') as auth_poll,patch.object(e,'recover') as bridge_recover:
                e.poll();auth_poll.assert_not_called();bridge_recover.assert_not_called()

    def test_concurrent_journals_require_intervention_before_any_lane(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);hermes=base/'hermes';hermes.mkdir();mcp=base/'mcp';mcp.mkdir()
            db=hermes/'state.db';database(db);self.transaction(hermes,db)
            e.atomic(mcp/'indexa-auth-pending.json',{'phase':'secure-hold'})
            with patch.object(h,'BASE',hermes),patch.object(e,'BASE',mcp),patch.object(h,'recover') as recover,patch.object(e,'notify'):
                self.assertFalse(h.recovery_barrier());recover.assert_not_called()

    def test_installer_preserves_paused_timer_and_starts_independent_monitor(self):
        import install_executor as installer
        with patch.object(installer.subprocess,'run') as run:
            installer.activate_timers(False)
            commands=[c.args[0] for c in run.call_args_list]
            self.assertIn(['systemctl','--user','enable','mcp-maintenance-deploy.timer'],commands)
            self.assertIn(['systemctl','--user','enable','--now','hermes-health-watch.timer'],commands)
        with patch.object(installer.subprocess,'run') as run:
            installer.activate_timers(True)
            self.assertIn(['systemctl','--user','enable','--now','mcp-maintenance-deploy.timer'],[c.args[0] for c in run.call_args_list])

    def test_orphan_intervention_also_fences_all_deploy_lanes(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);e.atomic(base/'intervention.json',{'reason':'operator review'})
            with patch.object(h,'BASE',base),patch.object(h,'recover') as recover:
                self.assertFalse(h.poll());recover.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            import indexa_auth_executor as auth
            import client_executor as client
            with patch.object(e,'BASE',Path(tmp)),patch.object(auth,'poll_safely',return_value=True),patch.object(e,'recover'),patch.object(h,'poll',return_value=False),patch.object(client,'poll_safely') as next_lane,patch.object(e,'api') as api:
                e.poll();next_lane.assert_not_called();api.assert_not_called()

    def test_canary_catches_cron_migration_before_any_service_action(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'live';copies=base/'copies'
            for path in [home/'state.db',home/'cron/executions.db',home/'shared-state.db']:
                database(path)
            with patch.object(h,'HOME',home):
                schemas=h.snapshot(copies)
                def migrate(args,*a,**kw):
                    copied_home=Path(next(x.split('=',1)[1] for x in args if x.startswith('HERMES_HOME=')))
                    self.assertTrue(copied_home.is_relative_to(copies))
                    self.assertIn('CANARY_SHARED_DB='+str(copies/'shared-state.db'),args)
                    with closing(sqlite3.connect(copied_home/'cron/executions.db')) as db,db:
                        db.execute('ALTER TABLE messages ADD COLUMN new_ledger_field INTEGER')
                with patch.object(h,'command',side_effect=migrate),patch.object(h,'ctl') as ctl,self.assertRaisesRegex(ValueError,'schema migration'):
                    h.canary(base,copies,schemas,base/'canary.log')
                ctl.assert_not_called()
                self.assertEqual(h.live_schemas(),schemas)

    def test_full_apply_reproduces_healthy_start_then_unexpected_migration(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);home=base/'home';db=home/'state.db';database(db)
            (home/'gateway_state.json').write_text(json.dumps({'code_sha':'b'*40,'code_version':'0.22.5'}))
            units=base/'units';units.mkdir();shim=base/'shim';shim.write_text('launcher')
            def resume(states):
                with closing(sqlite3.connect(db)) as connection,connection:
                    connection.execute('ALTER TABLE messages ADD COLUMN migrated INTEGER')
                    connection.execute("INSERT INTO messages VALUES(2,'arrived after startup',1)")
            with patch.object(h,'BASE',base),patch.object(h,'HOME',home),patch.object(h,'UNITS',units),patch.object(h,'SHIM',shim),patch.object(h,'validate_local',return_value=base),patch.object(h,'canary'),patch.object(h,'state_files',return_value=[]),patch.object(h.common,'gateway_quiet',return_value=True),patch.object(h.common,'busy_service',return_value=False),patch.object(h.common,'notify'),patch.object(h,'active',return_value=True),patch.object(h,'ctl') as ctl,patch.object(h,'command',return_value='candidate-branch'),patch.object(h,'resume',side_effect=resume),patch.object(h,'healthy'):
                request=dict(action='promote',candidate_sha='a'*40,tag='v0.22.6')
                with self.assertRaisesRegex(ValueError,'live schema differs'):
                    h.apply(request,123)
                initial_stops=ctl.call_count
                self.assertFalse(h.recover());self.assertEqual(ctl.call_count,initial_stops)
            self.assertTrue((base/'intervention.json').exists())
            with closing(sqlite3.connect(db)) as connection:
                self.assertEqual(connection.execute('SELECT body FROM messages WHERE id=2').fetchone()[0],'arrived after startup')


class WatchdogTests(unittest.TestCase):
    def test_corrupt_incident_is_rebuilt_and_failed_delivery_is_visible(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);path=base/'hermes-health-incident.json';path.write_text('{bad json')
            with patch.object(e,'BASE',base),patch.object(w,'maintenance_active',return_value=False),patch.object(w,'problem',return_value='gateway-inactive'),patch.object(e,'notify',side_effect=OSError('offline')):
                w.check(100)
                with self.assertRaises(OSError):w.check(401)
                self.assertTrue(json.loads(path.read_text())['delivery_failed'])

    def test_maintenance_suppression_is_bounded_and_disabled_by_intervention(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp);hermes=base/'hermes-deploy';hermes.mkdir()
            e.atomic(hermes/'pending.json',dict(phase='installing',started_at=10,maintenance_deadline=100))
            with patch.object(e,'BASE',base/'mcp-deploy'):
                self.assertTrue(w.maintenance_active(50));self.assertFalse(w.maintenance_active(101))
                e.atomic(hermes/'intervention.json',{})
                self.assertFalse(w.maintenance_active(50))

    def test_independent_monitor_alerts_after_grace_without_service_mutations(self):
        with tempfile.TemporaryDirectory() as tmp:
            base=Path(tmp)
            with patch.object(e,'BASE',base),patch.object(w,'problem',return_value='gateway-inactive'),patch.object(e,'notify') as notify:
                w.check(0);w.check(299);notify.assert_not_called()
                w.check(301);key=notify.call_args.args[0]
                w.check(500);self.assertEqual(notify.call_args.args[0],key)
                with patch.object(w,'problem',return_value=None):w.check(501)
                self.assertFalse((base/'hermes-health-incident.json').exists())
                w.check(600);w.check(901);self.assertNotEqual(notify.call_args.args[0],key)

    def test_unpaired_telegram_profile_is_detected(self):
        with tempfile.TemporaryDirectory() as tmp:
            home=Path(tmp)
            state=dict(pid=123,gateway_state='running',updated_at='2026-11-09T13:00:00+00:00',
                       platforms={'telegram':{'state':'connected'},'profile:telegram':{'state':'disconnected'}})
            (home/'gateway_state.json').write_text(json.dumps(state))
            with patch.object(e,'HERMES',home),patch.object(e,'run',return_value='MainPID=123\nActiveState=active'):
                self.assertEqual(w.problem(1794229200),'telegram-disconnected')


if __name__=='__main__':unittest.main()
