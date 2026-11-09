"""Receipt-bound Hermes cutover using the upstream installer, never auth restore."""
from contextlib import closing
import datetime as dt
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import sqlite3
import subprocess
import tempfile
import time
import zipfile

import executor as common
from control import REPOSITORY, require_sha, gateway_idle
from hermes_control import candidate_root, stable_release, validate_request

BASE=Path('/home/thiago/hermes-maintenance/hermes-deploy')
HOME=Path('/home/thiago/.hermes')
WORKFLOW='.github/workflows/hermes-maintenance.yml'
CONSUMERS=[*common.WATCH_TIMERS,*common.WATCH_SERVICES,'hermes-dashboard.service','hermes-gateway.service']
UNITS=Path('/home/thiago/.config/systemd/user')
SHIM=Path('/home/thiago/.local/bin/hermes')


def command(args,root=None,timeout=120,log=None):
    env={'HOME':'/home/thiago','HERMES_HOME':str(HOME),'PATH':'/home/thiago/.local/bin:/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8',
         'XDG_RUNTIME_DIR':'/run/user/1000','DBUS_SESSION_BUS_ADDRESS':'unix:path=/run/user/1000/bus'}
    if log is None:return subprocess.check_output(args,cwd=root,env=env,stdin=subprocess.DEVNULL,stderr=subprocess.PIPE,timeout=timeout).decode().strip()
    with Path(log).open('ab') as stream:
        subprocess.run(args,cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,timeout=timeout,check=True)


def ctl(action,unit):
    if unit not in CONSUMERS:raise ValueError('Hermes unit outside scope')
    if action=='stop' and unit=='hermes-gateway.service':
        return command([str(SHIM),'gateway','stop'],timeout=240)
    return command(['systemctl','--user',action,unit],timeout=240)


def active(unit):
    return command(['systemctl','--user','show',unit,'-p','ActiveState','--value'])=='active'


def json_file(path):return json.loads(Path(path).read_text())


def databases():
    return sorted(p for p in HOME.rglob('*.db') if p.is_file() and not p.is_symlink()
                  and not any(x in p.parts for x in ['node_modules','.git','tools','installs','hermes-agent']))


def live_schemas():return {str(p):common.db_schema(p) for p in databases()}


def snapshot(destination):
    destination.mkdir(mode=0o700,parents=True,exist_ok=True)
    schemas={}
    for path in databases():
        target=destination/path.relative_to(HOME);target.parent.mkdir(parents=True,exist_ok=True)
        with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as source:
            with closing(sqlite3.connect(target)) as copy:source.backup(copy)
        target.chmod(0o600);schemas[str(path)]=common.db_schema(target)
    return schemas



def snapshot_archive(destination):
    # A canary copy is already gone before this phase. Keep one database at a
    # time, then retain a verified compressed backup rather than a second full tree.
    destination=Path(destination);schemas={}
    temporary=destination.with_suffix('.tmp')
    with tempfile.TemporaryDirectory(prefix='sqlite-copy-',dir=destination.parent) as folder:
        with zipfile.ZipFile(temporary,'w',compression=zipfile.ZIP_DEFLATED,compresslevel=1) as archive:
            for path in sorted(databases(),key=lambda p:p.stat().st_size,reverse=True):
                # Budget both a disposable database and its worst-case ZIP growth.
                if shutil.disk_usage(destination.parent).free<1024**3+2*path.stat().st_size:
                    raise ValueError('compressed backup disk reserve')
                target=Path(folder)/'snapshot.db'
                with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as source:
                    with closing(sqlite3.connect(target)) as copy:source.backup(copy)
                schemas[str(path)]=common.db_schema(target)
                archive.write(target,str(path.relative_to(HOME)))
                target.unlink()
    temporary.chmod(0o600)
    with zipfile.ZipFile(temporary) as archive:
        if archive.testzip() is not None:raise ValueError('compressed snapshot integrity')
    temporary.replace(destination)
    return schemas


def validate_local(request):
    receipt_path=BASE/'receipts'/request['candidate_sha']/'receipt.json'
    raw=receipt_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=request['receipt_sha256']:raise ValueError('test receipt changed')
    receipt=json.loads(raw);root=candidate_root(receipt['root'])
    if (receipt.get('result')!='passed' or receipt.get('runner')!='official-per-file-private-home'
            or any(receipt.get(k)!=request[k] for k in ['candidate_sha','upstream_sha','tag'])
            or receipt.get('tests_log_sha256')!=common.digest(receipt_path.parent/'tests.log')):
        raise ValueError('candidate test receipt invalid')
    if command(['git','-C',str(root),'rev-parse','HEAD'])!=request['candidate_sha']:
        raise ValueError('candidate HEAD changed')
    if command(['git','-C',str(root),'status','--porcelain','--untracked-files=no']):raise ValueError('candidate dirty')
    command(['git','-C',str(root),'merge-base','--is-ancestor',request['upstream_sha'],request['candidate_sha']])
    if common.digest(root/'scripts/install.sh')!=receipt['installer_sha256']:raise ValueError('installer changed')
    if not (root/'.hermes/bin/hermes').is_file():raise ValueError('official candidate staging is incomplete')
    return root


def request_artifact(run):
    records=common.api(f'repos/{REPOSITORY}/actions/runs/{run["id"]}/artifacts')['artifacts']
    candidates=[a for a in records if a['name']=='hermes-installer-request' and not a['expired']]
    if len(candidates)!=1:raise ValueError('expected one Hermes request artifact')
    artifact=candidates[0]
    if not 0<artifact['size_in_bytes']<65536:raise ValueError('Hermes artifact size')
    raw=subprocess.check_output(['gh','api',f'repos/{REPOSITORY}/actions/artifacts/{artifact["id"]}/zip'],timeout=60)
    if len(raw)>65536 or artifact.get('digest')!='sha256:'+hashlib.sha256(raw).hexdigest():raise ValueError('Hermes artifact digest')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if archive.namelist()!=['request.json'] or archive.infolist()[0].file_size>8192:raise ValueError('Hermes artifact members')
        request=json.loads(archive.read('request.json'))
    validate_request(request)
    release=common.api('repos/NousResearch/hermes-agent/releases/tags/'+request['tag'])
    stable_release(release,request['tag'],request['upstream_sha'],dt.datetime.now(dt.timezone.utc))
    official=common.api('repos/NousResearch/hermes-agent/commits/'+request['tag'])
    if official.get('sha')!=request['upstream_sha']:raise ValueError('tag does not match official upstream commit')
    return request


def state_files():
    result=[]
    for root in [HOME,*(HOME/'profiles').glob('*')]:
        if root.is_dir():
            for name in ['config.yaml','.env','auth.json']:
                path=root/name
                if path.is_file() and not path.is_symlink():result.append(path)
    return result



def active_session_copies(copies,schemas):
    # Historical snapshots and nested agent sandboxes are backups, not profiles
    # served by this gateway. Never migrate them while testing an app update.
    return [str(copies/Path(p).relative_to(HOME)) for p in schemas
            if Path(p)==HOME/'state.db' or
            (Path(p).name=='state.db' and Path(p).parent.parent==HOME/'profiles')]


def canary(root,copies,schemas,log):
    # Exercise the known gateway stores, including shared state and cron ledgers.
    # New upstream store APIs fail closed here, BEFORE stopping live consumers.
    paths=active_session_copies(copies,schemas)
    with tempfile.TemporaryDirectory(prefix='hermes-home-') as home:
        code=('import os; from pathlib import Path; from hermes_state import SessionDB; '
              'from cron import executions, incidents, notepad; '
              'from gateway import hosted_rooms; '
              'p=Path(os.environ["HERMES_HOME"]); '
              'SessionDB(p/"state.db").close(); '
              'executions._connect().close(); incidents._connect().close(); notepad._connect().close(); '
              'hosted_rooms._connect(Path(os.environ["CANARY_SHARED_DB"])).close()')
        for path in paths:
            command(['bash',str(root/'scripts/run-in-hermes-env'),'env','HOME='+home,
                     'HERMES_HOME='+str(Path(path).parent),
                     'CANARY_SHARED_DB='+str(copies/'shared-state.db'),
                     'python3','-c',code],root,timeout=180,log=log)
        copied={str(HOME/p.relative_to(copies)):common.db_schema(p)
                for p in copies.rglob('*.db') if p.is_file()}
        if set(copied)!=set(schemas):
            raise ValueError('Hermes store inventory migration: automatic promotion blocked')
        for path in schemas:
            if copied[path]!=schemas[path]:
                raise ValueError('Hermes schema migration: automatic promotion blocked for '+str(Path(path).relative_to(HOME)))
        command([str(root/'.hermes/bin/hermes'),'--help'],root,timeout=60,log=log)


def resume(states):
    for unit in ['hermes-gateway.service','hermes-dashboard.service',*common.WATCH_TIMERS]:
        if states.get(unit):ctl('start',unit)


def healthy(states,sha):
    for _ in range(150):
        current=json_file(HOME/'gateway_state.json')
        if (not states['hermes-gateway.service'] or
            (active('hermes-gateway.service') and current.get('gateway_state')=='running'
             and current.get('code_sha')==sha
             and current.get('platforms',{}).get('telegram',{}).get('state')=='connected')):
            if not states['hermes-dashboard.service'] or active('hermes-dashboard.service'):return
        time.sleep(2)
    raise ValueError('Hermes candidate did not resume healthy')


def intervention(tx,reason):
    # Persist BEFORE notification: a delivery failure must never re-arm stops.
    common.atomic(BASE/'intervention.json',{
        'candidate_sha':tx['candidate_sha'],'run_id':tx['run_id'],'reason':reason})
    try:
        common.notify('hermes-intervention-'+tx['candidate_sha'],
            'Hermes: recuperação automática bloqueada. Dados e versão instalada preservados; '
            'nenhuma nova tentativa de parar serviços será feita. Atualizações suspensas; '
            'é necessária intervenção para conferir a saúde do gateway e os bancos atuais.')
    except (OSError,subprocess.SubprocessError,ValueError):
        # Subsequent polls retry delivery, but never repeat service operations.
        pass
    return False


def recover():
    journal=BASE/'pending.json'
    if not journal.exists():return True
    tx=json_file(journal)
    marker=BASE/'intervention.json'
    if marker.exists():
        return intervention(tx,json_file(marker)['reason'])
    # Schema compatibility must be established BEFORE any stop, including when
    # reconciling legacy journals written by the old controller.
    try: compatible=live_schemas()==tx['schemas']
    except (OSError,sqlite3.Error,ValueError):
        return intervention(tx,'schema-unreadable')
    if not compatible:return intervention(tx,'schema-incompatible')
    for unit in CONSUMERS:ctl('stop',unit)
    # Close the check/stop race; never restore old code over newly migrated data.
    try: compatible=live_schemas()==tx['schemas']
    except (OSError,sqlite3.Error,ValueError):
        return intervention(tx,'schema-unreadable-after-stop')
    if not compatible:return intervention(tx,'schema-changed-during-stop')
    backup=Path(tx['backup'])
    for name in ['hermes-gateway.service','hermes-dashboard.service']:
        path=UNITS/name
        if (backup/name).exists():shutil.copyfile(backup/name,path)
    shutil.copyfile(backup/'hermes-shim',SHIM);SHIM.chmod(0o755)
    command(['systemctl','--user','daemon-reload'])
    resume(tx['units']);healthy(tx['units'],tx['previous_sha'])
    common.atomic(BASE/'last-rollback.json',tx);journal.unlink()
    # A rollback blocks this exact candidate under the control code that
    # observed the failure.  A reviewed control release may intentionally
    # retry it after fixing (or proving transient) the gate; unchanged code
    # still suppresses every timer poll.
    common.atomic(BASE/'blocked.json',{
        'candidate_sha':tx['candidate_sha'],
        'control_sha256':common.digest(Path(__file__)),
    })
    common.notify('hermes-rollback-'+tx['candidate_sha'],'Hermes: atualização revertida para a instalação anterior. Preservei sessões, filas, configuração e autenticação atuais; candidato bloqueado até correção.')
    return True


def apply(request,run_id):
    root=validate_local(request)
    state=json_file(HOME/'gateway_state.json')
    if request['action']=='promote' and not common.gateway_quiet():
        print('Hermes update deferred: active work');return False
    if request['action']=='promote' and state.get('code_sha')!=request['candidate_sha']:
        current=tuple(int(n) for n in state.get('code_version','0.0.0').lstrip('v').split('+')[0].split('.')[:3])
        target=tuple(int(n) for n in request['tag'].lstrip('v').split('.'))
        if target<=current:raise ValueError('Hermes automatic downgrade or same-version patch requires explicit deployment')
    if any(common.busy_service(u) for u in common.WATCH_SERVICES):return False
    # Preserve live config/auth. Official config migration is not in this automation.
    config_hashes={str(p):common.digest(p) for p in state_files() if p.name!='auth.json'}
    required=sum(p.stat().st_size for p in databases())
    if shutil.disk_usage(BASE).free<1024**3+required:raise ValueError('Hermes snapshot disk gate')
    destination=BASE/'runs'/str(run_id);destination.mkdir(mode=0o700,parents=True,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hermes-canary-',dir=destination) as temporary:
        schemas=snapshot(Path(temporary));canary(root,Path(temporary),schemas,destination/'canary.log')
    if request['action']=='check':
        common.atomic(BASE/'last-check.json',{'run_id':run_id,'candidate_sha':request['candidate_sha'],'result':'passed','executor_cgroup':Path('/proc/self/cgroup').read_text().strip()})
        common.notify('hermes-check-'+str(run_id),'Executor do Hermes validado na VM, fora do gateway: recibo dos testes, release oficial, canário de bancos em cópias privadas e saúde conferidos. Este teste não trocou a versão em produção.')
        return True
    state=json_file(HOME/'gateway_state.json')
    if not common.gateway_quiet():return False
    if state.get('code_sha')==request['candidate_sha']:
        common.atomic(BASE/'last-success.json',{'run_id':run_id,'candidate_sha':request['candidate_sha'],'result':'already_current'})
        return True
    backup=destination/'backup';backup.mkdir(mode=0o700,exist_ok=True)
    for name in ['hermes-gateway.service','hermes-dashboard.service']:
        if (UNITS/name).is_file():shutil.copyfile(UNITS/name,backup/name)
    shutil.copyfile(SHIM,backup/'hermes-shim')
    units={u:active(u) for u in CONSUMERS}
    tx={'candidate_sha':request['candidate_sha'],'previous_sha':require_sha(state['code_sha']),
        'backup':str(backup),'units':units,'schemas':schemas,'run_id':run_id}
    common.atomic(BASE/'pending.json',tx)
    try:
        for unit in common.WATCH_TIMERS:ctl('stop',unit)
        if any(common.busy_service(u) for u in common.WATCH_SERVICES):
            resume(units);(BASE/'pending.json').unlink();return False
        try:ctl('stop','hermes-gateway.service')
        except subprocess.CalledProcessError:
            if active('hermes-gateway.service'):
                resume(units);(BASE/'pending.json').unlink();return False
            raise
        ctl('stop','hermes-dashboard.service')
        tx['schemas']=snapshot_archive(backup/'sqlite-backup.zip')
        tx['sqlite_archive_sha256']=common.digest(backup/'sqlite-backup.zip')
        common.atomic(BASE/'pending.json',tx)
        # Snapshot configs privately; never replay auth.json in recovery.
        for path in state_files():
            target=backup/'config'/path.relative_to(HOME);target.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(path,target);target.chmod(0o600)
        validate_local(request)
        # Upstream install stages publish the launcher/products. No file-copy app deploy.
        branch=command(['git','-C',str(root),'branch','--show-current'])
        if not branch:raise ValueError('candidate needs a named local branch')
        for stage in ['products','complete']:
            command(['bash',str(root/'scripts/install.sh'),'--dir',str(root),'--stage',stage,
                     '--non-interactive','--branch',branch,'--commit',request['candidate_sha']],root,timeout=900,log=destination/'installer.log')
        command([str(root/'.hermes/bin/hermes'),'gateway','install','--force','--no-start-now','--start-on-login'],root,timeout=120,log=destination/'gateway-install.log')
        if any(common.digest(Path(p))!=sha for p,sha in config_hashes.items()):
            raise ValueError('material config migration requires explicit decision')
        command(['systemctl','--user','daemon-reload']);resume(units);healthy(units,request['candidate_sha'])
        if live_schemas()!=tx['schemas']:raise ValueError('live schema differs from canary')
        common.atomic(BASE/'last-success.json',tx);(BASE/'pending.json').unlink()
        common.notify('hermes-success-'+request['candidate_sha'],f"Hermes atualizado pelo instalador oficial: {tx['previous_sha'][:12]} → {request['candidate_sha'][:12]} ({request['tag']}). Testes, backup e canário passaram. Gateway, dashboard e principal retomados; perfis, filas e autenticação preservados.")
        return True
    except BaseException:
        recover();raise


def poll():
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True)
    if (BASE/'intervention.json').exists() and not (BASE/'pending.json').exists():
        return False  # Orphan marker still fences all lanes until operator review.
    if recover() is False:return False
    try: workflow=common.api(f'repos/{REPOSITORY}/actions/workflows/hermes-maintenance.yml')
    except subprocess.CalledProcessError:return  # Initial bootstrap before workflow merge.
    runs=common.api(f'repos/{REPOSITORY}/actions/workflows/{workflow["id"]}/runs?branch=main&per_page=1')['workflow_runs']
    if not runs or runs[0]['status']!='completed' or runs[0]['conclusion']!='success':return
    run=common.api(f'repos/{REPOSITORY}/actions/runs/{int(runs[0]["id"])}')
    if (run.get('path')!=WORKFLOW or run.get('workflow_id')!=workflow['id']
            or run.get('repository',{}).get('full_name')!=REPOSITORY
            or run.get('head_repository',{}).get('full_name')!=REPOSITORY
            or run.get('head_branch')!='main' or run.get('event')!='workflow_dispatch'
            or run.get('conclusion')!='success'):
        raise ValueError('Hermes request provenance')
    done=BASE/'last-run.json'
    if done.exists() and json_file(done).get('run_id')==run['id']:return
    failed=BASE/'failed-run.json'
    control_hash=common.digest(Path(__file__))
    if failed.exists():
        previous=json_file(failed)
        if previous.get('run_id')==run['id'] and previous.get('control_sha256')==control_hash:return
    request=request_artifact(run)
    blocked=BASE/'blocked.json'
    if blocked.exists():
        block=json_file(blocked)
        if (block.get('candidate_sha')==request['candidate_sha']
                and block.get('control_sha256')==control_hash):return
    try:
        completed=apply(request,run['id'])
    except Exception as error:
        common.atomic(failed,{'run_id':run['id'],'control_sha256':control_hash,'error_type':type(error).__name__})
        raise
    if completed:
        common.atomic(done,{'run_id':run['id']})
        failed.unlink(missing_ok=True)
