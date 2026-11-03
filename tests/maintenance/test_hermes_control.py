import datetime as dt
from pathlib import Path
import sys
import unittest
from unittest.mock import patch
import hermes_executor

sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'scripts/maintenance'))
from hermes_control import stable_release,candidate_root,validate_request


class HermesControlTests(unittest.TestCase):
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


if __name__=='__main__':unittest.main()
