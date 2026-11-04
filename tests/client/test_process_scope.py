"""Regression: unrelated non-dumpable user processes must not block MCP stage."""
from pathlib import Path
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'scripts/maintenance'))
import client_executor as ce

REAL_PATH = Path


class Entry:
    def __init__(self, process, name):
        self.process, self.name = process, name

    def read_bytes(self):
        if self.process.hidden_cmdline:
            raise PermissionError
        return self.process.args

    def read_text(self):
        self.process.cgroup_reads += 1
        return self.process.group


class Process:
    def __init__(self, pid, args, exe='/protected/agent', denied=False, uid=1000,
                 group='0::/hermes-gateway.service', hidden_cmdline=False):
        self.name = str(pid)
        self.args, self.exe, self.denied, self.uid = args, exe, denied, uid
        self.group, self.hidden_cmdline = group, hidden_cmdline
        self.cgroup_reads = self.stat_reads = 0

    def __truediv__(self, name):
        return Entry(self, name)

    def stat(self):
        self.stat_reads += 1
        return SimpleNamespace(st_uid=self.uid)


class ProcessScopeTests(unittest.TestCase):
    def inspect(self, processes):
        root = SimpleNamespace(iterdir=lambda: iter(processes))
        def path(value):
            return root if str(value) == '/proc' else REAL_PATH(value)
        def readlink(entry):
            if entry.process.denied:
                raise PermissionError
            return entry.process.exe
        with patch.object(ce, 'Path', side_effect=path), patch.object(ce.os, 'getuid', return_value=1000), patch.object(ce.os, 'readlink', side_effect=readlink) as links:
            records = ce.processes()
        return records, links

    def test_readable_irrelevant_argv_never_inspects_protected_exe_or_rest(self):
        # Same permission shape as sd-pam/sshd/gpg-agent, without a name allowlist.
        nodes = [Process(pid, b'arbitrary-unrelated-application\0', denied=True)
                 for pid in [5366, 3067574, 3810387]]
        records, links = self.inspect(nodes)
        self.assertEqual(records, []); links.assert_not_called()
        self.assertTrue(all(p.cgroup_reads == p.stat_reads == 0 for p in nodes))

    def test_relevant_inaccessible_exe_is_fail_closed_for_any_owner(self):
        for uid in [1000, 1001]:
            with self.subTest(uid=uid):
                node = Process(11, ce.LEGACY.encode() + b'/main.py\0', denied=True, uid=uid)
                with self.assertRaisesRegex(ValueError, 'cannot inspect relevant'):
                    self.inspect([node])

    def test_unknown_same_user_cmdline_permission_remains_fail_closed(self):
        with self.assertRaisesRegex(ValueError, 'cannot inspect same-user'):
            self.inspect([Process(12, b'', hidden_cmdline=True)])

    def test_relevant_deleted_python_and_uv_are_retained_and_cannot_pass_mapped(self):
        for exe in ['/old/python3.11 (deleted)', '/old/uv (deleted)']:
            with self.subTest(exe=exe):
                node = Process(13, ce.LEGACY.encode() + b'/main.py\0', exe=exe)
                records, _ = self.inspect([node])
                self.assertEqual(records, [(13, exe)])
                self.assertFalse(ce.mapped(REAL_PATH('/candidate/runtime'), records))

    def test_relevant_outside_managed_cgroup_remains_blocked(self):
        node = Process(14, ce.LEGACY.encode() + b'/main.py\0', exe='/current/python3.11', group='0::/ssh.session')
        with self.assertRaisesRegex(ValueError, 'unmanaged WhatsApp'):
            self.inspect([node])

    def test_relevant_accessible_current_process_still_included(self):
        node = Process(15, b'/home/thiago/hermes-maintenance/mcp-deploy/client-releases/' + b'a' * 40 + b'/runtime/app/main.py\0', exe='/current/python3.11')
        records, links = self.inspect([node])
        self.assertEqual(records, [(15, '/current/python3.11')]); links.assert_called_once()
        self.assertEqual(node.cgroup_reads, 1)


if __name__ == '__main__':
    unittest.main()
