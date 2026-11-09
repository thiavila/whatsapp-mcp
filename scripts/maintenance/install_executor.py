"""Bootstrap the user-level executor from this repo after its reviewed CI merge.

Infrastructure only: installs a small polling timer; no application cutover.
Run as thiago on the VM, never from the gateway cgroup.
"""
import os
import argparse
import fcntl
import re
from pathlib import Path
import shutil
import subprocess
import tempfile

ROOT=Path(__file__).resolve().parents[2]
BASE=Path('/home/thiago/hermes-maintenance/mcp-deploy')
PYTHON=Path('/home/thiago/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3')


def atomic_text(path, text):
    with tempfile.NamedTemporaryFile(mode='w',dir=path.parent,prefix='.executor-',delete=False) as stream:
        temporary=Path(stream.name)
        try:
            os.fchmod(stream.fileno(),0o600)
            stream.write(text);stream.flush();os.fsync(stream.fileno())
            os.replace(temporary,path)
            fd=os.open(path.parent,os.O_DIRECTORY)
            try:os.fsync(fd)
            finally:os.close(fd)
        finally:temporary.unlink(missing_ok=True)


def stage_control(base, source, sha):
    """An immutable version directory is complete before a unit can refer to it."""
    if not re.fullmatch('[0-9a-f]{40}',sha):raise ValueError('invalid control commit')
    releases=base/'control-releases';releases.mkdir(mode=0o700,parents=True,exist_ok=True)
    code=releases/sha
    names=['executor.py','control.py','release_ci.py','hermes_control.py','hermes_executor.py',
           'prepare_hermes.py','client_executor.py','client_archive.py',
           'indexa_auth_common.py','indexa_auth_runtime.py','indexa_auth_config.py',
           'indexa_auth_probe.py','indexa_auth_executor.py','hermes_watchdog.py']
    if code.exists():
        if code.is_symlink() or any((code/n).read_bytes()!=(source/n).read_bytes() for n in names):
            raise ValueError('existing control version differs; preserved')
        return code
    temporary=Path(tempfile.mkdtemp(prefix='.control-',dir=releases))
    try:
        for name in names:
            with (source/name).open('rb') as before,(temporary/name).open('wb') as after:
                os.fchmod(after.fileno(),0o600);shutil.copyfileobj(before,after);after.flush();os.fsync(after.fileno())
        fd=os.open(temporary,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
        os.rename(temporary,code)
        fd=os.open(releases,os.O_DIRECTORY)
        try:os.fsync(fd)
        finally:os.close(fd)
    finally:
        if temporary.exists():shutil.rmtree(temporary)
    return code


def main(expected_sha):
    if os.getuid()!=1000 or Path.home()!=Path('/home/thiago'):
        raise SystemExit('VM user thiago required')
    if 'hermes-gateway.service' in Path('/proc/self/cgroup').read_text():
        raise SystemExit('bootstrap outside gateway required')
    if not PYTHON.is_file(): raise SystemExit('compatible managed Python missing')
    if subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=no']).strip():
        raise SystemExit('clean committed source required')
    sha=subprocess.check_output(['git','-C',str(ROOT),'rev-parse','HEAD'],text=True).strip()
    if not re.fullmatch('[0-9a-f]{40}',expected_sha) or sha!=expected_sha:
        raise SystemExit('checkout is not the expected reviewed control SHA')
    os.umask(0o077)
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    # Root must hold the temporary maintenance lease described in the runbook.
    # Refuse to replace units while any old poll is running; never kill it.
    lock=(BASE/'executor.lock').open('a')
    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
    code=stage_control(BASE,ROOT/'scripts/maintenance',sha)
    units=Path.home()/'.config/systemd/user';units.mkdir(parents=True,exist_ok=True)
    service=f'''[Unit]
Description=Pull approved MCP releases outside Hermes gateway
After=network-online.target
[Service]
Type=oneshot
ExecStart={PYTHON} -I -c "import sys,runpy; sys.path.insert(0, '{code}'); runpy.run_path('{code}/executor.py', run_name='__main__')" --poll
Environment=PATH=/home/thiago/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/thiago
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
UMask=0077
Nice=10
CPUQuota=50%
MemoryMax=1536M
TimeoutStartSec=30min
NoNewPrivileges=true
'''
    atomic_text(units/'mcp-maintenance-deploy.service',service)
    atomic_text(units/'mcp-maintenance-deploy.timer','''[Unit]
Description=Poll MCP official release receipts without an LLM
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true
[Install]
WantedBy=timers.target
''')
    atomic_text(units/'hermes-health-watch.service',f'''[Unit]
Description=Check Hermes gateway health independently of deployment
[Service]
Type=oneshot
ExecStart={PYTHON} -I -c "import sys,runpy; sys.path.insert(0, '{code}'); runpy.run_path('{code}/hermes_watchdog.py', run_name='__main__')"
Environment=PATH=/home/thiago/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=HOME=/home/thiago
Environment=XDG_RUNTIME_DIR=/run/user/1000
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=/run/user/1000/bus
UMask=0077
TimeoutStartSec=90
NoNewPrivileges=true
''')
    atomic_text(units/'hermes-health-watch.timer','''[Unit]
Description=Check Hermes health every two minutes
[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
''')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','mcp-maintenance-deploy.timer'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','hermes-health-watch.timer'],check=True)
    print('MCP external executor installed; application deployment waits for an admissible release.')


if __name__=='__main__':
    parser=argparse.ArgumentParser()
    parser.add_argument('--expected-sha',required=True)
    args=parser.parse_args()
    main(args.expected_sha)
