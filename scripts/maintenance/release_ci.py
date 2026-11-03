"""Require a merged PR and green official CI before producing a deploy artifact."""
import json
import subprocess
import sys
from control import REPOSITORY, require_sha, admitted_prs, successful_checks


def api(path):
    return json.loads(subprocess.check_output(['gh', 'api', path], timeout=30))


def verify(sha):
    require_sha(sha)
    prs = admitted_prs(api(f'repos/{REPOSITORY}/commits/{sha}/pulls'), sha)
    if not prs:
        raise ValueError('deployment requires a merged main PR')
    for pr in prs:
        head = require_sha(pr['head']['sha'])
        checks = api(f'repos/{REPOSITORY}/commits/{head}/check-runs?per_page=100')['check_runs']
        if successful_checks(checks):
            return
    raise ValueError('official MCP CI is not green on merged PR head')


if __name__ == '__main__':
    verify(sys.argv[1])
    print('Merged PR and official code CI verified')
