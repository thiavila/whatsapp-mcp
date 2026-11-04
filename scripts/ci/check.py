"""Bounded code checks. No production data, service, Docker or LLM is used."""
import os
from pathlib import Path
import sys
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]


def main():
    cache = Path(os.environ.get('MCP_CI_CACHE', Path.home() / '.cache/whatsapp-mcp-ci'))
    cache.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='mcp-code-ci-') as temporary:
        work = Path(temporary)
        (work / 'home').mkdir()
        (work / 'store').mkdir()
        env = {'PATH': os.environ['PATH'], 'HOME': str(work / 'home'), 'LANG': 'C.UTF-8',
               'TMPDIR': str(work), 'GOCACHE': str(cache / 'go-build'),
               'GOMODCACHE': str(cache / 'go-mod'), 'GOPATH': str(cache / 'go'),
               'GOMAXPROCS': '2', 'GOFLAGS': '-p=2 -mod=readonly',
               'UV_CACHE_DIR': str(cache / 'uv'), 'UV_PROJECT_ENVIRONMENT': str(work / 'venv'),
               'UV_PYTHON_DOWNLOADS': 'never', 'PYTHONPYCACHEPREFIX': str(work / 'pycache'),
               'WHATSAPP_STORE_DIR': str(work / 'store'),
               'WHATSAPP_MESSAGES_DB_PATH': str(work / 'store/messages.db'),
               'WHATSAPP_DB_PATH': str(work / 'store/whatsapp.db'),
               'WHATSAPP_API_BASE_URL': 'http://127.0.0.1:1/api'}
        def run(args, directory=ROOT, timeout=120):
            print('+', ' '.join(args), flush=True)
            return subprocess.run(args, cwd=directory, env=env, stdin=subprocess.DEVNULL,
                                  timeout=timeout, check=True)
        bridge = ROOT / 'whatsapp-bridge'
        files = sorted(str(p) for p in bridge.glob('*.go'))
        formatted = subprocess.check_output(['gofmt', '-l', *files], env=env, text=True)
        if formatted.strip():
            raise SystemExit('Run gofmt on: ' + formatted)
        # go test includes compilation/vet: no separate duplicate go build/vet.
        run(['go', 'test', '-timeout=90s', '-count=1', '-parallel=2', './...'], bridge, 240)
        server = ROOT / 'whatsapp-mcp-server'
        run(['uv', 'sync', '--frozen', '--no-dev', '--group', 'bootstrap', '--python', sys.executable], server)
        python = str(work / 'venv/bin/python')
        run([python, '-m', 'compileall', '-q', *[str(p) for p in server.glob('*.py')]])
        run([python, '-m', 'unittest', 'discover', '-s', str(server / 'tests')], server)
        # Deployment control is stdlib-only; synthetic transition tests.
        if (ROOT / 'tests/maintenance').exists():
            run([python, '-m', 'unittest', 'discover', '-s', 'tests/maintenance'])
        run([python, '-m', 'unittest', 'discover', '-s', 'tests/client'])
    print('Code CI passed; no service started.', flush=True)


if __name__ == '__main__':
    main()
