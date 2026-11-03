"""Bootstrap the user-level executor from this repo after its reviewed CI merge.

Infrastructure only: installs a small polling timer; no application cutover.
Run as thiago on the VM, never from the gateway cgroup.
"""
import os
from pathlib import Path
import shutil
import subprocess

ROOT=Path(__file__).resolve().parents[2]
BASE=Path('/home/thiago/hermes-maintenance/mcp-deploy')
PYTHON=Path('/home/thiago/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3')


def main():
    if os.getuid()!=1000 or Path.home()!=Path('/home/thiago'):
        raise SystemExit('VM user thiago required')
    if 'hermes-gateway.service' in Path('/proc/self/cgroup').read_text():
        raise SystemExit('bootstrap outside gateway required')
    if not PYTHON.is_file(): raise SystemExit('compatible managed Python missing')
    if subprocess.check_output(['git','-C',str(ROOT),'status','--porcelain','--untracked-files=no']).strip():
        raise SystemExit('clean committed source required')
    os.umask(0o077)
    code=BASE/'control';code.mkdir(mode=0o700,parents=True,exist_ok=True)
    for name in ['executor.py','control.py','release_ci.py','hermes_control.py','hermes_executor.py','prepare_hermes.py']:
        shutil.copyfile(ROOT/'scripts/maintenance'/name,code/name)
        (code/name).chmod(0o600)
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
    (units/'mcp-maintenance-deploy.service').write_text(service)
    (units/'mcp-maintenance-deploy.timer').write_text('''[Unit]
Description=Poll MCP official release receipts without an LLM
[Timer]
OnBootSec=3min
OnUnitActiveSec=5min
RandomizedDelaySec=30s
Persistent=true
[Install]
WantedBy=timers.target
''')
    subprocess.run(['systemctl','--user','daemon-reload'],check=True)
    subprocess.run(['systemctl','--user','enable','--now','mcp-maintenance-deploy.timer'],check=True)
    print('MCP external executor installed; application deployment waits for an admissible release.')


if __name__=='__main__': main()
