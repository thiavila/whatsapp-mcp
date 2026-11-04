"""Build the Linux x64 client from pinned upstream archives + hash-locked wheels.

Run only in isolated CI/local build directories. Never installs on the VM.
"""
import argparse
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'scripts/maintenance'))
from client_archive import digest, extract


def build(output, sha):
    if platform.system() != 'Linux' or platform.machine() not in ['x86_64', 'AMD64']:
        raise ValueError('Linux x64 build host required')
    sys.path.insert(0, str(ROOT / 'scripts/maintenance'))
    from control import require_sha
    require_sha(sha)
    output.mkdir(parents=True, exist_ok=True)
    if list(output.iterdir()):
        raise ValueError('build output must be empty')
    pins = json.loads((ROOT / 'scripts/client/pins.json').read_text())
    with tempfile.TemporaryDirectory(prefix='mcp-client-build-') as temporary:
        work = Path(temporary)
        home = work / 'home'; home.mkdir()
        env = {'PATH': '/usr/bin:/bin', 'HOME': str(home), 'LANG': 'C.UTF-8',
               'UV_PYTHON_DOWNLOADS': 'never', 'UV_CACHE_DIR': str(work / 'cache'),
               'UV_NO_CONFIG': 'true', 'PYTHONDONTWRITEBYTECODE': '1'}
        bundle = work / 'bundle'; bundle.mkdir()
        for name in ['python', 'uv']:
            target = work / (name + '.tar.gz')
            subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location',
                            '--proto', '=https', '--tlsv1.2', '--max-time', '120',
                            '--max-filesize', str(160 * 1024**2),
                            '--output', str(target), pins[name]['url']], env=env, check=True)
            if digest(target) != pins[name]['sha256']:
                raise ValueError('upstream archive digest mismatch')
            extract(target, work / name)
        shutil.move(str(work / 'python/python'), bundle / 'python')
        shutil.copy2(work / 'uv/uv-x86_64-unknown-linux-gnu/uv', bundle / 'uv')
        uv = str(bundle / 'uv'); python = str(bundle / 'python/bin/python3')
        server = ROOT / 'whatsapp-mcp-server'
        requirements = work / 'requirements.txt'
        subprocess.run([uv, 'export', '--project', str(server), '--frozen', '--no-dev',
                        '--group', 'bootstrap', '--no-emit-project', '--no-header',
                        '--output-file', str(requirements)], env=env, check=True)
        subprocess.run([uv, 'pip', 'install', '--python', python, '--system',
                        '--break-system-packages', '--require-hashes',
                        '--only-binary', ':all:', '-r', str(requirements)],
                       env=env, check=True, timeout=180)
        app = bundle / 'app'; app.mkdir()
        for path in server.glob('*.py'):
            shutil.copy2(path, app / path.name)
        for name in ['launch.py', 'canary.py']:
            shutil.copy2(ROOT / 'scripts/client' / name, bundle / name)
        # Config adapter runs only in the executor, never imports runtime auth.
        shutil.copy2(ROOT / 'scripts/client/config.py', bundle / 'config.py')
        (bundle / 'run-client').write_text(
            '#!/bin/sh\nset -eu\nroot=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)\n'
            'exec "$root/python/bin/python3" -I -B "$root/launch.py" "$@" 2>/dev/null\n')
        (bundle / 'run-client').chmod(0o700)
        runtime = {'schema': 1, 'repository': 'thiavila/whatsapp-mcp', 'sha': sha,
                   'versions': {k: pins[k]['version'] if k in ['python', 'uv'] else pins[k]
                                for k in ['python', 'uv', 'certifi', 'pip']},
                   'lock_sha256': digest(server / 'uv.lock'),
                   'source_sha256': {p.name: digest(p) for p in app.glob('*.py')}}
        (bundle / 'runtime.json').write_text(json.dumps(runtime, sort_keys=True) + '\n')
        subprocess.run([python, '-I', '-B', str(bundle / 'canary.py'), str(bundle)],
                       env=env, check=True, timeout=75)
        archive = output / 'client.tar.gz'
        with tarfile.open(archive, 'w:gz', compresslevel=6) as tar:
            for path in sorted(bundle.iterdir()):
                tar.add(path, arcname=path.name)
        # Verify the published/relocated bytes, not just the build directory.
        extract(archive, work / 'relocated')
        relocated = work / 'relocated'
        subprocess.run([str(relocated / 'python/bin/python3'), '-I', '-B',
                        str(relocated / 'canary.py'), str(relocated)],
                       env=env, check=True, timeout=75)
        (output / 'manifest.json').write_text(json.dumps(
            dict(runtime, client_sha256=digest(archive)), sort_keys=True) + '\n')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--sha', required=True)
    args = parser.parse_args()
    build(args.output, args.sha)
