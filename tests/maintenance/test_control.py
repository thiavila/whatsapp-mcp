import copy
import datetime as dt
import importlib.util
from pathlib import Path
import unittest

path = Path(__file__).resolve().parents[2] / 'scripts/maintenance/control.py'
spec = importlib.util.spec_from_file_location('control', path)
c = importlib.util.module_from_spec(spec); spec.loader.exec_module(c)
SHA = 'a' * 40


class ControlTests(unittest.TestCase):
    def test_run_provenance_rejects_pr_other_repo_branch_and_stale_sha(self):
        run = dict(repository={'full_name': c.REPOSITORY}, head_repository={'full_name':c.REPOSITORY},
                   workflow_id=7, path=c.WORKFLOW, head_branch='main', head_sha=SHA,
                   event='push', status='completed', conclusion='success')
        c.admit_run(run, 7, SHA)
        for key, value in [('repository', {'full_name':'attacker/fork'}),
                           ('head_repository', {'full_name':'attacker/fork'}),
                           ('workflow_id', 8), ('path', '.github/workflows/test.yml'),
                           ('head_branch', 'pr'), ('head_sha', 'b'*40),
                           ('event', 'pull_request'), ('status', 'queued'), ('conclusion', 'failure')]:
            changed = copy.deepcopy(run); changed[key] = value
            with self.subTest(key=key), self.assertRaises(ValueError): c.admit_run(changed, 7, SHA)

    def test_new_failure_does_not_fall_back_to_old_success(self):
        checks = [dict(id=1,name='MCP CI result',app={'slug':'github-actions'},status='completed',conclusion='success')]
        self.assertTrue(c.successful_checks(checks))
        checks.append(dict(checks[0], id=2, conclusion='failure'))
        self.assertFalse(c.successful_checks(checks))

    def test_pr_must_be_merged_into_our_main_at_exact_commit(self):
        pr = dict(merged_at='date', merge_commit_sha=SHA, base={'ref':'main','repo':{'full_name':c.REPOSITORY}})
        self.assertEqual(c.admitted_prs([pr], SHA), [pr])
        self.assertFalse(c.admitted_prs([dict(pr, merged_at=None)], SHA))
        self.assertFalse(c.admitted_prs([pr], 'b'*40))

    def test_idle_requires_fresh_explicit_zero_without_active_work(self):
        now = dt.datetime.now(dt.timezone.utc)
        state = dict(updated_at=now.isoformat(), active_agents=0, active_work=None,gateway_state='running')
        self.assertTrue(c.gateway_idle(state, now.timestamp()))
        for change in [dict(active_agents=1), dict(active_agents=False), dict(active_agents=None),
                       dict(active_work=['turn']), dict(updated_at='bad'),
                       dict(updated_at=(now-dt.timedelta(minutes=5)).isoformat())]:
            self.assertFalse(c.gateway_idle(dict(state, **change),now.timestamp()))


if __name__ == '__main__': unittest.main()
