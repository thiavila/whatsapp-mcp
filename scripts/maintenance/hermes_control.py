"""Admission rules for the official Hermes installer, executed outside gateway."""
import datetime as dt
from pathlib import Path
import re
from control import require_sha


def stable_release(release, tag, upstream_sha, now):
    require_sha(upstream_sha)
    if (not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+', tag)
            or release.get('tag_name')!=tag or release.get('draft') is not False
            or release.get('prerelease') is not False):
        raise ValueError('official stable release required')
    published=dt.datetime.fromisoformat(release['published_at'].replace('Z','+00:00'))
    if published.tzinfo is None or (now-published).total_seconds()<72*3600:
        raise ValueError('release must be at least 72 hours old')


def candidate_root(path):
    root=Path(path).resolve()
    if root.parent!=Path('/home/thiago/worktrees').resolve() or not re.fullmatch(r'hermes-[A-Za-z0-9_-]+',root.name):
        raise ValueError('candidate must be a dedicated Hermes worktree')
    return root


def validate_request(request):
    if set(request)!= {'schema','component','candidate_sha','upstream_sha','tag','receipt_sha256','action'}:
        raise ValueError('Hermes request schema')
    if request['schema']!=1 or request['component']!='hermes' or request['action'] not in ['check','promote']:
        raise ValueError('Hermes request scope')
    require_sha(request['candidate_sha']);require_sha(request['upstream_sha'])
    if not re.fullmatch('[0-9a-f]{64}',request['receipt_sha256']):raise ValueError('receipt hash')
    if not re.fullmatch(r'v[0-9]+\.[0-9]+\.[0-9]+',request['tag']):raise ValueError('tag')
