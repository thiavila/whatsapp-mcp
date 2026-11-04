"""Produce a small config artifact; never reads env/auth files or credentials."""
import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/maintenance'))
import indexa_auth_common as a
from control import require_sha
from indexa_auth_executor import module_hashes


def build(sha, output):
    require_sha(sha)
    config = json.loads((ROOT / 'ops/indexa-auth.json').read_text())
    if a.canonical(config) != a.canonical(a.CONFIG):
        raise ValueError('only the reviewed Indexa config contract is allowed')
    manifest = {'schema': 1, 'repository': a.REPOSITORY, 'sha': sha,
                'config_sha256': a.fingerprint(config), 'control_sha256': module_hashes()}
    output.mkdir(parents=True, exist_ok=True)
    for name, value in [('auth.json', config), ('manifest.json', manifest)]:
        (output / name).write_bytes(a.canonical(value) + b'\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser(); parser.add_argument('--sha', required=True)
    parser.add_argument('--output', type=Path, required=True); args = parser.parse_args()
    build(args.sha, args.output)
