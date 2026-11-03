"""Pure admission rules shared by release CI and the VM executor."""
import datetime as dt
import re

REPOSITORY = 'thiavila/whatsapp-mcp'
WORKFLOW = '.github/workflows/release.yml'


def require_sha(value):
    if not isinstance(value, str) or not re.fullmatch('[0-9a-f]{40}', value):
        raise ValueError('invalid commit')
    return value


def admit_run(run, workflow_id, expected_sha):
    require_sha(expected_sha)
    if (run.get('repository', {}).get('full_name') != REPOSITORY
            or run.get('head_repository', {}).get('full_name') != REPOSITORY
            or run.get('workflow_id') != workflow_id or run.get('path') != WORKFLOW
            or run.get('head_branch') != 'main' or run.get('head_sha') != expected_sha
            or run.get('event') not in ['push', 'workflow_dispatch']
            or run.get('status') != 'completed' or run.get('conclusion') != 'success'):
        raise ValueError('release run provenance rejected')


def admitted_prs(prs, sha):
    return [p for p in prs if p.get('merged_at') and p.get('merge_commit_sha') == sha
            and p.get('base', {}).get('ref') == 'main'
            and p.get('base', {}).get('repo', {}).get('full_name') == REPOSITORY]


def successful_checks(checks):
    # Latest named check wins; a rerun failure must not fall back to old success.
    matching = [c for c in checks if c.get('name') == 'MCP CI result'
                and c.get('app', {}).get('slug') == 'github-actions']
    latest = max(matching, key=lambda c: c.get('id', 0), default={})
    return latest.get('conclusion') == 'success' and latest.get('status') == 'completed'


def gateway_idle(state, now):
    try:
        stamp = dt.datetime.fromisoformat(state['updated_at'].replace('Z', '+00:00'))
        age = now - stamp.timestamp()
    except (KeyError, TypeError, ValueError):
        return False
    return (0 <= age < 180 and type(state.get('active_agents')) is int
            and state['active_agents'] == 0 and not state.get('active_work')
            and state.get('gateway_state') == 'running')
