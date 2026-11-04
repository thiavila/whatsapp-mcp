"""Immutable stdio launcher: packaged uv/Python only; no resolver or auth changes."""
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parent
LEGACY = Path('/home/thiago/repos/whatsapp-mcp/whatsapp-mcp-server')


def command(root=ROOT, canary=False):
    python = str(root / 'python/bin/python3')
    code = ('import runpy,sys,os;' +
            ('assert os.environ.get("MCP_CLIENT_POISON") is None;'
             'assert os.environ.get("MCP_CLIENT_OPAQUE_ENV_PROBE")==os.environ.get("MCP_CLIENT_EXPECT_ENV");'
             if canary else '') +
            'os.environ["WHATSAPP_BRIDGE_DIR"]=' + repr(str(LEGACY.parent / 'whatsapp-bridge')) + ';'
            'sys.path.insert(0,' + repr(str(root / 'app')) + ');'
            'runpy.run_path(' + repr(str(root / 'app/main.py')) + ',run_name="__main__")')
    return [str(root / 'uv'), '--no-config', '--offline', '--no-cache', 'run',
            '--no-project', '--no-sync', '--frozen',
            '--no-python-downloads', '--python', python, '--', python, '-I', '-B', '-c', code]


def environment(environ=None):
    # Keep application credentials opaque and unchanged, but discard resolver/
    # Python injection variables before setting the fixed runner policy.
    env = {k: v for k, v in (os.environ if environ is None else environ).items()
           if (not k.startswith(('UV_', 'PYTHON')) or k in ['UV_ENV_FILE', 'UV_ENV_FILE_OVERRIDE', 'UV_NO_ENV_FILE'])
           and k != 'VIRTUAL_ENV'}
    env.update(UV_NO_SYNC='true', UV_FROZEN='true', UV_OFFLINE='true',
               UV_PYTHON_DOWNLOADS='never', PYTHONDONTWRITEBYTECODE='1',
               WHATSAPP_BRIDGE_DIR=str(LEGACY.parent / 'whatsapp-bridge'))
    return env


def main():
    if sys.argv[1:] == ['--runtime-info']:
        import importlib.metadata as md
        print(json.dumps({'python': sys.version.split()[0], 'executable': sys.executable,
                          'certifi': md.version('certifi'), 'pip': md.version('pip')}))
    else:
        cwd = LEGACY; canary = False; env = environment()
        if len(sys.argv) == 3 and sys.argv[1] == '--canary-cwd':
            cwd = Path(sys.argv[2]).resolve()
            if not (cwd / '.maintenance-canary').is_file():
                raise SystemExit('synthetic canary marker required')
            canary = True
            for key in ['WHATSAPP_BRIDGE_TOKEN', 'WHATSAPP_BRIDGE_TOKEN_FILE']:
                env.pop(key, None)
            env.update(WHATSAPP_STORE_DIR=str(cwd / 'nonexistent-store'),
                       WHATSAPP_DB_PATH=str(cwd / 'nonexistent-store/whatsapp.db'),
                       WHATSAPP_MESSAGES_DB_PATH=str(cwd / 'nonexistent-store/messages.db'),
                       WHATSAPP_API_BASE_URL='http://127.0.0.1:1/api', MCP_TRANSPORT='stdio')
        elif sys.argv[1:]:
            raise SystemExit('client launcher takes no production arguments')
        os.chdir(cwd)
        args = command(canary=canary)
        os.execve(args[0], args, env)


if __name__ == '__main__':
    main()
