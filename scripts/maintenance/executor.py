"""VM pull deployer, outside the Hermes gateway cgroup. Stdlib + existing gh/git.

Only successful official main release artifacts are admissible. No code from a
PR runs here. No credentials are copied into Actions or deployment artifacts.
"""
from contextlib import closing
import argparse
import datetime as dt
import fcntl
import hashlib
import io
import json
import os
import re
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import zipfile

from control import REPOSITORY, WORKFLOW, admit_run, gateway_idle, control_idle, require_sha
from release_ci import verify

BASE = Path('/home/thiago/hermes-maintenance/mcp-deploy')
REPO = Path('/home/thiago/repos/whatsapp-mcp')
HERMES = Path('/home/thiago/.hermes')
LAUNCHER = '/home/thiago/.local/bin/hermes'
BRIDGES = [
    ('whatsapp-bridge-indexa.service', 8080, REPO / 'whatsapp-bridge/store'),
    ('whatsapp-bridge-melancia@melancia-roberto.service', 8083, Path('/home/thiago/.local/share/whatsapp-bridges/melancia-roberto/store')),
    ('whatsapp-bridge-melancia@melancia-lismeri.service', 8084, Path('/home/thiago/.local/share/whatsapp-bridges/melancia-lismeri/store')),
]
WATCH_TIMERS=['wa-indexa-watch.timer','melancia-roberto-watch.timer','deploy-indexa-watch.timer','sentry-indexa-watch.timer']
WATCH_SERVICES=[unit.replace('.timer','.service') for unit in WATCH_TIMERS]
CONSUMERS=WATCH_TIMERS+WATCH_SERVICES+['hermes-gateway.service']
BINARY = REPO / 'whatsapp-bridge/whatsapp-bridge'


def run(args, timeout=60, cwd=None, env=None):
    return subprocess.check_output(args, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, timeout=timeout).decode().strip()


def api(path):
    return json.loads(run(['gh', 'api', path]))


def atomic(path, data):
    path = Path(path)
    temporary = path.with_name(path.name + '.tmp')
    with temporary.open('w') as stream:
        json.dump(data, stream, sort_keys=True, indent=2)
        stream.flush(); os.fsync(stream.fileno())
    temporary.chmod(0o600); temporary.replace(path)
    directory = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(directory)
    finally: os.close(directory)


def ctl(action, unit):
    if unit not in CONSUMERS + [b[0] for b in BRIDGES]:
        raise ValueError('unknown unit')
    if action=='stop' and unit=='hermes-gateway.service':
        # Native refusal protects turns/delegations that start after the idle probe.
        return run([LAUNCHER,'gateway','stop'],timeout=240)
    return run(['systemctl', '--user', action, unit], timeout=240)


def busy_service(unit):
    state=run(['systemctl','--user','show',unit,'-p','ActiveState','--value'])
    return state not in ['inactive','failed']


def active(unit):
    return run(['systemctl', '--user', 'show', unit, '-p', 'ActiveState', '--value']) == 'active'



def gateway_quiet():
    try:
        state=json.loads((HERMES/'gateway_state.json').read_text())
        pid=int(run(['systemctl','--user','show','hermes-gateway.service','-p','MainPID','--value']))
        shim=Path(LAUNCHER).read_text()
        match=re.search(r'exec (/home/thiago/worktrees/hermes-[A-Za-z0-9_-]+)/\.hermes/bin/hermes',shim)
        if not match or pid<=1:return False
        python=Path(f'/proc/{pid}/exe').resolve()
        if not str(python).startswith('/home/thiago/.hermes/tools/python-'):return False
        code=("import sys,json;from pathlib import Path;sys.path.insert(0,"+repr(match.group(1))+");"
              "from gateway.control_socket import query_gateway_control;"
              "print(json.dumps(query_gateway_control(Path('/home/thiago/.hermes'),'active-work',timeout=7)))")
        env={'HOME':'/home/thiago','HERMES_HOME':str(HERMES),'PATH':'/usr/bin:/bin','LANG':'C.UTF-8'}
        live=json.loads(run([str(python),'-I','-c',code],timeout=10,env=env))
        return control_idle(state,live,pid,time.time())
    except (OSError,ValueError,subprocess.SubprocessError):
        return False


def notify(key, message):
    path = BASE / 'notifications.json'
    previous = json.loads(path.read_text()) if path.exists() else {}
    if key in previous: return
    # Official CLI reuses the principal's configured delivery, without an LLM.
    subprocess.run([LAUNCHER, '-p', 'default', 'send', '--to', 'telegram:6792818290', '--file', '-'],
                   input=message.encode(), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                   timeout=30, check=True)
    previous[key] = int(time.time()); atomic(path, previous)


def digest(path):
    with Path(path).open('rb') as stream:
        h = hashlib.file_digest(stream, 'sha256')
    return h.hexdigest()


def db_schema(path):
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as connection:
        if connection.execute('PRAGMA quick_check').fetchall() != [('ok',)]:
            raise ValueError('database integrity check failed')
        rows = connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        version = connection.execute('PRAGMA user_version').fetchone()[0]
        # Include migration bookkeeping values without logging any data.
        migration_tables={'whatsmeow_version','schema_version','schema_migrations','alembic_version','db_version'}
        tables=[row[1] for row in rows if row[0]=='table' and row[1].lower() in migration_tables]
        metadata = []
        for table in tables:
            quoted = '"' + table.replace('"','""') + '"'
            metadata.append((table, connection.execute(f'SELECT * FROM {quoted} LIMIT 1000').fetchall()))
        return hashlib.sha256(repr((rows, version, metadata)).encode()).hexdigest()


def copy_databases(store, destination):
    destination.mkdir(mode=0o700, parents=True, exist_ok=True)
    schemas = {}
    for name in ['whatsapp.db', 'messages.db']:
        source = store / name
        if not source.is_file() or source.is_symlink():
            raise ValueError('expected regular paired database absent')
        target = destination / name
        with closing(sqlite3.connect(f'file:{source}?mode=ro', uri=True)) as before:
            with closing(sqlite3.connect(target)) as after:
                before.backup(after)
        target.chmod(0o600)
        schemas[name] = db_schema(target)
    return schemas


def offline_canary(binary, destination, expected):
    (destination / '.maintenance-canary').touch(mode=0o600)
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(destination), 'LANG':'C.UTF-8'}
    result = run([str(binary), '-check-store', '-store-dir', str(destination)], env=env, timeout=90)
    if result.splitlines()[-1:] != ['STORE_CANARY_OK']:
        raise ValueError('candidate does not support offline store verification')
    if {n: db_schema(destination / n) for n in expected} != expected:
        raise ValueError('schema or migration version change: automatic deployment blocked')


def health(port):
    with urllib.request.urlopen(f'http://127.0.0.1:{port}/api/health', timeout=4) as response:
        payload = json.loads(response.read(8192))
    return payload.get('success') is True and payload.get('connected') is True and payload.get('logged_in') is True


def healthy(unit, port):
    for _ in range(24):
        try:
            if active(unit) and health(port): return
        except (OSError, ValueError): pass
        time.sleep(2)
    raise ValueError('bridge failed connected/authenticated health check')


def ensure_clean():
    if run(['git', '-C', str(REPO), 'status', '--porcelain', '--untracked-files=no']):
        raise ValueError('production source has tracked local changes')
    if run(['git', '-C', str(REPO), 'branch', '--show-current']) != 'main':
        raise ValueError('production checkout must remain on main')
    remote = run(['git', '-C', str(REPO), 'remote', 'get-url', 'origin'])
    if not remote.endswith('thiavila/whatsapp-mcp.git'):
        raise ValueError('unexpected source remote')


def atomic_binary(source):
    temporary = BINARY.with_name('.whatsapp-bridge.next')
    shutil.copyfile(source, temporary); temporary.chmod(0o755)
    with temporary.open('rb') as stream: os.fsync(stream.fileno())
    temporary.replace(BINARY)


def resume_consumers(states):
    order = ['hermes-gateway.service',*WATCH_TIMERS]
    for unit in order:
        if states.get(unit): ctl('start', unit)



def upgrade_journal(tx):
    if tx.get('schema_fingerprint_version')==2:return tx
    # v1 accidentally included normal WhatsApp app-state sync versions.
    # Reconstruct only from the deployment's verified stopped-store backups.
    schemas={}
    for index,(_,_,store) in enumerate(BRIDGES):
        backup=BASE/'releases'/tx['sha']/'backups'/str(index)
        schemas[str(store)]={name:db_schema(backup/name) for name in ['whatsapp.db','messages.db']}
        if schemas[str(store)]!={name:db_schema(store/name) for name in schemas[str(store)]}:
            raise ValueError('actual migration differs from retained backups')
    tx=dict(tx,schemas=schemas,schema_fingerprint_version=2)
    atomic(BASE/'pending.json',tx)
    return tx


def finish_pending_if_healthy(tx):
    if run(['git','-C',str(REPO),'rev-parse','HEAD'])!=tx['sha']:return False
    candidate=BASE/'releases'/tx['sha']/'whatsapp-bridge'
    if not candidate.is_file() or digest(candidate)!=digest(BINARY):return False
    for unit,port,store in BRIDGES:
        if not active(unit) or not health(port):return False
        if {n:db_schema(store/n) for n in tx['schemas'][str(store)]}!=tx['schemas'][str(store)]:return False
    if tx['consumers'].get('hermes-gateway.service'):
        if not active('hermes-gateway.service'):return False
        for _ in range(150):
            state=json.loads((HERMES/'gateway_state.json').read_text())
            if state.get('gateway_state')=='running' and state.get('platforms',{}).get('telegram',{}).get('state')=='connected':break
            time.sleep(2)
        else:return False
    workflow=api(f'repos/{REPOSITORY}/actions/workflows/release.yml')
    admitted=api(f'repos/{REPOSITORY}/actions/runs/{int(tx["run_id"])}')
    admit_run(admitted,workflow['id'],tx['sha']);verify(tx['sha'])
    resume_consumers(tx['consumers'])
    atomic(BASE/'last-success.json',tx);(BASE/'pending.json').unlink()
    notify('success-'+tx['sha'],f"MCP atualizado e validado pela rotina da VM: {tx['previous_sha'][:12]} → {tx['sha'][:12]}. Origem/CI do pipeline, bancos compatíveis, três bridges autenticadas e gateway retomado. Dados e sessões atuais preservados.")
    return True


def recover():
    journal = BASE / 'pending.json'
    if not journal.exists(): return
    tx = upgrade_journal(json.loads(journal.read_text()))
    if finish_pending_if_healthy(tx):return 'completed'
    # Same-schema updates only; always preserve current sessions/messages.
    for unit in CONSUMERS: ctl('stop', unit)
    for unit, _, _ in BRIDGES: ctl('stop', unit)
    for _, _, store in BRIDGES:
        expected = tx['schemas'][str(store)]
        if {n:db_schema(store/n) for n in expected} != expected:
            notify('unsafe-rollback-' + tx['sha'], 'Atualização do MCP interrompida: o banco mudou de esquema. Serviços preservados parados para evitar perda de dados; preciso de intervenção. Nenhum banco ou token antigo foi restaurado.')
            raise ValueError('unsafe rollback: schema drift; live data preserved')
    ensure_clean()
    current = run(['git','-C',str(REPO),'rev-parse','HEAD'])
    if current not in [tx['sha'], tx['previous_sha']]:
        raise ValueError('foreign source commit during recovery; preserved')
    run(['git','-c','core.hooksPath=/dev/null','-C',str(REPO),'reset','--hard',require_sha(tx['previous_sha'])])
    atomic_binary(Path(tx['old_binary']))
    for unit, port, _ in BRIDGES:
        if tx['bridges'].get(unit): ctl('start',unit); healthy(unit,port)
    resume_consumers(tx['consumers'])
    atomic(BASE/'last-rollback.json',tx)
    journal.unlink()
    notify('rollback-'+tx['sha'],f"MCP: atualização {tx['sha'][:12]} revertida para {tx['previous_sha'][:12]}. Sessões e bancos atuais foram preservados. Vou bloquear esse candidato até uma nova correção.")
    atomic(BASE/'blocked.json', {'sha':tx['sha']})


def artifact(run_id, sha, destination):
    records = api(f'repos/{REPOSITORY}/actions/runs/{run_id}/artifacts')['artifacts']
    candidates = [a for a in records if a['name']=='mcp-vm-release' and not a['expired']]
    if len(candidates)!=1: raise ValueError('expected one unexpired release artifact')
    a=candidates[0]
    if not 0 < a['size_in_bytes'] < 100*1024**2: raise ValueError('artifact size bound')
    raw=subprocess.check_output(['gh','api',f'repos/{REPOSITORY}/actions/artifacts/{int(a["id"])}/zip'],timeout=90)
    if len(raw)>100*1024**2 or a.get('digest')!='sha256:'+hashlib.sha256(raw).hexdigest():
        raise ValueError('GitHub artifact digest mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sorted(archive.namelist())!=['manifest.json','whatsapp-bridge']:
            raise ValueError('unexpected artifact members')
        if any(i.file_size>100*1024**2 for i in archive.infolist()): raise ValueError('expanded artifact size')
        manifest=json.loads(archive.read('manifest.json'))
        binary=archive.read('whatsapp-bridge')
    if (set(manifest)!= {'schema','repository','sha','binary_sha256'} or manifest['schema']!=1
            or manifest['repository']!=REPOSITORY or manifest['sha']!=sha
            or hashlib.sha256(binary).hexdigest()!=manifest['binary_sha256']):
        raise ValueError('release manifest mismatch')
    destination.write_bytes(binary); destination.chmod(0o700)


def deploy(run_id, sha):
    require_sha(sha); ensure_clean()
    if not gateway_quiet():
        print('deferred: Hermes is busy or its state is stale'); return
    if any(not active(unit) or not health(port) for unit,port,_ in BRIDGES):
        raise ValueError('all bridges must be healthy before deployment')
    run(['git','-C',str(REPO),'fetch','--quiet','origin','main'])
    if run(['git','-C',str(REPO),'rev-parse','origin/main'])!=sha:
        print('superseded release; waiting for current main'); return
    import client_executor
    if not client_executor.source_ready(sha):
        print('deferred: client activation required before exposing Python pins')
        notify('go-client-activation-'+sha,'Release das bridges aguardando ativação segura dos clientes MCP. Binário, dados e serviços atuais preservados; retomo automaticamente quando o gate do runner passar.')
        return
    previous=require_sha(run(['git','-C',str(REPO),'rev-parse','HEAD']))
    release=BASE/'releases'/sha
    release.mkdir(mode=0o700,parents=True,exist_ok=True)
    binary=release/'whatsapp-bridge'
    artifact(run_id,sha,binary)
    required=sum((store/n).stat().st_size for _,_,store in BRIDGES for n in ['messages.db','whatsapp.db'])
    if shutil.disk_usage(BASE).free < 1024**3+3*required:
        raise ValueError('insufficient free disk for private backups and canaries')
    schemas={}
    with tempfile.TemporaryDirectory(prefix='canary-',dir=release) as temp:
        for index,(_,_,store) in enumerate(BRIDGES):
            target=Path(temp)/str(index)
            schemas[str(store)]=copy_databases(store,target)
            offline_canary(binary,target,schemas[str(store)])
    tx={'schema_fingerprint_version':2,'sha':sha,'run_id':run_id,'previous_sha':previous,'old_binary':str(release/'previous-binary'),
        'schemas':schemas,'consumers':{u:active(u) for u in CONSUMERS},
        'bridges':{u:active(u) for u,_,_ in BRIDGES}}
    shutil.copyfile(BINARY,tx['old_binary']); Path(tx['old_binary']).chmod(0o700)
    if not gateway_quiet():
        print('deferred: work started during staging'); return
    if any(busy_service(u) for u in WATCH_SERVICES):
        print('deferred: watcher is processing work'); return
    # Persist before any service stops: crash recovery knows exactly what to resume.
    atomic(BASE/'pending.json',tx)
    try:
        for unit in WATCH_TIMERS:ctl('stop',unit)
        if any(busy_service(u) for u in WATCH_SERVICES):
            resume_consumers(tx['consumers']);(BASE/'pending.json').unlink()
            print('deferred: watcher started while staging');return
        try:ctl('stop','hermes-gateway.service')
        except subprocess.CalledProcessError:
            if active('hermes-gateway.service'):
                resume_consumers(tx['consumers']);(BASE/'pending.json').unlink()
                print('deferred by native gateway stop guard');return
            raise
        ensure_clean()
        run(['git','-c','core.hooksPath=/dev/null','-C',str(REPO),'merge','--ff-only',sha])
        # Old instances continue using their old mapped executable; each is promoted separately.
        atomic_binary(binary)
        for index,(unit,port,store) in enumerate(BRIDGES):
            ctl('stop',unit)
            copy_databases(store,release/'backups'/str(index))
            ctl('start',unit); healthy(unit,port)
            if {n:db_schema(store/n) for n in schemas[str(store)]}!=schemas[str(store)]:
                raise ValueError('live schema changed after candidate preflight')
        resume_consumers(tx['consumers'])
        if tx['consumers']['hermes-gateway.service']:
            for attempt in range(150):
                state=json.loads((HERMES/'gateway_state.json').read_text())
                if active('hermes-gateway.service') and state.get('gateway_state')=='running' and state.get('platforms',{}).get('telegram',{}).get('state')=='connected': break
                time.sleep(2)
            else: raise ValueError('Hermes gateway did not resume connected')
        atomic(BASE/'last-success.json',tx)
        (BASE/'pending.json').unlink()
        notify('success-'+sha,f'MCP atualizado pela rotina da VM: {previous[:12]} → {sha[:12]}. CI oficial aprovado; canário offline em cópias dos três bancos, backups privados e três bridges conectados. Hermes principal retomado. Rollback conserva os bancos e sessões atuais.')
    except BaseException as error:
        atomic(BASE/'last-error.json',{'sha':sha,'type':type(error).__name__,'message':str(error)})
        if recover()=='completed':return
        raise


def poll():
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    with (BASE/'executor.lock').open('w') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        # Auth journals fence restarts in every other lane, including recovery.
        import indexa_auth_executor
        if not indexa_auth_executor.poll_safely():
            return
        recover()
        import hermes_executor
        hermes_executor.poll()
        # Same external lock; client-only publication never stops the bridges.
        import client_executor
        client_executor.poll_safely()
        workflow=api(f'repos/{REPOSITORY}/actions/workflows/release.yml')
        if workflow['path']!=WORKFLOW: raise ValueError('workflow identity mismatch')
        runs=api(f'repos/{REPOSITORY}/actions/workflows/{workflow["id"]}/runs?branch=main&per_page=1')['workflow_runs']
        if not runs or runs[0]['status']!='completed': return
        candidate=runs[0]; sha=require_sha(candidate['head_sha']); run_id=int(candidate['id'])
        done=BASE/'last-success.json'; blocked=BASE/'blocked.json'
        if any(p.exists() and json.loads(p.read_text()).get('sha')==sha for p in [done,blocked]): return
        if candidate['conclusion']!='success':
            notify('pipeline-failed-'+str(run_id),'O pipeline oficial do MCP falhou. Mantive a versão atual e vou aguardar uma execução corrigida.'); return
        complete=api(f'repos/{REPOSITORY}/actions/runs/{run_id}')
        admit_run(complete,workflow['id'],sha)
        verify(sha)
        deploy(run_id,sha)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--poll',action='store_true');args=parser.parse_args()
    os.umask(0o077)
    if not args.poll: parser.error('--poll required')
    try: poll()
    except BlockingIOError: pass
    except Exception as error:
        print('MCP deployment blocked:',type(error).__name__,str(error) if isinstance(error,ValueError) else '(private operational error)')
        if BASE.exists():
            try: notify('error-'+hashlib.sha256((type(error).__name__+str(error)).encode()).hexdigest()[:16],
                        'A rotina de atualização do MCP encontrou um bloqueio técnico. Mantive ou recuperei a versão anterior; detalhes privados em hermes-maintenance/mcp-deploy. Nenhum dado ou credencial foi restaurado automaticamente.')
            except Exception: pass
        raise SystemExit(1)
