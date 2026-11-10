"""Receipt-bound Hermes cutover using the upstream installer, never auth restore."""
from contextlib import closing
import ctypes
import datetime as dt
import errno
import hashlib
import io
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import stat
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
VERSION_LINE=re.compile(r'^Hermes Agent v(?P<version>\d+\.\d+\.\d+(?:\+[^ ]+)?) .* · local (?P<sha>[0-9a-f]{12,40})$')
MAX_PUBLICATION_QUARANTINES=8


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


def file_identity(path):
    """Return a stable regular-file identity, including its exact bytes."""
    path=Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('Hermes file must be a regular file')
    before=path.stat();digest=common.digest(path);after=path.stat()
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_uid,
                         value.st_gid,value.st_mtime_ns,value.st_ctime_ns)
    if fields(before)!=fields(after):raise ValueError('Hermes file changed while fingerprinting')
    return {'sha256':digest,'device':after.st_dev,'inode':after.st_ino,'size':after.st_size,
            'mode':after.st_mode,'uid':after.st_uid,'gid':after.st_gid,
            'mtime_ns':after.st_mtime_ns,'ctime_ns':after.st_ctime_ns}


def entry_identity(path):
    """Fingerprint one stable directory entry without following symlinks."""
    path=Path(path);before=path.lstat()
    if stat.S_ISREG(before.st_mode):return file_identity(path)
    link_target=os.readlink(path) if stat.S_ISLNK(before.st_mode) else None
    after=path.lstat()
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_uid,
                         value.st_gid,value.st_mtime_ns,value.st_ctime_ns)
    if fields(before)!=fields(after):raise ValueError('Hermes entry changed while fingerprinting')
    identity:dict[str,object]={'device':after.st_dev,'inode':after.st_ino,'size':after.st_size,
                              'mode':after.st_mode,'uid':after.st_uid,'gid':after.st_gid,
                              'mtime_ns':after.st_mtime_ns,'ctime_ns':after.st_ctime_ns}
    if link_target is not None:identity['link_target']=link_target
    return identity


def exchanged_identity_matches(actual,expected):
    """Match an exchanged entry; renameat2 changes ctime but no owned data."""
    keys=set(actual)|set(expected)
    keys.discard('ctime_ns')
    return all(actual.get(key)==expected.get(key) for key in keys)


def fsync_directory(path):
    directory=os.open(path,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:os.fsync(directory)
    finally:os.close(directory)


def require_publication_slots(target,slots=1):
    """Refuse before disruption unless bounded quarantine has room."""
    target=Path(target)
    count=len(list(target.parent.glob('.'+target.name+'.quarantine.*')))
    if slots<1 or count+slots>MAX_PUBLICATION_QUARANTINES:
        raise ValueError('Hermes publication quarantine limit reached')


def preflight_publication_capacity(include_promotion):
    """Reserve rollback slots before services stop or installers run."""
    require_publication_slots(SHIM,2 if include_promotion else 1)
    require_publication_slots(UNITS/'hermes-dashboard.service',2 if include_promotion else 1)
    require_publication_slots(UNITS/'hermes-gateway.service',1)


def launcher_identity(path,trusted_digest):
    path=Path(path)
    if not os.access(path,os.X_OK):raise ValueError('Hermes launcher must be executable')
    before=file_identity(path)
    if before['sha256']!=trusted_digest:
        raise ValueError('Hermes launcher bytes are not independently trusted')
    output=command([str(path),'--version'],timeout=60)
    if file_identity(path)!=before:raise ValueError('Hermes launcher changed during identity check')
    if not isinstance(output,str):raise ValueError('Hermes launcher identity is unavailable')
    lines=output.splitlines()
    match=VERSION_LINE.fullmatch(lines[0] if lines else '')
    installs=[line.removeprefix('Install directory: ') for line in lines
              if line.startswith('Install directory: ')]
    if match is None or len(installs)!=1:
        raise ValueError('Hermes launcher identity is unavailable')
    return {'version':match.group('version'),'sha':match.group('sha'),
            'root':str(Path(installs[0]).resolve())}


def prove_launcher(path,expected_sha,trusted_digest,expected_root=None,expected_tag=None):
    identity=launcher_identity(path,trusted_digest)
    if len(identity['sha'])<12 or not expected_sha.startswith(identity['sha']):
        raise ValueError('Hermes launcher does not match expected checkout')
    if expected_root is not None and identity['root']!=str(Path(expected_root).resolve()):
        raise ValueError('Hermes launcher install root mismatch')
    if expected_tag is not None:
        version=expected_tag.removeprefix('v')
        if identity['version']!=version and not identity['version'].startswith(version+'+'):
            raise ValueError('Hermes launcher version mismatch')
    return identity


def rename_exchange(left,right):
    """Atomically exchange two Linux directory entries, or fail closed."""
    left=Path(left);right=Path(right)
    if left.parent!=right.parent:raise ValueError('Hermes exchange must stay in one directory')
    libc=ctypes.CDLL(None,use_errno=True)
    function=getattr(libc,'renameat2',None)
    if function is None:raise OSError(errno.ENOSYS,'renameat2 is required for Hermes publication')
    function.argtypes=[ctypes.c_int,ctypes.c_char_p,ctypes.c_int,ctypes.c_char_p,ctypes.c_uint]
    function.restype=ctypes.c_int
    directory=os.open(left.parent,os.O_RDONLY|getattr(os,'O_DIRECTORY',0))
    try:
        if function(directory,os.fsencode(left.name),directory,os.fsencode(right.name),2)!=0:
            error=ctypes.get_errno()
            raise OSError(error,os.strerror(error))
    finally:os.close(directory)


def atomic_publish_bytes(target,data,mode,expected_identity):
    """CAS-replace one owned file, retaining both entries on uncertainty."""
    target=Path(target);target.parent.mkdir(parents=True,exist_ok=True)
    if expected_identity is None:raise ValueError('Hermes publication requires an expected target identity')
    if target.is_symlink() or not target.is_file():
        raise ValueError('refusing to replace non-regular Hermes file')
    # There is no compare-and-unlink primitive, so old entries are retained as
    # evidence. Bound that retention before creating another file rather than
    # allowing retries or recurring upgrades to consume the filesystem.
    require_publication_slots(target)
    fd,name=tempfile.mkstemp(prefix='.'+target.name+'.quarantine.',dir=target.parent)
    temporary=Path(name)
    with os.fdopen(fd,'wb') as stream:
        stream.write(data);stream.flush();os.fchmod(stream.fileno(),mode);os.fsync(stream.fileno())
    candidate=entry_identity(temporary)
    try:
        rename_exchange(temporary,target)
    except BaseException:
        # A wrapper can fail after the kernel completed the exchange. Never
        # attempt a second exchange: pathname validation followed by rename is
        # itself racy and could move a foreign entry. Preserve both names and
        # best-effort persist whichever state the kernel reached.
        try:fsync_directory(target.parent)
        except OSError:pass
        raise
    try:
        displaced=entry_identity(temporary)
        if not exchanged_identity_matches(displaced,expected_identity):
            raise ValueError('refusing to replace changed Hermes file')
        if not exchanged_identity_matches(entry_identity(target),candidate):
            raise ValueError('Hermes publication verification failed')
        fsync_directory(target.parent)
    except BaseException:
        # Once the first exchange has happened no second pathname exchange can
        # be a CAS: a writer can replace either side between verification and
        # renameat2. Leave target and quarantine untouched for receipt-bound
        # recovery, and best-effort persist the evidence before failing closed.
        try:fsync_directory(target.parent)
        except OSError:pass
        raise
    # The displaced entry remains at the unique same-directory path as durable
    # quarantine. Linux has no compare-and-unlink primitive: even a verified
    # pathname can be replaced immediately before unlink. Retention therefore
    # avoids deleting either the displaced owner or a later foreign entry.


def publish_file(source,target,expected_identity,mode=None,expected_source_identity=None):
    source=Path(source)
    before=file_identity(source);data=source.read_bytes()
    if expected_source_identity is not None and before!=expected_source_identity:
        raise ValueError('Hermes rollback source changed')
    if (hashlib.sha256(data).hexdigest()!=before['sha256'] or file_identity(source)!=before):
        raise ValueError('Hermes publication source changed')
    atomic_publish_bytes(target,data,mode if mode is not None else source.stat().st_mode&0o777,
                         expected_identity)
    if common.digest(target)!=before['sha256']:raise ValueError('Hermes publication verification failed')
    return before['sha256']


def base_unit_exec(path):
    """Read the one simple no-space ExecStart that may be rewritten."""
    service=False;commands=[]
    for raw in Path(path).read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith(('#',';')):continue
        if line.startswith('[') and line.endswith(']'):
            service=line=='[Service]';continue
        if service and line.startswith('ExecStart='):
            value=line.removeprefix('ExecStart=')
            if not value or value.endswith('\\'):
                raise ValueError('Hermes unit has unsupported ExecStart semantics')
            argv=value.split(' ')
            if (any(not token or not re.fullmatch(r'[A-Za-z0-9_./:@%+=,-]+',token) for token in argv)
                    or not argv[0].startswith('/')):
                raise ValueError('Hermes unit has unsupported ExecStart syntax')
            commands.append(argv)
    if len(commands)!=1 or not commands[0]:raise ValueError('Hermes unit needs one ExecStart')
    return commands[0]


def effective_unit_exec(unit):
    """Parse systemd's effective command, including all drop-ins, fail closed."""
    raw=command(['systemctl','--user','show',unit,'-p','ExecStart','--value'])
    match=re.fullmatch(r'\{ path=(/[^ ;]+) ; argv\[\]=([^;]+?) ; ignore_errors=(?:yes|no) ; .+ \}',raw)
    if match is None:raise ValueError('Hermes effective ExecStart has unsupported syntax')
    path=match.group(1);argv_text=match.group(2)
    if path is None or argv_text is None:
        raise ValueError('Hermes effective ExecStart has unsupported metadata')
    argv=argv_text.split(' ')
    if (not argv or argv[0]!=path
            or any(not token or not re.fullmatch(r'[A-Za-z0-9_./:@%+=,-]+',token) for token in argv)):
        raise ValueError('Hermes effective ExecStart has unsupported argv')
    return argv


def validate_gateway_unit(root):
    expected=[str(Path(root)/'.hermes/bin/hermes'),'gateway','run']
    if effective_unit_exec('hermes-gateway.service')!=expected:
        raise ValueError('Hermes gateway unit does not run the candidate checkout')


def validate_dashboard_unit():
    argv=effective_unit_exec('hermes-dashboard.service')
    if len(argv)<2 or argv[:2]!=[str(SHIM),'dashboard']:
        raise ValueError('Hermes dashboard unit does not run the global candidate launcher')
    return argv


def align_dashboard_unit(expected_identity):
    path=UNITS/'hermes-dashboard.service';raw=path.read_text()
    lines=raw.splitlines(keepends=True)
    argv=base_unit_exec(path)
    if len(argv)<2 or argv[1]!='dashboard':
        raise ValueError('Hermes dashboard unit is not a dashboard command')
    service=False;indexes=[]
    for n,line in enumerate(lines):
        stripped=line.strip()
        if stripped.startswith('[') and stripped.endswith(']'):service=stripped=='[Service]'
        elif service and stripped.startswith('ExecStart='):indexes.append(n)
    if len(indexes)!=1:raise ValueError('Hermes dashboard unit needs one ExecStart')
    index=indexes[0];ending='\n' if lines[index].endswith('\n') else ''
    lines[index]='ExecStart='+' '.join([str(SHIM),*argv[1:]])+ending
    atomic_publish_bytes(path,''.join(lines).encode(),path.stat().st_mode&0o777,expected_identity)


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
    launcher=root/'.hermes/bin/hermes'
    if (not launcher.is_file() or receipt.get('launcher_sha256')!=common.digest(launcher)):
        raise ValueError('official candidate staging is incomplete')
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
              '[(m._connect().close()) for m,n in [(executions,"executions"),(incidents,"executions"),(notepad,"notepad")] if (p/"cron"/(n+".db")).exists()]; '
              'shared=Path(os.environ["CANARY_SHARED_DB"]); '
              'hosted_rooms._connect(shared).close() if shared.exists() else None')
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
        command(['env','HOME='+home,'HERMES_HOME='+home,
                 str(root/'.hermes/bin/hermes'),'--help'],root,timeout=60,log=log)


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
            'nenhuma nova tentativa de parar serviços será feita. O gateway pode estar parado '
            'se a falha ocorreu durante a troca/retomada; confira o monitor de saúde. Atualizações suspensas; '
            'é necessária intervenção para conferir a saúde do gateway e os bancos atuais.')
    except (OSError,subprocess.SubprocessError,ValueError):
        # Subsequent polls retry delivery, but never repeat service operations.
        pass
    return False


def installation():
    return {str(p):file_identity(p) for p in
            [SHIM,UNITS/'hermes-gateway.service',UNITS/'hermes-dashboard.service'] if p.is_file()}


def expected_installation_paths():
    return {str(SHIM),str(UNITS/'hermes-gateway.service'),str(UNITS/'hermes-dashboard.service')}


def trusted_existing_launcher(expected_sha):
    """Bind the installed shim to a tested receipt without executing old code."""
    receipt_path=BASE/'receipts'/expected_sha/'receipt.json'
    receipt=json_file(receipt_path)
    if (receipt.get('schema')!=1 or receipt.get('candidate_sha')!=expected_sha
            or receipt.get('result')!='passed'
            or receipt.get('runner')!='official-per-file-private-home'
            or receipt.get('tests_log_sha256')!=common.digest(receipt_path.parent/'tests.log')):
        raise ValueError('Hermes running receipt is not trusted')
    root=candidate_root(receipt.get('root',''))
    command(['systemctl','--user','daemon-reload'])
    argv=effective_unit_exec('hermes-gateway.service')
    if len(argv)!=3 or argv[1:]!=['gateway','run']:
        raise ValueError('Hermes gateway unit command is not owned')
    launcher=Path(argv[0])
    if launcher!=root/'.hermes/bin/hermes':
        raise ValueError('Hermes gateway launcher path is not canonical')
    if command(['git','-C',str(root),'rev-parse','HEAD'])!=expected_sha:
        raise ValueError('Hermes gateway checkout does not match running metadata')
    if command(['git','-C',str(root),'status','--porcelain']):
        raise ValueError('Hermes gateway checkout is not clean')
    if common.digest(root/'scripts/install.sh')!=receipt.get('installer_sha256'):
        raise ValueError('Hermes running checkout installer changed')
    gateway_identity=file_identity(launcher);shim_identity=file_identity(SHIM)
    if gateway_identity['sha256']!=receipt.get('launcher_sha256'):
        raise ValueError('Hermes running launcher changed after trusted tests')
    if gateway_identity['sha256']!=shim_identity['sha256']:
        raise ValueError('Hermes global launcher does not match the trusted running checkout')
    return shim_identity


def aligned_candidate_installation(root):
    candidate=Path(root)/'.hermes/bin/hermes';candidate_identity=file_identity(candidate)
    installed=installation()
    if set(installed)!=expected_installation_paths():
        raise ValueError('Hermes installation fingerprint is incomplete')
    if installed[str(SHIM)]['sha256']!=candidate_identity['sha256']:
        raise ValueError('Hermes installation is partial: global launcher is not the candidate')
    command(['systemctl','--user','daemon-reload'])
    validate_gateway_unit(root);validate_dashboard_unit()
    return installed


def config_fingerprints():
    return {str(p):common.digest(p) for p in state_files() if p.name!='auth.json'}


def other_transactions():
    return any((common.BASE/name).exists() for name in
               ['pending.json','client-pending.json','indexa-auth-pending.json'])


def candidate_healthy(tx):
    # Only new, receipt-bound journals permit automatic completion/resumption.
    # Legacy journals cannot prove which installation is still on disk.
    try:
        if not tx.get('installed') or installation()!=tx['installed']:return False
        validate_local(tx['request'])
        if config_fingerprints()!=tx['config_hashes']:return False
        current=json_file(HOME/'gateway_state.json')
        pid=int(command(['systemctl','--user','show','hermes-gateway.service','-p','MainPID','--value']))
        age=time.time()-dt.datetime.fromisoformat(current['updated_at']).timestamp()
        telegram=[v for k,v in current.get('platforms',{}).items()
                  if k=='telegram' or k.endswith(':telegram')]
        return (active('hermes-gateway.service') and pid>1 and current.get('pid')==pid
                and -30<=age<=180 and current.get('gateway_state')=='running'
                and current.get('code_sha')==tx['candidate_sha'] and telegram
                and all(v.get('state')=='connected' for v in telegram)
                and (not tx['units'].get('hermes-dashboard.service') or active('hermes-dashboard.service')))
    except (OSError,ValueError,KeyError,TypeError,subprocess.SubprocessError):return False


def recovery_barrier():
    """Resolve/fence Hermes BEFORE any other lane can stop the gateway."""
    pending=BASE/'pending.json'
    if (BASE/'intervention.json').exists():
        return recover() if pending.exists() else False
    if not pending.exists():return True
    # Concurrent journals are an operator decision, never an ordering guess.
    if other_transactions():
        return intervention(json_file(pending),'concurrent-transactions')
    return recover()


def recover():
    journal=BASE/'pending.json'
    if not journal.exists():return True
    tx=json_file(journal)
    marker=BASE/'intervention.json'
    if marker.exists():
        return intervention(tx,json_file(marker)['reason'])
    if tx.get('rollback_attempted'):
        return intervention(tx,'rollback-incomplete')
    # Schema compatibility must be established BEFORE any stop, including when
    # reconciling legacy journals written by the old controller.
    try: compatible=live_schemas()==tx['schemas']
    except (OSError,sqlite3.Error,ValueError):
        return intervention(tx,'schema-unreadable')
    if not compatible:return intervention(tx,'schema-incompatible')
    if candidate_healthy(tx):
        try:resume(tx['units'])
        except (OSError,subprocess.SubprocessError,ValueError):
            return intervention(tx,'candidate-consumer-resume-failed')
        common.atomic(BASE/'last-success.json',tx)
        journal.unlink()
        return True
    # A rollback may only replace the exact installation journaled at the last
    # durable phase boundary. Legacy, partial, or externally changed targets
    # are preserved and fenced before any service is stopped.
    try:
        if not tx.get('installed') or installation()!=tx['installed']:
            return intervention(tx,'rollback-installation-mismatch')
    except (OSError,ValueError):
        return intervention(tx,'rollback-installation-unreadable')
    # A full quarantine is a deterministic preflight gate, never an outage
    # discovered only after stopping otherwise healthy consumers.
    try:preflight_publication_capacity(False)
    except (OSError,ValueError):
        return intervention(tx,'rollback-quarantine-capacity')
    # Arm durably before the first stop. A crash/failed restart cannot repeat it.
    installed_before=installation()
    tx['rollback_attempted']=True
    common.atomic(journal,tx)
    try:
        for unit in CONSUMERS:ctl('stop',unit)
    except (OSError,subprocess.SubprocessError,ValueError):
        return intervention(tx,'rollback-stop-incomplete')
    # Close the check/stop race; never restore old code over newly migrated data.
    try: compatible=live_schemas()==tx['schemas']
    except (OSError,sqlite3.Error,ValueError):
        return intervention(tx,'schema-unreadable-after-stop')
    if not compatible:
        # No old files were restored. Restart only a receipt-bound unchanged
        # candidate installation; legacy or foreign files require an operator.
        try:
            if (tx.get('installed')==installed_before==installation()
                    and config_fingerprints()==tx['config_hashes']):
                validate_local(tx['request'])
                resume(tx['units'])
                healthy(tx['units'],tx['candidate_sha'])
        except (OSError,subprocess.SubprocessError,ValueError,KeyError):
            return intervention(tx,'schema-changed-candidate-resume-failed')
        return intervention(tx,'schema-changed-during-stop')
    backup=Path(tx['backup'])
    try:
        for name in ['hermes-gateway.service','hermes-dashboard.service']:
            path=UNITS/name
            source=backup/name
            if source.exists():publish_file(source,path,tx['installed'][str(path)],
                expected_source_identity=tx['backup_sources'][str(source)])
        source=backup/'hermes-shim'
        publish_file(source,SHIM,tx['installed'][str(SHIM)],mode=0o755,
                     expected_source_identity=tx['backup_sources'][str(source)])
        command(['systemctl','--user','daemon-reload'])
        resume(tx['units']);healthy(tx['units'],tx['previous_sha'])
    except (OSError,subprocess.SubprocessError,ValueError):
        return intervention(tx,'rollback-unhealthy')
    common.atomic(BASE/'last-rollback.json',tx);journal.unlink()
    # A rollback blocks this exact candidate under the control code that
    # observed the failure.  A reviewed control release may intentionally
    # retry it after fixing (or proving transient) the gate; unchanged code
    # still suppresses every timer poll.
    common.atomic(BASE/'blocked.json',{
        'candidate_sha':tx['candidate_sha'],
        'control_sha256':common.digest(Path(__file__)),
    })
    try:
        common.notify('hermes-rollback-'+tx['candidate_sha'],'Hermes: atualização revertida para a instalação anterior. Preservei sessões, filas, configuração e autenticação atuais; candidato bloqueado até correção.')
    except (OSError,subprocess.SubprocessError,ValueError):pass
    return True


def apply(request,run_id):
    if other_transactions():return False
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
        candidate=root/'.hermes/bin/hermes';candidate_digest=file_identity(candidate)['sha256']
        prove_launcher(candidate,request['candidate_sha'],candidate_digest,root,request['tag'])
        aligned_candidate_installation(root)
        common.atomic(BASE/'last-success.json',{'run_id':run_id,'candidate_sha':request['candidate_sha'],'result':'already_current'})
        return True
    previous_sha=require_sha(state['code_sha'])
    # The controller may take over only the launcher belonging to the running
    # checkout according to independent unit, git, path and digest evidence. It
    # is deliberately not executed while establishing ownership.
    trusted_shim=trusted_existing_launcher(previous_sha)
    installed_before=installation()
    expected_installation=expected_installation_paths()
    if set(installed_before)!=expected_installation:
        raise ValueError('Hermes installation fingerprint is incomplete')
    if installed_before[str(SHIM)]!=trusted_shim:
        raise ValueError('Hermes launcher changed after ownership proof')
    # Reserve promotion plus possible rollback slots before writing a journal,
    # stopping services, or invoking any installer stage.
    preflight_publication_capacity(True)
    backup=destination/'backup';backup.mkdir(mode=0o700,exist_ok=True)
    backup_sources={}
    for name in ['hermes-gateway.service','hermes-dashboard.service']:
        source=UNITS/name;target=backup/name
        if source.is_file():
            shutil.copyfile(source,target);backup_sources[str(target)]=file_identity(target)
            if backup_sources[str(target)]['sha256']!=installed_before[str(source)]['sha256']:
                raise ValueError('Hermes backup source copy changed')
    target=backup/'hermes-shim';shutil.copyfile(SHIM,target)
    backup_sources[str(target)]=file_identity(target)
    if backup_sources[str(target)]['sha256']!=installed_before[str(SHIM)]['sha256']:
        raise ValueError('Hermes backup source copy changed')
    units={u:active(u) for u in CONSUMERS}
    if other_transactions():return False
    tx={'candidate_sha':request['candidate_sha'],'previous_sha':previous_sha,
        'backup':str(backup),'units':units,'schemas':schemas,'run_id':run_id,
        'request':request,'config_hashes':config_hashes,'phase':'stopping','started_at':time.time(),
        'maintenance_deadline':time.time()+2220,'installed':installed_before,
        'backup_sources':backup_sources}
    common.atomic(BASE/'pending.json',tx)
    try:
        for unit in common.WATCH_TIMERS:ctl('stop',unit)
        if any(common.busy_service(u) for u in common.WATCH_SERVICES):
            resume(units);(BASE/'pending.json').unlink();return False
        if installation()!=tx['installed']:
            raise ValueError('Hermes installation changed before gateway stop')
        try:ctl('stop','hermes-gateway.service')
        except subprocess.CalledProcessError:
            if active('hermes-gateway.service'):
                resume(units);(BASE/'pending.json').unlink();return False
            raise
        ctl('stop','hermes-dashboard.service')
        tx['schemas']=snapshot_archive(backup/'sqlite-backup.zip')
        tx['sqlite_archive_sha256']=common.digest(backup/'sqlite-backup.zip')
        common.atomic(BASE/'pending.json',tx)
        tx['phase']='installing';common.atomic(BASE/'pending.json',tx)
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
        # Re-bind the handoff to the receipt and exact full git SHA after every
        # installer stage, not merely to the launcher's abbreviated banner.
        validate_local(request)
        candidate=root/'.hermes/bin/hermes'
        candidate_digest=file_identity(candidate)['sha256']
        prove_launcher(candidate,request['candidate_sha'],candidate_digest,root,request['tag'])
        publish_file(candidate,SHIM,installed_before[str(SHIM)],mode=0o755)
        # Prove the bytes through the public path before any consumer can start.
        prove_launcher(SHIM,request['candidate_sha'],candidate_digest,root,request['tag'])
        align_dashboard_unit(installed_before[str(UNITS/'hermes-dashboard.service')])
        command(['systemctl','--user','daemon-reload'])
        validate_gateway_unit(root);validate_dashboard_unit()
        tx['installed']=aligned_candidate_installation(root)
        tx['phase']='starting';common.atomic(BASE/'pending.json',tx)
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
