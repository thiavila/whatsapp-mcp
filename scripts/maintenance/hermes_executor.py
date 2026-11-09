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
HOSTED_ROOM_CAPACITY_SCHEMA=('table','hosted_room_policy_capacity','hosted_room_policy_capacity',
    'CREATE TABLE hosted_room_policy_capacity (\n        singleton INTEGER PRIMARY KEY CHECK(singleton=1), max_active_events INTEGER NOT NULL)')
OLD_EXECUTION_STATUSES="('claimed','running','completed','failed','unknown')"
NEW_EXECUTION_STATUSES="('claimed','running','completed','failed','unknown','cancelled','missed')"


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


def active_state_database(path):
    path=Path(path)
    return path==HOME/'state.db' or (path.name=='state.db' and path.parent.parent==HOME/'profiles')


def active_execution_database(path):
    path=Path(path)
    return (path==HOME/'cron/executions.db' or
            (path.name=='executions.db' and path.parent.name=='cron'
             and path.parent.parent.parent==HOME/'profiles'))


def schema_description(path):
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as connection:
        if connection.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:
            raise ValueError('database integrity check failed')
        rows=connection.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name").fetchall()
        version=connection.execute('PRAGMA user_version').fetchone()[0]
        metadata=[]
        for table in [r[1] for r in rows if r[0]=='table' and r[1].lower() in
                      {'whatsmeow_version','schema_version','schema_migrations','alembic_version','db_version'}]:
            quoted='"'+table.replace('"','""')+'"'
            metadata.append((table,connection.execute(f'SELECT * FROM {quoted} LIMIT 1000').fetchall()))
    return rows,version,metadata


def expected_forward_description(path,before):
    rows,version,metadata=before
    if active_state_database(path):
        if metadata!=[('schema_version',[(32,)])]:
            raise ValueError('forward state schema_version is not exactly 32')
        if any(row[1]=='hosted_room_policy_capacity' for row in rows):
            raise ValueError('forward state schema already contained capacity table')
        return sorted([*rows,HOSTED_ROOM_CAPACITY_SCHEMA],key=lambda row:(row[0],row[1])),version,metadata
    if active_execution_database(path):
        matches=[i for i,row in enumerate(rows) if row[:3]==('table','executions','executions')]
        if len(matches)!=1:raise ValueError('forward executions table absent')
        index=matches[0];sql=rows[index][3]
        if not isinstance(sql,str) or sql.count(OLD_EXECUTION_STATUSES)!=1 or NEW_EXECUTION_STATUSES in sql:
            raise ValueError('forward executions status constraint unexpected')
        changed=list(rows);changed[index]=(*rows[index][:3],sql.replace(OLD_EXECUTION_STATUSES,NEW_EXECUTION_STATUSES))
        return changed,version,metadata
    raise ValueError('database is not eligible for forward reconciliation')


def forward_schema_state(tx):
    current=live_schemas()
    if set(current)!=set(tx['schemas']):raise ValueError('forward database set changed')
    selected=[Path(path) for path in current if active_state_database(path) or active_execution_database(path)]
    if not selected or not any(active_state_database(p) for p in selected) or not any(active_execution_database(p) for p in selected):
        raise ValueError('forward database selection incomplete')
    for path,digest in current.items():
        if Path(path) not in selected and digest!=tx['schemas'][path]:
            raise ValueError('unapproved forward schema drift')
    backup=Path(tx['backup'])
    expected_backup=(BASE/'runs'/str(tx['run_id'])/'backup').resolve()
    if backup.resolve()!=expected_backup:raise ValueError('forward backup path outside transaction')
    archive=backup/'sqlite-backup.zip'
    if common.digest(archive)!=tx.get('sqlite_archive_sha256'):raise ValueError('forward backup digest changed')
    with tempfile.TemporaryDirectory(prefix='forward-schema-',dir=backup.parent) as folder,zipfile.ZipFile(archive) as saved:
        if saved.testzip() is not None or len(saved.namelist())!=len(set(saved.namelist())):
            raise ValueError('forward backup archive invalid')
        members=set(saved.namelist())
        for number,path in enumerate(selected):
            member=str(path.relative_to(HOME))
            if member not in members:raise ValueError('forward database missing from backup')
            original=Path(folder)/f'{number}.db';original.write_bytes(saved.read(member))
            before=schema_description(original);after=schema_description(path)
            if active_state_database(path) and after[2]!=[('schema_version',[(32,)])]:
                raise ValueError('forward state schema_version is not exactly 32')
            if after!=expected_forward_description(path,before):
                raise ValueError('unapproved forward schema delta: '+member)
            if current[str(path)]==tx['schemas'][str(path)]:
                raise ValueError('expected forward schema delta absent: '+member)
    return current


def canary(root,copies,schemas,log):
    # Initializers run only against copied state.db files with a private HOME.
    paths=active_session_copies(copies,schemas)
    with tempfile.TemporaryDirectory(prefix='hermes-home-') as home:
        code='import json,sys; from pathlib import Path; from hermes_state import SessionDB; '+\
             '[SessionDB(Path(p)).close() for p in json.loads(sys.argv[1])]'
        command(['bash',str(root/'scripts/run-in-hermes-env'),'env','HOME='+home,'HERMES_HOME='+home,
                 'python3','-c',code,json.dumps(paths)],root,timeout=180,log=log)
        for path in schemas:
            if common.db_schema(copies/Path(path).relative_to(HOME))!=schemas[path]:
                raise ValueError('Hermes schema migration: automatic promotion blocked for '+str(Path(path).relative_to(HOME)))
        command([str(root/'.hermes/bin/hermes'),'--help'],root,timeout=60,log=log)


def resume(states):
    for unit in ['hermes-gateway.service','hermes-dashboard.service',*common.WATCH_TIMERS,*common.WATCH_SERVICES]:
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


def stop_consumers():
    failures=[]
    for unit in CONSUMERS:
        try:ctl('stop',unit)
        except Exception as error:failures.append((unit,error))
    if failures:
        raise ValueError('failed to stop Hermes consumers: '+','.join(unit for unit,_ in failures)) from failures[0][1]


def durable_unlink(path):
    path=Path(path);path.unlink()
    descriptor=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:os.fsync(descriptor)
    finally:os.close(descriptor)


def live_forward_candidate(tx):
    candidate=require_sha(tx['candidate_sha'])
    if not tx.get('units',{}).get('hermes-gateway.service'):
        raise ValueError('forward transaction did not originate with an active gateway')
    state=json_file(HOME/'gateway_state.json')
    if (not active('hermes-gateway.service') or state.get('gateway_state')!='running'
            or state.get('code_sha')!=candidate
            or state.get('platforms',{}).get('telegram',{}).get('state')!='connected'):
        raise ValueError('forward candidate gateway is not live and connected')
    return candidate


def pending_request(tx):
    candidate=require_sha(tx['candidate_sha']);run_id=int(tx['run_id'])
    run=common.api(f'repos/{REPOSITORY}/actions/runs/{run_id}')
    if (run.get('id')!=run_id or run.get('path')!=WORKFLOW
            or run.get('repository',{}).get('full_name')!=REPOSITORY
            or run.get('head_repository',{}).get('full_name')!=REPOSITORY
            or run.get('head_branch')!='main' or run.get('event')!='workflow_dispatch'
            or run.get('status')!='completed' or run.get('conclusion')!='success'):
        raise ValueError('pending Hermes request provenance')
    request=request_artifact(run)
    if request['action']!='promote' or request['candidate_sha']!=candidate:
        raise ValueError('pending Hermes request changed')
    return request,validate_local(request)


def install_candidate(request,root,destination):
    branch=command(['git','-C',str(root),'branch','--show-current'])
    if not branch:raise ValueError('candidate needs a named local branch')
    if validate_local(request)!=root:raise ValueError('candidate root changed')
    for stage in ['products','complete']:
        command(['bash',str(root/'scripts/install.sh'),'--dir',str(root),'--stage',stage,
                 '--non-interactive','--branch',branch,'--commit',request['candidate_sha']],
                root,timeout=900,log=destination/'installer.log')
    command([str(root/'.hermes/bin/hermes'),'gateway','install','--force','--no-start-now','--start-on-login'],
            root,timeout=120,log=destination/'gateway-install.log')


def reconcile_forward(tx,live_candidate):
    request,root=pending_request(tx)
    if request['candidate_sha']!=live_candidate:raise ValueError('forward candidate evidence changed')
    expected=forward_schema_state(tx)
    config_hashes={str(p):common.digest(p) for p in state_files() if p.name!='auth.json'}
    destination=BASE/'runs'/str(tx['run_id'])
    if live_schemas()!=expected:raise ValueError('forward schemas changed before reconciliation')
    install_candidate(request,root,destination)
    if any(common.digest(Path(path))!=digest for path,digest in config_hashes.items()):
        raise ValueError('material config migration requires explicit decision')
    try:
        command(['systemctl','--user','daemon-reload']);resume(tx['units'])
        healthy(tx['units'],request['candidate_sha'])
        if live_schemas()!=expected:raise ValueError('forward schemas changed during reconciliation')
    except BaseException:
        try:stop_consumers()
        except BaseException:pass
        raise
    result=dict(tx,result='forward_reconciled')
    common.atomic(BASE/'last-success.json',result);durable_unlink(BASE/'pending.json')
    try:
        common.notify('hermes-forward-'+request['candidate_sha'],
            'Hermes: instalação candidata reconciliada pelo instalador oficial; esquemas esperados e saúde conferidos, sem restaurar autenticação.')
    except BaseException:pass


def recover():
    journal=BASE/'pending.json'
    if not journal.exists():return
    tx=json_file(journal)
    if live_schemas()!=tx['schemas']:
        evidence=None;failure=None
        try:evidence=live_forward_candidate(tx)
        except BaseException as error:failure=error
        try:stop_consumers()
        except BaseException as error:failure=error
        if failure is None:
            try:return reconcile_forward(tx,evidence)
            except BaseException as error:failure=error
        try:
            common.notify('hermes-unsafe-rollback-'+tx['candidate_sha'],'Hermes: rollback automático bloqueado por mudança no esquema de dados. Preservei o estado atual e tentei parar todos os consumidores para evitar corrupção. Preciso de intervenção; nenhum token/banco antigo foi restaurado.')
        except BaseException as error:
            if failure is None:failure=error
        raise ValueError('Hermes rollback requires compatible current schemas') from failure
    stop_consumers()
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
        install_candidate(request,root,destination)
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
    BASE.mkdir(mode=0o700,parents=True,exist_ok=True);recover()
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
