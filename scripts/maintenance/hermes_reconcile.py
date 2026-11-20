"""Explicit receipt-bound repair for one interrupted Hermes partial cutover.

This controller is intentionally not reachable from the polling path.  An
operator must supply the hashes and transaction coordinates printed by the
private incident record.  All discovery is read-only until preflight has
proved the complete repair plan.
"""
from contextlib import closing
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
import sys
import tarfile
import tempfile
import time

import executor as common
import hermes_executor as hermes
from control import require_sha

EXPECTED_PROFILES=(
    'melancia-lismeri','melancia-manager','melancia-roberto','revella-carol','whats-monica',
)
INCIDENT_RUN_ID=42478485171
INCIDENT_CANDIDATE_SHA='8352c5764c6bbef2ad39d99df533da91c74bf953'
INCIDENT_UPSTREAM_SHA='f0d94882e89ab8d4098b5bb2f166adfb9ee0a0ab'
INCIDENT_TAG='v0.22.6'
INCIDENT_RECEIPT_SHA256='80c4ef654a6358c093759d57ea08e0dd54b8a7ddab3b44279cc61d9dea51276d'
INCIDENT_PENDING_SHA256='3d94d0ab0aeecfa4e8b9e310df51ec690508a95c95a22a312594afa15221a4f3'
INCIDENT_INTERVENTION_SHA256='43c5650f5b8c07699b0ea28a0f77aea02f223cf49fab95e4b2ec1ed2c7a5c652'
INCIDENT_SQLITE_ARCHIVE_SHA256='ebe901f7d47df5579c96b82a70ea6ee60f2d1d5350f7acdbfd4ae5f7920c6595'
INCIDENT_CANDIDATE_ROOT='/home/thiago/worktrees/hermes-stable-20261106'
INCIDENT_PREVIOUS_SHA='82faffd425455211f47e9906c85053ad1e74481a'
INCIDENT_CONTROL_SHA='2f337f6b5e97f72a2ee0f43a731b8d7344ccc9ff'
INCIDENT_WORKFLOW_ID=406286011
INCIDENT_CHECK_SUITE_ID=115069238920
INCIDENT_PYTHON_TARGET='linux-x64'
INCIDENT_PYTHON_RELEASE='20261021'
INCIDENT_PYTHON_VERSION='3.14.9+'+INCIDENT_PYTHON_RELEASE
INCIDENT_PYTHON_ARTIFACT_URL=('https://github.com/astral-sh/python-build-standalone/releases/download/'
                              +INCIDENT_PYTHON_RELEASE+'/cpython-'+INCIDENT_PYTHON_VERSION
                              +'-x86_64-unknown-linux-gnu-install_only.tar.gz')
INCIDENT_PYTHON_ARTIFACT_SHA256='dfa4c5a40c47f338f41658017be550c72247c391e6bfb1fa4da03ebc8451abdf'
INCIDENT_PYTHON_TREE_DIGEST='7bbc7426f297f1a14c27d94514f92fab081b02896e9218c8666e6a6af497765a'
INCIDENT_INSTALL_STAMP_SHA256='2c157982ffaed5f74bb00ec18b685ce791458897bb69ae7c877c161fe00bc5e3'
INCIDENT_ENVIRONMENT_TREE_DIGEST='1a4641bb4da43a8c902e381c43ee58ad0bdb87467399c712383ba5263fd6903c'
DEPLOY_TIMER='mcp-maintenance-deploy.timer'
JOURNAL='partial-cutover-reconcile.json'
LAST='last-partial-cutover-reconcile.json'
ARCHIVE='partial-cutover-marker-archive'
MAX_ARCHIVED_MARKERS=8
ALLOWED_ADDITIONS={
    'table:cron_incidents',
    'index:idx_cron_incidents_job',
    'index:idx_cron_incidents_state',
    'index:sqlite_autoindex_cron_incidents_1',
}
TOMBSTONE_SCHEMA=1
LEGACY_PENDING_KEYS={'backup','candidate_sha','previous_sha','run_id','schemas',
                     'sqlite_archive_sha256','units'}
LEGACY_RECEIPT_KEYS={'schema','candidate_sha','upstream_sha','tag','root','tests',
                     'tests_log_sha256','result','runner','installer_sha256'}
RECEIPT_IDENTITY_KEYS={'sha256','device','inode','size','mode','uid','gid','mtime_ns','ctime_ns'}
COMPLETION_KEYS={'schema','spec','candidate_root','source_candidate_identity','runtime_generation',
                 'runtime_generation_digest','dependency_closure','candidate_identity','shim_identity',
                 'request','receipt_identity',
                 'external_evidence','pending_identity','intervention_identity',
                 'sqlite_archive_identity','backup_sources','database_before','database_content_before',
                 'reconciled_schemas',
                 'installed_before','config_hashes','telegram_adapters','gateway_pid','archive_plan',
                 'units','phase','migrated_profiles','aligned','archived','started_at'}
TERMINAL_ACTIVE_STATES={'active','inactive','failed'}
PHASES=('prepared','stopping','stopped','migrating','migrated','aligning','aligned',
        'starting','verified','archiving','archived','resuming-timer','timer-started',
        'last-published','complete')


def marker_digest(path):
    """Read one stable regular directory entry without following replacements."""
    path=Path(path)
    try:entry_before=path.lstat()
    except OSError as error:raise ValueError('reconciliation marker must be a regular file') from error
    if not stat.S_ISREG(entry_before.st_mode):
        raise ValueError('reconciliation marker must be a regular file')
    flags=(os.O_RDONLY|getattr(os,'O_CLOEXEC',0)|getattr(os,'O_NOFOLLOW',0)
           |getattr(os,'O_NONBLOCK',0))
    try:fd=os.open(path,flags)
    except (FileNotFoundError,OSError) as error:
        raise ValueError('reconciliation marker must be a regular file') from error
    try:
        before=os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            raise ValueError('reconciliation marker must be a regular file')
        if (before.st_dev,before.st_ino)!=(entry_before.st_dev,entry_before.st_ino):
            raise ValueError('reconciliation marker changed while opening')
        chunks=[]
        while True:
            chunk=os.read(fd,65536)
            if not chunk:break
            chunks.append(chunk)
        raw=b''.join(chunks);after=os.fstat(fd)
    finally:os.close(fd)
    try:entry=path.lstat()
    except FileNotFoundError:raise ValueError('reconciliation marker changed while reading') from None
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_uid,
                         value.st_gid,value.st_mtime_ns,value.st_ctime_ns)
    if fields(entry_before)!=fields(before) or fields(before)!=fields(after) or fields(after)!=fields(entry):
        raise ValueError('reconciliation marker changed while reading')
    identity={'sha256':hashlib.sha256(raw).hexdigest(),'device':after.st_dev,'inode':after.st_ino,
              'size':after.st_size,'mode':after.st_mode,'uid':after.st_uid,'gid':after.st_gid,
              'mtime_ns':after.st_mtime_ns,'ctime_ns':after.st_ctime_ns}
    return identity,raw


def unit_state(unit):
    raw=hermes.command(['systemctl','--user','show',unit,'-p','ActiveState','-p','UnitFileState'])
    if not isinstance(raw,str):raise ValueError('unit state unavailable for '+unit)
    values=dict(line.split('=',1) for line in raw.splitlines() if '=' in line)
    active=values.get('ActiveState');unit_file=values.get('UnitFileState')
    if set(values)!={'ActiveState','UnitFileState'} or not active or not unit_file:
        raise ValueError('unsupported unit state for '+unit)
    return {'active_state':active,'unit_file_state':unit_file}


def safely_offline(state):
    """Only systemd's terminal no-process states admit a protected write."""
    return state['active_state'] in {'inactive','failed'}


def validate_preflight_unit_states(units):
    transitional=[unit for unit,state in units.items()
                  if state['active_state'] not in TERMINAL_ACTIVE_STATES]
    if transitional:
        raise ValueError('transitional systemd ActiveState before reconciliation: '+','.join(transitional))


def db_inventory(path):
    # mode=ro deliberately includes a live WAL; immutable=1 could silently
    # inspect an older main file while the gateway is running.
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
        if db.execute('PRAGMA quick_check').fetchall()!=[('ok',)]:raise ValueError('cron database quick_check failed')
        objects={kind+':'+name:[table,sql] for kind,name,table,sql in
                 db.execute("SELECT type,name,tbl_name,sql FROM sqlite_master ORDER BY type,name")}
        tables=[key.split(':',1)[1] for key in objects
                if key.startswith('table:') and not key.startswith('table:sqlite_')]
        counts={name:db.execute('SELECT count(*) FROM "'+name.replace('"','""')+'"').fetchone()[0]
                for name in tables}
        incidents=(db.execute('SELECT count(*) FROM cron_incidents').fetchone()[0]
                   if 'table:cron_incidents' in objects else None)
    return {'objects':objects,'counts':counts,'incidents':incidents}


def db_content_digest(path,tables):
    """Hash existing logical rows without persisting their potentially sensitive contents."""
    path=Path(path);before=path.lstat()
    if not stat.S_ISREG(before.st_mode) or stat.S_IMODE(before.st_mode)&0o077:
        raise ValueError('cron database must be a private regular file')
    digest=hashlib.sha256()
    with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as db:
        data_version=db.execute('PRAGMA data_version').fetchone()[0]
        db.execute('BEGIN')
        for table in sorted(tables):
            quoted='"'+table.replace('"','""')+'"';digest.update(table.encode()+b'\0')
            columns=len(db.execute('PRAGMA table_info('+quoted+')').fetchall())
            if columns<1:raise ValueError('cron database table has no columns')
            order=','.join(str(index) for index in range(1,columns+1))
            cursor=db.execute('SELECT * FROM '+quoted+' ORDER BY '+order)
            for row in cursor:
                digest.update(b'R')
                for value in row:
                    if value is None:data=b'';kind=b'n'
                    elif isinstance(value,bytes):data=value;kind=b'b'
                    elif isinstance(value,str):data=value.encode();kind=b's'
                    elif isinstance(value,int):data=str(value).encode();kind=b'i'
                    elif isinstance(value,float):data=value.hex().encode();kind=b'f'
                    else:raise ValueError('unsupported SQLite value type')
                    digest.update(kind+len(data).to_bytes(8,'big')+data)
        db.execute('COMMIT')
        if db.execute('PRAGMA data_version').fetchone()[0]!=data_version:
            raise ValueError('cron database changed while hashing')
    after=path.lstat()
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_uid,
                         value.st_gid,value.st_mtime_ns,value.st_ctime_ns)
    if fields(before)!=fields(after):raise ValueError('cron database entry changed while hashing')
    return digest.hexdigest()


def expected_databases():
    return {name:hermes.HOME/'profiles'/name/'cron/executions.db' for name in EXPECTED_PROFILES}


def validate_schema_delta(before,after):
    if set(before['objects'])-set(after['objects']):raise ValueError('cron migration removed schema objects')
    additions=set(after['objects'])-set(before['objects'])
    if additions!=ALLOWED_ADDITIONS:raise ValueError('cron migration schema delta is not the reviewed incident migration')
    if any(after['objects'][key]!=value for key,value in before['objects'].items()):
        raise ValueError('cron migration changed an existing schema object')
    if any(after['counts'].get(name)!=count for name,count in before['counts'].items()):
        raise ValueError('cron migration changed existing row counts')
    if after['incidents']!=0:raise ValueError('cron incidents must be empty after initialization')
    for key in ALLOWED_ADDITIONS:
        table,sql=after['objects'][key]
        if table!='cron_incidents':raise ValueError('cron incident index targets unexpected table')
        if key.startswith('index:sqlite_autoindex_') and sql is not None:
            raise ValueError('unexpected SQLite autoindex definition')


def validate_spec(spec):
    required={'pending_sha256','intervention_sha256','candidate_sha','run_id','backup'}
    if set(spec)!=required:raise ValueError('reconciliation arguments are incomplete')
    require_sha(spec['candidate_sha'])
    for name in ['pending_sha256','intervention_sha256']:
        if not isinstance(spec[name],str) or not re.fullmatch('[0-9a-f]{64}',spec[name]):raise ValueError(name)
    if type(spec['run_id']) is not int or spec['run_id']<=0:raise ValueError('run_id')
    if not isinstance(spec['backup'],str) or not Path(spec['backup']).is_absolute():raise ValueError('backup')
    expected_backup=str((hermes.BASE/'runs'/str(INCIDENT_RUN_ID)/'backup').resolve())
    if (spec['run_id']!=INCIDENT_RUN_ID or spec['candidate_sha']!=INCIDENT_CANDIDATE_SHA
            or spec['pending_sha256']!=INCIDENT_PENDING_SHA256
            or spec['intervention_sha256']!=INCIDENT_INTERVENTION_SHA256
            or str(Path(spec['backup']).resolve())!=expected_backup):
        raise ValueError('reconciliation is pinned to the retained Hermes incident')


def private_regular_bytes(path,label,private=True):
    path=Path(path)
    try:before=path.lstat()
    except FileNotFoundError:raise ValueError(label+' is missing') from None
    if not stat.S_ISREG(before.st_mode) or (private and stat.S_IMODE(before.st_mode)&0o077):
        raise ValueError(label+' must be a '+('private ' if private else '')+'regular file')
    flags=os.O_RDONLY|getattr(os,'O_NOFOLLOW',0);descriptor=os.open(path,flags)
    try:
        opened=os.fstat(descriptor)
        if (opened.st_dev,opened.st_ino)!=(before.st_dev,before.st_ino):
            raise ValueError(label+' changed while opening')
        chunks=[]
        while True:
            chunk=os.read(descriptor,1024*1024)
            if not chunk:break
            chunks.append(chunk)
        after_fd=os.fstat(descriptor)
    finally:os.close(descriptor)
    after=path.lstat()
    fields=lambda value:(value.st_dev,value.st_ino,value.st_size,value.st_mode,value.st_uid,
                         value.st_gid,value.st_mtime_ns,value.st_ctime_ns)
    if fields(before)!=fields(opened) or fields(opened)!=fields(after_fd) or fields(after_fd)!=fields(after):
        raise ValueError(label+' changed while reading')
    raw=b''.join(chunks)
    identity={'sha256':hashlib.sha256(raw).hexdigest(),'device':after_fd.st_dev,
              'inode':after_fd.st_ino,'size':after_fd.st_size,'mode':after_fd.st_mode,
              'uid':after_fd.st_uid,'gid':after_fd.st_gid,'mtime_ns':after_fd.st_mtime_ns,
              'ctime_ns':after_fd.st_ctime_ns}
    return identity,raw


def private_regular(path,label):return private_regular_bytes(path,label)[0]


def external_request(run_id,expected):
    """Bind the local legacy receipt to its immutable published workflow artifact."""
    workflow=common.api(f'repos/{common.REPOSITORY}/actions/workflows/hermes-maintenance.yml')
    run=common.api(f'repos/{common.REPOSITORY}/actions/runs/{run_id}')
    if (run_id!=INCIDENT_RUN_ID or workflow.get('id')!=INCIDENT_WORKFLOW_ID
            or workflow.get('path')!=hermes.WORKFLOW or run.get('id')!=run_id
            or run.get('path')!=hermes.WORKFLOW or run.get('workflow_id')!=workflow.get('id')
            or run.get('repository',{}).get('full_name')!=common.REPOSITORY
            or run.get('head_repository',{}).get('full_name')!=common.REPOSITORY
            or run.get('head_branch')!='main' or run.get('event')!='workflow_dispatch'
            or run.get('status')!='completed' or run.get('conclusion')!='success'
            or run.get('check_suite_id')!=INCIDENT_CHECK_SUITE_ID
            or run.get('head_sha')!=INCIDENT_CONTROL_SHA):
        raise ValueError('legacy Hermes workflow provenance mismatch')
    published=hermes.request_artifact(run)
    if published!=expected:
        raise ValueError('published Hermes request does not match the retained receipt')
    return {'run_id':run_id,'workflow_id':workflow['id'],'check_suite_id':run.get('check_suite_id'),
            'control_sha':require_sha(run['head_sha']),'request':published}


def legacy_request(candidate_sha,run_id):
    """Reconstruct a request and bind it to the published immutable artifact."""
    receipt_path=hermes.BASE/'receipts'/candidate_sha/'receipt.json'
    receipt_identity,raw=private_regular_bytes(receipt_path,'candidate receipt')
    receipt=json.loads(raw)
    if set(receipt)!=LEGACY_RECEIPT_KEYS or receipt.get('schema')!=1:
        raise ValueError('candidate receipt is not the reviewed legacy schema')
    if (receipt.get('candidate_sha')!=candidate_sha or receipt.get('result')!='passed'
            or receipt.get('runner')!='official-per-file-private-home'):
        raise ValueError('candidate receipt provenance mismatch')
    if (candidate_sha!=INCIDENT_CANDIDATE_SHA or run_id!=INCIDENT_RUN_ID
            or receipt_identity['sha256']!=INCIDENT_RECEIPT_SHA256
            or receipt.get('upstream_sha')!=INCIDENT_UPSTREAM_SHA
            or receipt.get('tag')!=INCIDENT_TAG):
        raise ValueError('candidate receipt is not the retained Hermes incident')
    tests=receipt_path.parent/'tests.log';tests_identity,_=private_regular_bytes(tests,'candidate tests log')
    if tests_identity['sha256']!=receipt['tests_log_sha256']:
        raise ValueError('candidate tests log changed')
    request={'schema':1,'component':'hermes','candidate_sha':candidate_sha,
             'upstream_sha':receipt['upstream_sha'],'tag':receipt['tag'],
             'receipt_sha256':receipt_identity['sha256'],'action':'promote'}
    from hermes_control import validate_request
    validate_request(request)
    root=Path(receipt['root']).resolve()
    if str(root)!=INCIDENT_CANDIDATE_ROOT:
        raise ValueError('candidate root is not the retained Hermes incident root')
    if hermes.candidate_root(receipt['root'])!=root:
        raise ValueError('candidate root changed')
    if hermes.command(['git','-C',str(root),'rev-parse','HEAD'])!=candidate_sha:
        raise ValueError('candidate HEAD changed')
    if hermes.command(['git','-C',str(root),'status','--porcelain','--untracked-files=all']):
        raise ValueError('candidate dirty')
    hermes.command(['git','-C',str(root),'merge-base','--is-ancestor',receipt['upstream_sha'],candidate_sha])
    if common.digest(root/'scripts/install.sh')!=receipt['installer_sha256']:
        raise ValueError('installer changed')
    launcher=root/'.hermes/bin/hermes'
    launcher_identity,runtime_proof=official_launcher_identity(root,launcher)
    evidence=external_request(run_id,request)
    if (hermes.file_identity(launcher)!=launcher_identity
            or official_python_closure(root)!=runtime_proof):
        raise ValueError('candidate launcher changed while validating external evidence')
    return request,root,receipt_identity,evidence


def immutable_python_plan(root):
    """Read the incident Python pin only from the fixed candidate Git object."""
    try:
        raw=subprocess.run(['git','-C',str(root),'show',INCIDENT_CANDIDATE_SHA+':pm/lock.json'],
                           check=True,capture_output=True,timeout=60).stdout
        lock=json.loads(raw)
        package=lock['packages']['python'];artifact=package['artifacts'][INCIDENT_PYTHON_TARGET]
    except (OSError,subprocess.SubprocessError,KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('immutable official Python lock is unavailable') from None
    expected={'version':INCIDENT_PYTHON_VERSION,
              'artifact':{'url':INCIDENT_PYTHON_ARTIFACT_URL,
                          'sha256':INCIDENT_PYTHON_ARTIFACT_SHA256}}
    if (lock.get('schema')!=1 or package.get('version')!=expected['version']
            or artifact!=expected['artifact']):
        raise ValueError('immutable official Python pin mismatch')
    entry='python-'+expected['version']+'-'+INCIDENT_PYTHON_TARGET
    return entry,expected


def validate_runtime_tree(root):
    """Reject anything except directories, files and contained live symlinks."""
    root=Path(root)
    try:
        root_stat=root.lstat();resolved_root=root.resolve(strict=True)
    except OSError:raise ValueError('official Python runtime closure is unavailable') from None
    if not stat.S_ISDIR(root_stat.st_mode) or root.is_symlink():
        raise ValueError('official Python runtime root is not a directory')
    stack=[root]
    try:
        while stack:
            directory=stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    mode=entry.stat(follow_symlinks=False).st_mode;path=Path(entry.path)
                    if stat.S_ISDIR(mode):stack.append(path)
                    elif stat.S_ISREG(mode):pass
                    elif stat.S_ISLNK(mode):
                        target=(path.parent/os.readlink(path)).resolve(strict=True)
                        if not target.is_relative_to(resolved_root):
                            raise ValueError('official Python runtime symlink escapes its closure')
                    else:raise ValueError('official Python runtime contains a special file')
    except ValueError:raise
    except OSError:raise ValueError('official Python runtime closure is unsafe or incomplete') from None


def official_tree_digest(source,runtime):
    """Run tree_digest from the immutable candidate source, not local controller code."""
    code=("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
          "from pm.store import tree_digest; print(tree_digest(Path(sys.argv[2])))")
    env={'HOME':str(Path(source).parent/'home'),'PATH':os.environ.get('PATH','/usr/bin:/bin'),
         'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
    try:
        result=subprocess.run([sys.executable,'-I','-c',code,str(source),str(runtime)],
                              check=True,capture_output=True,text=True,timeout=180,env=env).stdout.strip()
    except (OSError,subprocess.SubprocessError):
        raise ValueError('official Python runtime digest failed') from None
    if not re.fullmatch('[0-9a-f]{64}',result):raise ValueError('official Python runtime digest is invalid')
    return result


def official_dependency_selection(candidate_root,source):
    """Resolve the committed venv with the pinned, stdlib-only PM contract."""
    code=("import json,sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
          "from pm.environments import install_key,install_state_dir,runtime_facts_path,"
          "selected_venv,site_packages; r=Path(sys.argv[2]).resolve(); v=selected_venv(r); "
          "print(json.dumps({'install_key':install_key(r),'state':str(install_state_dir(r)),"
          "'facts':str(runtime_facts_path(r)),'environment':str(v),"
          "'site_packages':str(site_packages(v))}))")
    env={'HOME':str(hermes.HOME.parent),'PATH':os.environ.get('PATH','/usr/bin:/bin'),
         'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
    try:
        raw=subprocess.run([sys.executable,'-I','-c',code,str(source),str(candidate_root)],
                           check=True,capture_output=True,text=True,timeout=60,env=env).stdout
        value=json.loads(raw)
    except (OSError,subprocess.SubprocessError,json.JSONDecodeError):
        raise ValueError('official dependency selection is unavailable') from None
    if not isinstance(value,dict) or set(value)!={'install_key','state','facts','environment','site_packages'}:
        raise ValueError('official dependency selection is invalid')
    return value


def validate_dependency_tree(root,python_runtimes):
    """Admit only a complete venv whose links target itself or a verified Python."""
    root=Path(root);python_runtimes=[Path(value) for value in python_runtimes]
    try:allowed=(root.resolve(strict=True),*(value.resolve(strict=True) for value in python_runtimes))
    except OSError:raise ValueError('selected dependency closure is unavailable') from None
    stack=[root]
    try:
        while stack:
            directory=stack.pop()
            with os.scandir(directory) as entries:
                for entry in entries:
                    path=Path(entry.path);mode=entry.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):stack.append(path)
                    elif stat.S_ISREG(mode):pass
                    elif stat.S_ISLNK(mode):
                        target=(path.parent/os.readlink(path)).resolve(strict=True)
                        if not any(target.is_relative_to(parent) for parent in allowed):
                            raise ValueError('selected dependency symlink escapes its closure')
                    else:raise ValueError('selected dependency closure contains a special file')
    except ValueError:raise
    except OSError:raise ValueError('selected dependency closure is unsafe or incomplete') from None


def dependency_closure_plan(candidate_root,source,python_proof):
    """Bind facts, activation inputs, resolved lock and selected venv to Git bytes."""
    candidate_root=Path(candidate_root);selection=official_dependency_selection(candidate_root,source)
    state=Path(selection['state']);facts=Path(selection['facts']);environment=Path(selection['environment'])
    site=Path(selection['site_packages']);key=selection['install_key']
    expected_state=hermes.HOME/'installs'/key;generations=expected_state/'environments'
    if (not re.fullmatch('[0-9a-f]{16}',str(key)) or state.resolve()!=expected_state.resolve()
            or facts.resolve()!=expected_state.joinpath('facts.json').resolve()
            or not environment.resolve().is_relative_to(generations.resolve())
            or environment.name!='venv' or not (environment/'pyvenv.cfg').is_file()
            or not site.is_dir() or not site.resolve().is_relative_to(environment.resolve())):
        raise ValueError('official dependency selection is outside the candidate install')
    facts_identity,facts_raw=marker_digest(facts)
    try:data=json.loads(facts_raw);fact=data['packages']['venv']
    except (KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('official dependency facts are invalid') from None
    required={'environment','resolved_lock','stamp','extras'}
    if (data.get('schema')!=1 or set(fact)!=required or fact['environment']!=str(environment.resolve())
            or not isinstance(fact['stamp'],str) or not re.fullmatch('[0-9a-f]{64}',fact['stamp'])
            or not isinstance(fact['extras'],list) or any(not isinstance(x,str) for x in fact['extras'])):
        raise ValueError('official dependency facts are invalid')
    resolved=Path(fact['resolved_lock']);generation=environment.parent
    if (resolved.resolve()!=generation.joinpath('workspace/uv.lock').resolve()
            or not resolved.is_file() or not generation.resolve().is_relative_to(generations.resolve())):
        raise ValueError('resolved dependency lock is outside the selected generation')
    resolved_identity,_=marker_digest(resolved)
    inputs={};stamps=expected_state/'inputs'
    try:
        if (stamps/'.project-root').read_text().strip()!=str(candidate_root.resolve()):raise ValueError
        for name in ('uv.lock','pyproject.toml','pm/lock.json'):
            pinned=Path(source)/name;live=candidate_root/name;stamp=stamps/name
            if (not pinned.is_file() or not live.is_file() or not stamp.is_file()
                    or live.stat().st_mtime_ns!=stamp.stat().st_mtime_ns
                    or pinned.read_bytes()!=live.read_bytes()):raise ValueError
            inputs[name]={'sha256':hashlib.sha256(pinned.read_bytes()).hexdigest(),
                          'mtime_ns':stamp.stat().st_mtime_ns}
    except (OSError,ValueError):raise ValueError('dependency activation inputs are not pinned') from None
    if resolved_identity['sha256']!=inputs['uv.lock']['sha256']:
        raise ValueError('resolved dependency lock differs from the pinned candidate lock')
    candidate_python=candidate_root/'.hermes/tools'/python_proof['entry']
    selected_python=hermes.HOME/'tools'/python_proof['entry']
    validate_runtime_tree(selected_python)
    selected_python_digest=official_tree_digest(source,selected_python)
    if selected_python_digest!=python_proof['tree_digest']:
        raise ValueError('selected dependency interpreter differs from the pinned Python')
    try:venv_python=(environment/'bin/python').resolve(strict=True)
    except OSError:raise ValueError('selected dependency interpreter is unavailable') from None
    if not any(venv_python.is_relative_to(value.resolve()) for value in [candidate_python,selected_python]):
        raise ValueError('selected dependency interpreter is outside the verified stores')
    validate_dependency_tree(environment,[candidate_python,selected_python])
    digest=official_tree_digest(source,environment)
    if digest!=INCIDENT_ENVIRONMENT_TREE_DIGEST:
        raise ValueError('selected dependency environment differs from the retained incident')
    validate_dependency_tree(environment,[candidate_python,selected_python])
    if (official_tree_digest(source,environment)!=digest
            or official_tree_digest(source,selected_python)!=selected_python_digest):
        raise ValueError('selected dependency closure changed while hashing')
    install_stamp_identity,_=private_regular_bytes(candidate_root/'install-stamp.json',
                                                   'candidate install identity stamp',private=False)
    if install_stamp_identity['sha256']!=INCIDENT_INSTALL_STAMP_SHA256:
        raise ValueError('candidate install identity stamp differs from the retained incident')
    return {'schema':1,'install_key':key,'facts_identity':facts_identity,
            'environment':str(environment.resolve()),'environment_digest':digest,
            'resolved_lock_identity':resolved_identity,'activation_inputs':inputs,
            'extras':fact['extras'],'stamp':fact['stamp'],'site_packages':str(site.resolve()),
            'python':python_proof,'selected_python':str(selected_python.resolve()),
            'install_stamp_identity':install_stamp_identity}


def payload_install_key(source,root):
    code=("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
          "from pm.environments import install_key; print(install_key(Path(sys.argv[2])))")
    result=subprocess.run([sys.executable,'-I','-c',code,str(source),str(root)],check=True,
                          capture_output=True,text=True,timeout=60).stdout.strip()
    if not re.fullmatch('[0-9a-f]{16}',result):raise ValueError('payload install key is invalid')
    return result


def reconciliation_homes():
    return {hermes.HOME,*[Path(path).parents[1] for path in expected_databases().values()]}


def assert_no_payload_install_state(root,homes,key=None):
    key=key or payload_install_key(root,root)
    for home in {Path(value).resolve() for value in homes}:
        if os.path.lexists(home/'installs'/key):
            raise ValueError('protected runtime generation created install state')
    return key


def official_python_closure(root,source=None):
    """Bind the installed interpreter closure to the immutable incident pin."""
    if source is None:
        try:
            archived=subprocess.run(['git','-C',str(root),'archive','--format=tar',
                                     INCIDENT_CANDIDATE_SHA,'pm'],check=True,capture_output=True,
                                    timeout=60).stdout
        except (OSError,subprocess.SubprocessError):
            raise ValueError('immutable official Python digest source is unavailable') from None
        with tempfile.TemporaryDirectory(prefix='hermes-runtime-proof-') as folder:
            private=Path(folder);source=private/'source';source.mkdir(mode=0o700)
            try:
                with tarfile.open(fileobj=io.BytesIO(archived),mode='r:') as archive:
                    members=archive.getmembers()
                    if any(Path(member.name).is_absolute() or '..' in Path(member.name).parts
                           or not (member.isdir() or member.isfile()) for member in members):
                        raise ValueError('immutable official Python digest archive is unsafe')
                    archive.extractall(source,filter='data')
            except (OSError,tarfile.TarError):
                raise ValueError('immutable official Python digest source is unavailable') from None
            return official_python_closure(root,source)
    entry,_=immutable_python_plan(root);runtime=Path(root)/'.hermes/tools'/entry
    validate_runtime_tree(runtime)
    digest=official_tree_digest(source,runtime)
    validate_runtime_tree(runtime)
    second=official_tree_digest(source,runtime)
    if digest!=INCIDENT_PYTHON_TREE_DIGEST or second!=digest:
        raise ValueError('official Python runtime closure digest mismatch')
    python=runtime/'bin/python3'
    try:resolved=python.resolve(strict=True);mode=resolved.lstat().st_mode
    except OSError:raise ValueError('official launcher interpreter is unavailable') from None
    if not resolved.is_relative_to(runtime.resolve(strict=True)) or not stat.S_ISREG(mode) or not os.access(resolved,os.X_OK):
        raise ValueError('official launcher interpreter is unavailable')
    return {'entry':entry,'artifact_sha256':INCIDENT_PYTHON_ARTIFACT_SHA256,
            'tree_digest':digest,'python':str(python)}


def official_launcher_identity(root,launcher):
    """Re-mint the launcher from the pinned commit's official generator.

    The generator is loaded from a private extraction of the immutable Git
    object, never from candidate working-tree Python and never from the
    candidate launcher.  Its interpreter is selected by the fixed commit's
    lockfile and its complete installed closure is checked independently.
    """
    root=Path(root);launcher=Path(launcher)
    identity=hermes.file_identity(launcher)
    try:
        archived=subprocess.run(
            ['git','-C',str(root),'archive','--format=tar',INCIDENT_CANDIDATE_SHA,
             'hermes_cli','pm','hermes_constants.py'],check=True,capture_output=True,timeout=60).stdout
    except (OSError,subprocess.SubprocessError):
        raise ValueError('immutable official launcher source is unavailable') from None
    with tempfile.TemporaryDirectory(prefix='hermes-launcher-proof-') as folder:
        private=Path(folder);source=private/'source';out=private/'out'
        source.mkdir(mode=0o700);out.mkdir(mode=0o700)
        try:
            with tarfile.open(fileobj=io.BytesIO(archived),mode='r:') as archive:
                members=archive.getmembers()
                for member in members:
                    target=Path(member.name)
                    if (target.is_absolute() or '..' in target.parts
                            or not (member.isdir() or member.isfile())):
                        raise ValueError('immutable official launcher archive is unsafe')
                archive.extractall(source,filter='data')
        except (OSError,tarfile.TarError):
            raise ValueError('immutable official launcher source is unavailable') from None
        runtime_proof=official_python_closure(root,source)
        python=Path(runtime_proof['python'])
        code=("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
              "from hermes_cli._launchers import mint_launcher; "
              "p=mint_launcher('hermes',Path(sys.argv[2]),Path(sys.argv[3]),Path(sys.argv[4]),None); "
              "raise SystemExit(0 if p is not None else 1)")
        env={'HOME':str(private/'home'),'PATH':os.environ.get('PATH','/usr/bin:/bin'),
             'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
        try:
            subprocess.run([sys.executable,'-I','-c',code,str(source),str(root),str(out),str(python)],
                           check=True,capture_output=True,timeout=60,env=env)
        except (OSError,subprocess.SubprocessError):
            raise ValueError('official launcher generation failed') from None
        expected=out/'hermes'
        if not expected.is_file() or expected.read_bytes()!=launcher.read_bytes():
            raise ValueError('candidate launcher differs from immutable official install logic')
    if hermes.file_identity(launcher)!=identity:
        raise ValueError('candidate launcher changed during official generation')
    return identity,runtime_proof


def runtime_generation_path():
    return hermes.BASE/'partial-cutover-runtime-generations'/INCIDENT_CANDIDATE_SHA


def generation_digest(root):
    """Hash every entry in a stable, non-writable runtime generation."""
    root=Path(root);digest=hashlib.sha256();stack=[root]
    while stack:
        directory=stack.pop()
        with os.scandir(directory) as entries:
            for entry in sorted(entries,key=lambda value:value.name):
                path=Path(entry.path);relative=path.relative_to(root).as_posix()
                before=entry.stat(follow_symlinks=False);mode=before.st_mode
                digest.update(relative.encode()+b'\0'+str(stat.S_IMODE(mode)).encode()+b'\0')
                if stat.S_ISDIR(mode):
                    digest.update(b'd\0');stack.append(path)
                elif stat.S_ISREG(mode):
                    identity,raw=marker_digest(path)
                    if identity['device']!=before.st_dev or identity['inode']!=before.st_ino:
                        raise ValueError('runtime generation changed while hashing')
                    digest.update(b'f\0'+hashlib.sha256(raw).digest())
                elif stat.S_ISLNK(mode):
                    digest.update(b'l\0'+os.fsencode(os.readlink(path)))
                else:raise ValueError('runtime generation contains a special file')
    return digest.hexdigest()


def validate_runtime_generation(root,expected_digest=None):
    """Require a private pathname closure that cannot change during execution.

    The maintenance executor owns the enclosing 0500 directory.  Within the
    generation every directory and regular file is non-writable, and every
    symlink resolves inside the generation.  Thus normal candidate/worktree
    writers cannot replace a checked path between verification and import.
    """
    root=Path(root);parent=root.parent
    try:parent_entry=parent.lstat();root_entry=root.lstat();resolved=root.resolve(strict=True)
    except OSError:raise ValueError('protected runtime generation is unavailable') from None
    if (parent.is_symlink() or root.is_symlink() or not stat.S_ISDIR(parent_entry.st_mode)
            or not stat.S_ISDIR(root_entry.st_mode) or stat.S_IMODE(parent_entry.st_mode)!=0o500):
        raise ValueError('runtime generation container is not protected')
    stack=[root]
    try:
        while stack:
            directory=stack.pop();entry=directory.lstat()
            if not stat.S_ISDIR(entry.st_mode) or stat.S_IMODE(entry.st_mode)&0o222:
                raise ValueError('runtime generation directory is writable')
            with os.scandir(directory) as entries:
                for child in entries:
                    path=Path(child.path);mode=child.stat(follow_symlinks=False).st_mode
                    if stat.S_ISDIR(mode):stack.append(path)
                    elif stat.S_ISREG(mode):
                        if stat.S_IMODE(mode)&0o222:
                            raise ValueError('runtime generation file is writable')
                    elif stat.S_ISLNK(mode):
                        if not (path.parent/os.readlink(path)).resolve(strict=True).is_relative_to(resolved):
                            raise ValueError('runtime generation symlink escapes its closure')
                    else:raise ValueError('runtime generation contains a special file')
    except ValueError:raise
    except OSError:raise ValueError('runtime generation is unsafe or incomplete') from None
    actual=generation_digest(root)
    if expected_digest is not None and actual!=expected_digest:
        raise ValueError('runtime generation digest mismatch')
    return actual


def lock_runtime_generation(root):
    """Remove write bits bottom-up, then seal the incident-only parent."""
    root=Path(root)
    for directory,dirs,files in os.walk(root,topdown=False,followlinks=False):
        for name in files:
            path=Path(directory)/name
            if not path.is_symlink():path.chmod(0o500 if path.stat().st_mode&0o111 else 0o400)
        for name in dirs:
            path=Path(directory)/name
            if not path.is_symlink():path.chmod(0o500)
        Path(directory).chmod(0o500)
    root.parent.chmod(0o500)


def run_official_assembler(source,out,python,site):
    """Publish the payload contract with the assembler from the pinned Git object."""
    scratch=out/'.payload-assembler';scratch.mkdir()
    marker={'python':Path(os.path.relpath(python,scratch)).as_posix(),
            'sitePackages':Path(os.path.relpath(site,scratch)).as_posix()}
    (scratch/'pm-runtime.json').write_text(json.dumps(marker))
    inputs=out/'.payload-inputs.json'
    inputs.write_text(json.dumps({'project':str(out/'pyproject.toml'),'code':str(out),'repo':'.',
                                  'placement':'contained','target':INCIDENT_PYTHON_TARGET,
                                  'python':str(python),'site_packages':str(site),
                                  'environment':str(out/'venv'),'pm_runtime':str(scratch),
                                  'tools':str(out/'tools'),'ref':INCIDENT_CANDIDATE_SHA}))
    code=("import sys; sys.path.insert(0,sys.argv[1]); from scripts.build.agent import main; "
          "sys.argv=['agent.py','--inputs',sys.argv[2],'--out',sys.argv[3]]; main()")
    try:subprocess.run([sys.executable,'-I','-c',code,str(source),str(inputs),str(out)],
                       check=True,capture_output=True,timeout=180)
    except (OSError,subprocess.SubprocessError):
        raise ValueError('official payload assembly failed') from None
    finally:
        inputs.unlink(missing_ok=True);shutil.rmtree(scratch,ignore_errors=True)
    try:manifest=json.loads((out/'manifest.json').read_text())
    except (OSError,json.JSONDecodeError):raise ValueError('official payload manifest is unavailable') from None
    relative_site=site.relative_to(out).as_posix();relative_python=python.relative_to(out).as_posix()
    expected={'schema':1,'target':INCIDENT_PYTHON_TARGET,'repo':'.','venv':'venv','store':'tools',
              'launchers':['hermes','hermes-agent','hermes-acp'],'runtime':{
                  'repoDir':'.','toolsDir':'tools','storePython':relative_python,
                  'sitePackages':relative_site,
                  'commands':{'hermes':'bin/hermes','hermes-agent':'bin/hermes-agent',
                              'hermes-acp':'bin/hermes-acp'}},'ref':INCIDENT_CANDIDATE_SHA}
    if manifest!=expected:raise ValueError('official payload manifest differs from the incident contract')
    return manifest


def mint_generation_shim(source,generation,python,site):
    """Mint a copy-safe absolute launcher with the pinned payload generator."""
    code=("import sys; from pathlib import Path; sys.path.insert(0,sys.argv[1]); "
          "from scripts.build.launchers import write_launchers; "
          "write_launchers(Path(sys.argv[2]),{'hermes':'hermes_cli.main:main'},"
          "python=sys.argv[3],repo=sys.argv[4],site=sys.argv[5],target=sys.argv[6])")
    with tempfile.TemporaryDirectory(prefix='hermes-generation-shim-') as folder:
        try:subprocess.run([sys.executable,'-I','-c',code,str(source),folder,str(python),
                            str(generation),str(site),INCIDENT_PYTHON_TARGET],check=True,
                           capture_output=True,timeout=60)
        except (OSError,subprocess.SubprocessError):
            raise ValueError('protected runtime shim generation failed') from None
        return (Path(folder)/'bin/hermes').read_bytes()


def prepare_runtime_generation(candidate_root):
    """Create one sealed payload from Git, verified Python and the committed venv."""
    candidate_root=Path(candidate_root);container=runtime_generation_path().parent
    if os.path.lexists(container):
        raise ValueError('protected runtime generation already exists before journaling')
    container.mkdir(mode=0o700,parents=False)
    temporary=Path(tempfile.mkdtemp(prefix='.generation-',dir=container));final=runtime_generation_path()
    try:
        try:archived=subprocess.run(['git','-C',str(candidate_root),'archive','--format=tar',
                                     INCIDENT_CANDIDATE_SHA],check=True,capture_output=True,
                                    timeout=180).stdout
        except (OSError,subprocess.SubprocessError):
            raise ValueError('immutable candidate source archive is unavailable') from None
        try:
            with tarfile.open(fileobj=io.BytesIO(archived),mode='r:') as archive:
                members=archive.getmembers()
                if any(Path(member.name).is_absolute() or '..' in Path(member.name).parts
                       or not (member.isdir() or member.isfile()) for member in members):
                    raise ValueError('immutable candidate source archive is unsafe')
                archive.extractall(temporary,filter='data')
        except (OSError,tarfile.TarError):
            raise ValueError('immutable candidate source archive is unavailable') from None
        python_proof=official_python_closure(candidate_root,temporary)
        dependency=dependency_closure_plan(candidate_root,temporary,python_proof)
        stamp_identity,stamp_raw=private_regular_bytes(candidate_root/'install-stamp.json',
                                                       'candidate install identity stamp',private=False)
        if stamp_identity!=dependency['install_stamp_identity']:
            raise ValueError('candidate install identity stamp changed before sealing')
        stamp_target=temporary/'install-stamp.json'
        descriptor=os.open(stamp_target,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o644)
        try:
            view=memoryview(stamp_raw)
            while view:view=view[os.write(descriptor,view):]
            os.fsync(descriptor)
        finally:os.close(descriptor)
        if common.digest(temporary/'install-stamp.json')!=INCIDENT_INSTALL_STAMP_SHA256:
            raise ValueError('copied candidate install identity stamp mismatch')
        source_runtime=candidate_root/'.hermes/tools'/python_proof['entry']
        target_runtime=temporary/'tools'/python_proof['entry'];target_runtime.parent.mkdir(parents=True,exist_ok=True)
        shutil.copytree(source_runtime,target_runtime,symlinks=True)
        if official_tree_digest(temporary,target_runtime)!=python_proof['tree_digest']:
            raise ValueError('copied official Python runtime closure digest mismatch')
        source_environment=Path(dependency['environment']);target_environment=temporary/'venv'
        shutil.copytree(source_environment,target_environment,symlinks=True)
        source_site=Path(dependency['site_packages'])
        target_site=target_environment/source_site.relative_to(source_environment)
        target_python=target_runtime/'bin/python3'
        run_official_assembler(temporary,temporary,target_python,target_site)
        dependency['payload_environment_digest']=official_tree_digest(temporary,target_environment)
        dependency['payload_python_digest']=official_tree_digest(temporary,target_runtime)
        dependency['manifest_sha256']=common.digest(temporary/'manifest.json')
        dependency['payload_install_key']=payload_install_key(temporary,final)
        assert_no_payload_install_state(final,reconciliation_homes(),dependency['payload_install_key'])
        if (official_tree_digest(temporary,source_environment)!=dependency['environment_digest']
                or official_tree_digest(temporary,source_runtime)!=python_proof['tree_digest']):
            raise ValueError('dependency source closure changed during payload assembly')
        shim=temporary/'.hermes/bin/hermes';shim.parent.mkdir(parents=True,exist_ok=True)
        final_python=final/target_python.relative_to(temporary);final_site=final/target_site.relative_to(temporary)
        shim.write_bytes(mint_generation_shim(temporary,final,final_python,final_site));shim.chmod(0o755)
        os.rename(temporary,final);hermes.fsync_directory(container)
        assert_no_payload_install_state(final,reconciliation_homes(),dependency['payload_install_key'])
        lock_runtime_generation(final);hermes.fsync_directory(container.parent)
        digest=validate_runtime_generation(final)
        candidate_identity=hermes.file_identity(final/'bin/hermes')
        shim_identity=hermes.file_identity(final/'.hermes/bin/hermes')
        hermes.prove_launcher(final/'bin/hermes',INCIDENT_CANDIDATE_SHA,
                              candidate_identity['sha256'],final,INCIDENT_TAG,sealed_payload=True)
        hermes.prove_launcher(final/'.hermes/bin/hermes',INCIDENT_CANDIDATE_SHA,
                              shim_identity['sha256'],final,INCIDENT_TAG,sealed_payload=True)
        if validate_runtime_generation(final,digest)!=digest:
            raise ValueError('protected runtime generation changed during launcher proof')
        return final,digest,candidate_identity,shim_identity,dependency
    except BaseException:
        # Never relax a sealed container to clean it up: uncertain generations
        # remain evidence and block a retry until an operator inspects them.
        if temporary.exists() and os.access(container,os.W_OK):shutil.rmtree(temporary)
        raise


def validate_legacy_marker_payloads(pending,intervention,spec):
    if set(pending)!=LEGACY_PENDING_KEYS:
        raise ValueError('pending marker is not the exact reviewed legacy schema')
    if set(intervention)!={'candidate_sha','reason','run_id'}:
        raise ValueError('intervention marker schema mismatch')
    if (pending.get('candidate_sha')!=spec['candidate_sha'] or pending.get('run_id')!=spec['run_id']
            or intervention.get('candidate_sha')!=spec['candidate_sha'] or intervention.get('run_id')!=spec['run_id']):
        raise ValueError('marker transaction coordinates mismatch')
    if pending.get('previous_sha')!=INCIDENT_PREVIOUS_SHA:
        raise ValueError('pending marker previous revision mismatch')
    if intervention.get('reason')!='schema-incompatible':
        raise ValueError('markers do not describe the reviewed partial-cutover incident')


def validate_legacy_archive(backup,pending):
    archive=Path(backup)/'sqlite-backup.zip'
    identity=private_regular(archive,'legacy SQLite archive')
    expected=pending.get('sqlite_archive_sha256')
    if (not isinstance(expected,str) or not re.fullmatch('[0-9a-f]{64}',expected)
            or expected!=INCIDENT_SQLITE_ARCHIVE_SHA256 or expected!=identity['sha256']):
        raise ValueError('legacy SQLite archive hash mismatch')
    return identity


def preflight(spec):
    """Build the plan; only the final protected generation is written pre-journal."""
    validate_spec(spec)
    if os.path.lexists(hermes.BASE/JOURNAL):raise ValueError('reconciliation journal already exists; use resume')
    pending_path=hermes.BASE/'pending.json';intervention_path=hermes.BASE/'intervention.json'
    pending_identity,pending_raw=marker_digest(pending_path)
    intervention_identity,intervention_raw=marker_digest(intervention_path)
    if (stat.S_IMODE(pending_identity['mode'])!=0o600
            or stat.S_IMODE(intervention_identity['mode'])!=0o600):
        raise ValueError('reconciliation markers must be private regular files')
    if hashlib.sha256(pending_raw).hexdigest()!=spec['pending_sha256']:raise ValueError('pending receipt hash mismatch')
    if hashlib.sha256(intervention_raw).hexdigest()!=spec['intervention_sha256']:raise ValueError('intervention receipt hash mismatch')
    pending=json.loads(pending_raw);intervention=json.loads(intervention_raw)
    validate_legacy_marker_payloads(pending,intervention,spec)
    backup_entry=Path(spec['backup'])
    try:backup_mode=backup_entry.lstat().st_mode
    except FileNotFoundError:raise ValueError('backup coordinate mismatch') from None
    backup=backup_entry.resolve()
    if (Path(pending.get('backup','')).resolve()!=backup or not stat.S_ISDIR(backup_mode)
            or stat.S_IMODE(backup_mode)&0o077):
        raise ValueError('backup coordinate mismatch')
    expected_backup=(hermes.BASE/'runs'/str(spec['run_id'])/'backup').resolve()
    if backup!=expected_backup:raise ValueError('backup is outside the receipt-bound run')
    archive_identity=validate_legacy_archive(backup,pending)
    request,root,receipt_identity,external_evidence=legacy_request(spec['candidate_sha'],spec['run_id'])
    candidate=root/'.hermes/bin/hermes';source_candidate_identity=hermes.file_identity(candidate)
    hermes.prove_launcher(candidate,spec['candidate_sha'],source_candidate_identity['sha256'],root,request['tag'])
    state=hermes.json_file(hermes.HOME/'gateway_state.json')
    adapter_names=sorted(name for name in state.get('platforms',{})
                         if name=='telegram' or name.endswith(':telegram'))
    telegram=[state['platforms'][name] for name in adapter_names]
    pid_text=hermes.command(['systemctl','--user','show','hermes-gateway.service','-p','MainPID','--value'])
    if not isinstance(pid_text,str) or not pid_text.isdecimal():raise ValueError('gateway MainPID unavailable')
    pid=int(pid_text)
    try:age=time.time()-__import__('datetime').datetime.fromisoformat(state['updated_at']).timestamp()
    except (KeyError,TypeError,ValueError):raise ValueError('gateway state freshness is invalid') from None
    if (state.get('code_sha')!=spec['candidate_sha'] or state.get('gateway_state')!='running'
            or not telegram or any(value.get('state')!='connected' for value in telegram)
            or not -30<=age<=180 or pid<=1 or state.get('pid')!=pid):
        raise ValueError('running candidate or Telegram adapters are not healthy')
    if not hermes.active('hermes-gateway.service') or not hermes.active('hermes-dashboard.service'):
        raise ValueError('candidate gateway and dashboard must be active')
    if not common.gateway_quiet():raise ValueError('Hermes has active work or stale control state')
    if any(common.busy_service(unit) for unit in common.WATCH_SERVICES):
        raise ValueError('watch service is processing work')
    units={unit:unit_state(unit) for unit in [*hermes.CONSUMERS,DEPLOY_TIMER]}
    validate_preflight_unit_states(units)
    if units[DEPLOY_TIMER]['active_state']!='inactive' or units[DEPLOY_TIMER]['unit_file_state'] not in {'enabled','enabled-runtime'}:
        raise ValueError('deployment timer is not the expected paused enabled timer')
    dbs=expected_databases();before={};content_before={}
    missing=[]
    ledger_candidates=[hermes.HOME/'cron/executions.db',
                       *sorted(hermes.HOME.glob('profiles/*/cron/executions.db'))]
    for path in ledger_candidates:
        if path.is_file() and not path.is_symlink():
            if 'table:cron_incidents' not in db_inventory(path)['objects']:missing.append(path)
    if set(missing)!=set(dbs.values()):raise ValueError('unexpected cron incident migration inventory')
    for profile,path in dbs.items():
        if path.is_symlink() or not path.is_file():raise ValueError('expected cron database is not a regular file')
        before[profile]=db_inventory(path)
        if before[profile]['incidents'] is not None:raise ValueError('expected cron migration is already present')
        content_before[profile]=db_content_digest(path,before[profile]['counts'])
    live=hermes.live_schemas();expected_schemas=pending.get('schemas')
    if not isinstance(expected_schemas,dict) or set(live)!=set(expected_schemas):
        raise ValueError('pending schema inventory differs from live inventory')
    if live!=expected_schemas:raise ValueError('live schemas drifted from the pending receipt')
    reconciled_schemas=dict(expected_schemas)
    if hermes.file_identity(candidate)!=source_candidate_identity:
        raise ValueError('candidate changed during initializer preflight')
    # Publication and archive capacity are deterministic gates before journaling.
    hermes.preflight_publication_capacity(False)
    archive=hermes.BASE/ARCHIVE
    try:archive_mode=archive.lstat().st_mode
    except FileNotFoundError:archive_mode=None
    if archive_mode is not None:
        if not stat.S_ISDIR(archive_mode):raise ValueError('marker archive is not a private directory')
        if stat.S_IMODE(archive_mode)!=0o700:raise ValueError('marker archive permissions are not private')
    existing=list(archive.glob('*.json')) if archive_mode is not None else []
    holders=list(hermes.BASE.glob('.*.archive.*'))
    plans={}
    for kind,identity in [('pending',pending_identity),('intervention',intervention_identity)]:
        digest=spec[kind+'_sha256']
        tomb_data=(json.dumps(tombstone(kind,digest,spec['candidate_sha'],spec['run_id']),
                              sort_keys=True,indent=2)+'\n').encode()
        plans[kind]={'holder':str(hermes.BASE/('.'+kind+'.archive.'+digest)),
                     'destination':str(archive/(kind+'-'+digest+'.json')),
                     'marker_identity':identity,
                     'tombstone_sha256':hashlib.sha256(tomb_data).hexdigest()}
        if os.path.lexists(plans[kind]['holder']) or os.path.lexists(plans[kind]['destination']):
            raise ValueError('deterministic marker archive path already exists before journaling')
    if len(existing)+len(holders)+4>MAX_ARCHIVED_MARKERS:raise ValueError('marker archive limit reached')
    installed=hermes.installation()
    if set(installed)!=hermes.expected_installation_paths():raise ValueError('Hermes installation fingerprint is incomplete')
    backup_sources={}
    for name in ['hermes-shim','hermes-dashboard.service']:
        source_path=backup/name
        backup_sources[str(source_path)]=private_regular(source_path,'legacy backup '+name)
    old_shim=backup/'hermes-shim';old_dashboard=backup/'hermes-dashboard.service'
    if (str(old_shim) not in backup_sources or str(old_dashboard) not in backup_sources
            or installed[str(hermes.SHIM)]['sha256']!=backup_sources[str(old_shim)]['sha256']
            or installed[str(hermes.UNITS/'hermes-dashboard.service')]['sha256']!=backup_sources[str(old_dashboard)]['sha256']):
        raise ValueError('partial cutover does not match receipt-bound previous launcher/dashboard')
    hermes.validate_gateway_unit(root);hermes.validate_dashboard_unit()
    if hermes.file_identity(candidate)!=source_candidate_identity:
        raise ValueError('candidate changed at the preflight boundary')
    generation,generation_digest_value,candidate_identity,shim_identity,dependency=prepare_runtime_generation(root)
    if hermes.file_identity(candidate)!=source_candidate_identity:
        raise ValueError('candidate changed while creating protected runtime generation')
    # Final preflight gate: execute only the sealed generation against copies.
    with tempfile.TemporaryDirectory(prefix='hermes-reconcile-canary-') as folder:
        for profile,path in dbs.items():
            copy=Path(folder)/profile/'cron/executions.db';copy.parent.mkdir(parents=True)
            with closing(sqlite3.connect(f'file:{path}?mode=ro',uri=True)) as source:
                with closing(sqlite3.connect(copy)) as destination:source.backup(destination)
            official_initialize(generation,copy)
            migrated=db_inventory(copy);validate_schema_delta(before[profile],migrated)
            reconciled_schemas[str(path)]=common.db_schema(copy)
    validate_runtime_generation(generation,generation_digest_value)
    return {'schema':1,'spec':spec,'candidate_root':str(root),
            'source_candidate_identity':source_candidate_identity,
            'runtime_generation':str(generation),'runtime_generation_digest':generation_digest_value,
            'dependency_closure':dependency,'candidate_identity':candidate_identity,
            'shim_identity':shim_identity,
            'request':request,'receipt_identity':receipt_identity,'external_evidence':external_evidence,
            'pending_identity':pending_identity,'intervention_identity':intervention_identity,
            'sqlite_archive_identity':archive_identity,'backup_sources':backup_sources,
            'database_before':before,'database_content_before':content_before,
            'reconciled_schemas':reconciled_schemas,
            'installed_before':installed,'config_hashes':hermes.config_fingerprints(),
            'telegram_adapters':adapter_names,'gateway_pid':pid,'archive_plan':plans,
            'units':units,'phase':'prepared','migrated_profiles':[],
            'aligned':[],'archived':[],'started_at':time.time()}


def official_initialize(root,path):
    code=('from cron import executions, incidents; '
          'executions._connect().close(); incidents._connect().close()')
    with tempfile.TemporaryDirectory(prefix='hermes-reconcile-home-') as private_home:
        root=Path(root);manifest=json.loads((root/'manifest.json').read_text())
        python=root/manifest['runtime']['storePython'];site=root/manifest['runtime']['sitePackages']
        target_home=Path(path).parents[1]
        key=assert_no_payload_install_state(root,[hermes.HOME,target_home])
        bootstrap=("import site,sys; site.addsitedir(sys.argv.pop(1)); "
                   "sys.path.insert(0,sys.argv.pop(1)); "+code)
        env={'HOME':private_home,'HERMES_HOME':str(target_home),
             'PATH':os.environ.get('PATH','/usr/bin:/bin'),'LANG':'C.UTF-8','LC_ALL':'C.UTF-8'}
        subprocess.run([str(python),'-I','-S','-c',bootstrap,str(site),str(root)],cwd=root,
                       env=env,stdin=subprocess.DEVNULL,check=True,capture_output=True,timeout=180)
        assert_no_payload_install_state(root,[hermes.HOME,target_home],key)


def persist(tx,phase=None):
    if phase is not None:tx['phase']=phase
    common.atomic(hermes.BASE/JOURNAL,tx)


def validate_dependency_provenance(value):
    required={'schema','install_key','facts_identity','environment','environment_digest',
              'resolved_lock_identity','activation_inputs','extras','stamp','site_packages','python',
              'selected_python','install_stamp_identity','payload_environment_digest',
              'payload_python_digest','manifest_sha256','payload_install_key'}
    if not isinstance(value,dict) or set(value)!=required or value.get('schema')!=1:
        raise ValueError('dependency closure provenance schema mismatch')
    if (not re.fullmatch('[0-9a-f]{16}',str(value['install_key']))
            or not re.fullmatch('[0-9a-f]{16}',str(value['payload_install_key']))):
        raise ValueError('dependency closure install key is invalid')
    for name in ['environment_digest','payload_environment_digest','payload_python_digest','manifest_sha256','stamp']:
        if not isinstance(value[name],str) or not re.fullmatch('[0-9a-f]{64}',value[name]):
            raise ValueError('dependency closure digest is invalid')
    validate_identity(value['facts_identity']);validate_identity(value['resolved_lock_identity'])
    validate_identity(value['install_stamp_identity'])
    if value['install_stamp_identity']['sha256']!=INCIDENT_INSTALL_STAMP_SHA256:
        raise ValueError('dependency install identity stamp mismatch')
    inputs=value['activation_inputs']
    if not isinstance(inputs,dict) or set(inputs)!={'uv.lock','pyproject.toml','pm/lock.json'}:
        raise ValueError('dependency closure input provenance mismatch')
    for item in inputs.values():
        if (not isinstance(item,dict) or set(item)!={'sha256','mtime_ns'}
                or not re.fullmatch('[0-9a-f]{64}',str(item['sha256']))
                or type(item['mtime_ns']) is not int):
            raise ValueError('dependency closure input provenance mismatch')
    python=value['python']
    if (not isinstance(python,dict) or set(python)!={'entry','artifact_sha256','tree_digest','python'}
            or any(not isinstance(python[name],str) or not python[name]
                   for name in ['entry','artifact_sha256','tree_digest','python'])
            or not re.fullmatch('[0-9a-f]{64}',python['artifact_sha256'])
            or not re.fullmatch('[0-9a-f]{64}',python['tree_digest'])):
        raise ValueError('dependency Python provenance mismatch')
    if (not isinstance(value['environment'],str) or not isinstance(value['site_packages'],str)
            or not isinstance(value['selected_python'],str) or not value['selected_python']
            or not isinstance(value['extras'],list)
            or any(not isinstance(extra,str) for extra in value['extras'])):
        raise ValueError('dependency closure provenance is invalid')
    expected_key=hashlib.sha256(str(Path(INCIDENT_CANDIDATE_ROOT).resolve()).encode()).hexdigest()[:16]
    expected_payload_key=hashlib.sha256(str(runtime_generation_path().resolve()).encode()).hexdigest()[:16]
    state=hermes.HOME/'installs'/expected_key;environment=Path(value['environment']);site=Path(value['site_packages'])
    expected_entry='python-'+INCIDENT_PYTHON_VERSION+'-'+INCIDENT_PYTHON_TARGET
    if (value['install_key']!=expected_key or value['payload_install_key']!=expected_payload_key
            or not environment.resolve().is_relative_to((state/'environments').resolve())
            or environment.name!='venv' or not site.resolve().is_relative_to(environment.resolve())
            or python['entry']!=expected_entry
            or Path(value['selected_python']).resolve()!=(hermes.HOME/'tools'/expected_entry).resolve()):
        raise ValueError('dependency closure incident provenance mismatch')


def verify_dependency_closure(candidate_root,generation,value):
    validate_dependency_provenance(value)
    current=dependency_closure_plan(candidate_root,generation,value['python'])
    for key in current:
        if value.get(key)!=current[key]:raise ValueError('selected dependency closure changed')
    environment=Path(generation)/'venv';runtime=Path(generation)/'tools'/value['python']['entry']
    if (official_tree_digest(generation,environment)!=value['payload_environment_digest']
            or official_tree_digest(generation,runtime)!=value['payload_python_digest']
            or common.digest(Path(generation)/'manifest.json')!=value['manifest_sha256']
            or common.digest(Path(generation)/'install-stamp.json')
               !=value['install_stamp_identity']['sha256']):
        raise ValueError('protected payload dependency closure changed')
    assert_no_payload_install_state(generation,reconciliation_homes(),value['payload_install_key'])


def verify_candidate(tx):
    archive=Path(tx['spec']['backup'])/'sqlite-backup.zip'
    if private_regular(archive,'legacy SQLite archive')!=tx['sqlite_archive_identity']:
        raise ValueError('legacy SQLite archive changed after reconciliation preflight')
    for source,identity in tx['backup_sources'].items():
        if private_regular(Path(source),'legacy backup source')!=identity:
            raise ValueError('legacy backup source changed after reconciliation preflight')
    request,root,receipt_identity,evidence=legacy_request(tx['spec']['candidate_sha'],tx['spec']['run_id'])
    source_candidate=root/'.hermes/bin/hermes'
    if (request!=tx['request'] or receipt_identity!=tx['receipt_identity']
            or evidence!=tx['external_evidence']
            or str(root)!=tx['candidate_root']
            or hermes.file_identity(source_candidate)!=tx['source_candidate_identity']):
        raise ValueError('candidate changed after reconciliation preflight')
    generation=Path(tx['runtime_generation']);candidate=generation/'bin/hermes'
    validate_runtime_generation(generation,tx['runtime_generation_digest'])
    if hermes.file_identity(candidate)!=tx['candidate_identity']:
        raise ValueError('protected runtime launcher changed after reconciliation preflight')
    if hermes.file_identity(generation/'.hermes/bin/hermes')!=tx['shim_identity']:
        raise ValueError('protected runtime shim changed after reconciliation preflight')
    verify_dependency_closure(root,generation,tx['dependency_closure'])
    hermes.prove_launcher(candidate,tx['spec']['candidate_sha'],tx['candidate_identity']['sha256'],
                          generation,tx['request']['tag'],sealed_payload=True)
    validate_runtime_generation(generation,tx['runtime_generation_digest'])
    return generation,candidate


def restore_units(tx):
    for unit in ['hermes-gateway.service','hermes-dashboard.service',*common.WATCH_TIMERS]:
        expected=tx['units'][unit]['active_state']
        current=unit_state(unit)['active_state']
        if expected=='active' and current!='active':hermes.ctl('start',unit)
        elif expected!='active' and current=='active':hermes.ctl('stop',unit)


def verify_units(tx,include_timer=False):
    names=[*hermes.CONSUMERS]
    if include_timer:names.append(DEPLOY_TIMER)
    for unit in names:
        expected=dict(tx['units'][unit])
        if include_timer and unit==DEPLOY_TIMER:expected['active_state']='active'
        if unit_state(unit)!=expected:raise ValueError('unit state was not restored exactly: '+unit)


def verify_runtime(tx,root):
    state=hermes.json_file(hermes.HOME/'gateway_state.json')
    names=sorted(name for name in state.get('platforms',{})
                 if name=='telegram' or name.endswith(':telegram'))
    telegram=[state['platforms'][name] for name in names]
    try:age=time.time()-__import__('datetime').datetime.fromisoformat(state['updated_at']).timestamp()
    except (KeyError,TypeError,ValueError):raise ValueError('gateway state freshness is invalid') from None
    pid_text=hermes.command(['systemctl','--user','show','hermes-gateway.service','-p','MainPID','--value'])
    if not isinstance(pid_text,str) or not pid_text.isdecimal():raise ValueError('gateway MainPID unavailable')
    pid=int(pid_text)
    if (state.get('gateway_state')!='running' or state.get('code_sha')!=tx['spec']['candidate_sha']
            or names!=tx['telegram_adapters'] or not telegram
            or any(value.get('state')!='connected' for value in telegram)
            or not -30<=age<=180 or pid<=1 or state.get('pid')!=pid):
        raise ValueError('candidate or Telegram adapters failed post-restart verification')
    hermes.prove_launcher(hermes.SHIM,tx['spec']['candidate_sha'],tx['shim_identity']['sha256'],
                          root,tx['request']['tag'],sealed_payload=True)
    hermes.validate_gateway_unit(root,root/'bin/hermes');hermes.validate_dashboard_unit()


def enforce_offline(tx):
    """Reassert every stop gate before each live DB or installation write."""
    units=[*common.WATCH_TIMERS,*common.WATCH_SERVICES,
           'hermes-gateway.service','hermes-dashboard.service']
    states={unit:unit_state(unit) for unit in units}
    if any(not safely_offline(states[unit]) for unit in common.WATCH_SERVICES):
        raise ValueError('watch service became active during reconciliation')
    if not safely_offline(states['hermes-gateway.service']):
        if not common.gateway_quiet():raise ValueError('Hermes became busy before reconciliation stop')
    # Stop every unit, including an apparently inactive/failed one, to cancel a
    # queued activation.  A write is admitted only after a fresh all-unit read.
    for unit in units:
        try:hermes.ctl('stop',unit)
        except subprocess.CalledProcessError:
            if not safely_offline(unit_state(unit)):
                raise ValueError('unit stop refused reconciliation: '+unit) from None
    states={unit:unit_state(unit) for unit in units}
    unsafe=[unit for unit,state in states.items() if not safely_offline(state)]
    if unsafe:raise ValueError('reconciliation consumers did not remain offline: '+','.join(unsafe))


def verify_archived_fence(tx):
    if set(tx.get('archived',[]))!={'pending','intervention'}:
        raise ValueError('reconciliation marker archive receipt is incomplete')
    for kind in ['pending','intervention']:
        if not marker_is_archived(hermes.BASE/(kind+'.json'),tx):
            raise ValueError('reconciliation marker fence changed: '+kind)


def verify_final(tx,root,timer_started=False,require_fence=False):
    verify_candidate(tx);verify_units(tx);verify_runtime(tx,root)
    expected=dict(tx['units'][DEPLOY_TIMER])
    if timer_started:expected['active_state']='active'
    if unit_state(DEPLOY_TIMER)!=expected:
        raise ValueError('deployment timer changed during reconciliation')
    if hermes.config_fingerprints()!=tx['config_hashes']:
        raise ValueError('configuration changed during reconciliation')
    if hermes.live_schemas()!=tx['reconciled_schemas']:
        raise ValueError('live schema receipt mismatch')
    for profile,path in expected_databases().items():
        validate_schema_delta(tx['database_before'][profile],db_inventory(path))
        if db_content_digest(path,tx['database_before'][profile]['counts'])!=tx['database_content_before'][profile]:
            raise ValueError('existing cron database contents changed during migration')
    if require_fence:verify_archived_fence(tx)


def completion_receipt(tx):
    receipt=dict(tx);receipt['phase']='complete'
    return receipt


def verify_completion_receipt(tx):
    receipt,identity=validated_completion_receipt()
    if receipt!=completion_receipt(tx):
        raise ValueError('completion receipt changed during reconciliation')
    return identity


def validate_identity(value,private=False):
    if not isinstance(value,dict) or set(value)!=RECEIPT_IDENTITY_KEYS:
        raise ValueError('invalid file identity')
    if not isinstance(value['sha256'],str) or not re.fullmatch('[0-9a-f]{64}',value['sha256']):
        raise ValueError('invalid file identity digest')
    if any(type(value[key]) is not int for key in RECEIPT_IDENTITY_KEYS-{'sha256'}):
        raise ValueError('invalid file identity fields')
    if not stat.S_ISREG(value['mode']) or (private and stat.S_IMODE(value['mode'])&0o077):
        raise ValueError('invalid file identity mode')


def validate_transaction_structure(tx,completion=False):
    """Reject substituted or impossible durable state before any live action."""
    if not isinstance(tx,dict) or set(tx)!=COMPLETION_KEYS or tx.get('schema')!=1:
        raise ValueError('reconciliation transaction schema mismatch')
    validate_spec(tx['spec'])
    phase=tx.get('phase')
    if phase not in PHASES or (phase=='complete')!=completion:
        raise ValueError('reconciliation transaction phase mismatch')
    expected_request={'schema':1,'component':'hermes','candidate_sha':INCIDENT_CANDIDATE_SHA,
                      'upstream_sha':INCIDENT_UPSTREAM_SHA,'tag':INCIDENT_TAG,
                      'receipt_sha256':INCIDENT_RECEIPT_SHA256,'action':'promote'}
    if tx.get('request')!=expected_request or tx.get('candidate_root')!=INCIDENT_CANDIDATE_ROOT:
        raise ValueError('reconciliation incident request provenance mismatch')
    expected_evidence={'run_id':INCIDENT_RUN_ID,'workflow_id':INCIDENT_WORKFLOW_ID,
                       'check_suite_id':INCIDENT_CHECK_SUITE_ID,'control_sha':INCIDENT_CONTROL_SHA,
                       'request':expected_request}
    if tx.get('external_evidence')!=expected_evidence:
        raise ValueError('reconciliation external provenance mismatch')
    for name in ['source_candidate_identity','candidate_identity','shim_identity','receipt_identity','pending_identity','intervention_identity',
                 'sqlite_archive_identity']:
        validate_identity(tx.get(name),private=name not in {'source_candidate_identity','candidate_identity','shim_identity'})
    validate_dependency_provenance(tx.get('dependency_closure'))
    expected_generation=runtime_generation_path()
    if (tx.get('runtime_generation')!=str(expected_generation)
            or not isinstance(tx.get('runtime_generation_digest'),str)
            or not re.fullmatch('[0-9a-f]{64}',tx['runtime_generation_digest'])):
        raise ValueError('reconciliation runtime generation provenance mismatch')
    if (tx['receipt_identity']['sha256']!=INCIDENT_RECEIPT_SHA256
            or tx['pending_identity']['sha256']!=INCIDENT_PENDING_SHA256
            or tx['intervention_identity']['sha256']!=INCIDENT_INTERVENTION_SHA256
            or tx['sqlite_archive_identity']['sha256']!=INCIDENT_SQLITE_ARCHIVE_SHA256):
        raise ValueError('reconciliation retained evidence identity mismatch')
    backup=Path(tx['spec']['backup'])
    expected_sources={str(backup/'hermes-shim'),str(backup/'hermes-dashboard.service')}
    if not isinstance(tx.get('backup_sources'),dict) or set(tx['backup_sources'])!=expected_sources:
        raise ValueError('reconciliation backup source inventory mismatch')
    for value in tx['backup_sources'].values():validate_identity(value,private=True)
    inventories=tx.get('database_before')
    if not isinstance(inventories,dict) or set(inventories)!=set(EXPECTED_PROFILES):
        raise ValueError('reconciliation database inventory mismatch')
    content=tx.get('database_content_before')
    if (not isinstance(content,dict) or set(content)!=set(EXPECTED_PROFILES)
            or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)
                   for value in content.values())):
        raise ValueError('reconciliation database content receipt mismatch')
    for inventory in inventories.values():
        if (not isinstance(inventory,dict) or set(inventory)!={'objects','counts','incidents'}
                or not isinstance(inventory['objects'],dict) or not isinstance(inventory['counts'],dict)
                or inventory['incidents'] is not None):
            raise ValueError('reconciliation database inventory is invalid')
    schemas=tx.get('reconciled_schemas')
    if (not isinstance(schemas,dict)
            or any(not isinstance(path,str) or not isinstance(value,str) for path,value in schemas.items())):
        raise ValueError('reconciliation schema receipt is invalid')
    installed=tx.get('installed_before')
    if not isinstance(installed,dict) or set(installed)!=hermes.expected_installation_paths():
        raise ValueError('reconciliation installation inventory mismatch')
    for value in installed.values():validate_identity(value)
    config=tx.get('config_hashes')
    if (not isinstance(config,dict) or any(not isinstance(path,str) or not isinstance(digest,str)
            or not re.fullmatch('[0-9a-f]{64}',digest) for path,digest in config.items())):
        raise ValueError('reconciliation config receipt is invalid')
    adapters=tx.get('telegram_adapters')
    if (not isinstance(adapters,list) or not adapters or adapters!=sorted(set(adapters))
            or any(not isinstance(name,str) for name in adapters)):
        raise ValueError('reconciliation Telegram adapter receipt is invalid')
    if type(tx.get('gateway_pid')) is not int or tx['gateway_pid']<=1:
        raise ValueError('reconciliation gateway PID is invalid')
    units=tx.get('units');expected_units={*hermes.CONSUMERS,DEPLOY_TIMER}
    if not isinstance(units,dict) or set(units)!=expected_units:
        raise ValueError('reconciliation unit inventory mismatch')
    for state in units.values():
        if (not isinstance(state,dict) or set(state)!={'active_state','unit_file_state'}
                or state['active_state'] not in TERMINAL_ACTIVE_STATES
                or not isinstance(state['unit_file_state'],str) or not state['unit_file_state']):
            raise ValueError('reconciliation unit state is invalid')
    if type(tx.get('started_at')) not in {int,float} or tx['started_at']<=0:
        raise ValueError('reconciliation start time is invalid')
    plans=tx.get('archive_plan')
    if not isinstance(plans,dict) or set(plans)!={'pending','intervention'}:
        raise ValueError('reconciliation archive plan mismatch')
    for kind in ['pending','intervention']:
        plan=plans[kind];digest=tx['spec'][kind+'_sha256']
        if (not isinstance(plan,dict) or set(plan)!={'holder','destination','marker_identity','tombstone_sha256'}
                or plan['marker_identity']!=tx[kind+'_identity']
                or plan['holder']!=str(hermes.BASE/('.'+kind+'.archive.'+digest))
                or plan['destination']!=str(hermes.BASE/ARCHIVE/(kind+'-'+digest+'.json'))
                or not isinstance(plan['tombstone_sha256'],str)
                or not re.fullmatch('[0-9a-f]{64}',plan['tombstone_sha256'])):
            raise ValueError('reconciliation archive plan is invalid')
    rank=PHASES.index(phase)
    sequences=[('migrated_profiles',list(EXPECTED_PROFILES),'migrating','migrated'),
               ('aligned',['launcher','gateway','dashboard'],'aligning','aligned'),
               ('archived',['pending','intervention'],'archiving','archived')]
    for name,complete_values,in_progress,finished in sequences:
        value=tx.get(name)
        if not isinstance(value,list) or value!=complete_values[:len(value)]:
            raise ValueError('reconciliation '+name+' progress is invalid')
        if rank<PHASES.index(in_progress) and value:
            raise ValueError('reconciliation '+name+' is ahead of phase')
        if rank>=PHASES.index(finished) and value!=complete_values:
            raise ValueError('reconciliation '+name+' is behind phase')


def load_resume_journal(path):
    try:
        identity,raw=marker_digest(path)
        if stat.S_IMODE(identity['mode'])!=0o600:raise ValueError
        tx=json.loads(raw)
        validate_transaction_structure(tx)
        return tx
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('reconciliation journal is missing, unsafe, or invalid') from None


def validated_completion_receipt():
    """Load only the exact private, complete, internally consistent receipt."""
    path=hermes.BASE/LAST
    try:
        identity=private_regular(path,'completion receipt')
        before,raw=marker_digest(path)
        receipt=json.loads(raw)
        if identity!=before:
            raise ValueError
        validate_transaction_structure(receipt,completion=True)
        if set(receipt.get('request',{}))!={'schema','component','candidate_sha','upstream_sha','tag','receipt_sha256','action'}:
            raise ValueError
        from hermes_control import validate_request
        validate_request(receipt['request'])
        if (receipt['request']['candidate_sha']!=receipt['spec']['candidate_sha']
                or receipt['request']['action']!='promote'):raise ValueError
        if str(hermes.candidate_root(receipt['candidate_root']))!=receipt['candidate_root']:
            raise ValueError
        evidence=receipt.get('external_evidence')
        if (not isinstance(evidence,dict)
                or set(evidence)!={'run_id','workflow_id','check_suite_id','control_sha','request'}
                or evidence['run_id']!=receipt['spec']['run_id']
                or type(evidence['workflow_id']) is not int or type(evidence['check_suite_id']) is not int
                or require_sha(evidence['control_sha'])!=evidence['control_sha']
                or evidence['request']!=receipt['request']):raise ValueError
        for name in ['source_candidate_identity','candidate_identity','shim_identity','receipt_identity','pending_identity','intervention_identity',
                     'sqlite_archive_identity']:
            value=receipt.get(name)
            if not isinstance(value,dict) or set(value)!=RECEIPT_IDENTITY_KEYS:raise ValueError
            if not re.fullmatch('[0-9a-f]{64}',str(value.get('sha256',''))):raise ValueError
            if (any(type(value[key]) is not int for key in RECEIPT_IDENTITY_KEYS-{'sha256'})
                    or not stat.S_ISREG(value['mode'])):raise ValueError
        for name in ['receipt_identity','pending_identity','intervention_identity','sqlite_archive_identity']:
            if stat.S_IMODE(receipt[name]['mode'])&0o077:raise ValueError
        backup=Path(receipt['spec']['backup'])
        expected_sources={str(backup/'hermes-shim'),str(backup/'hermes-dashboard.service')}
        if set(receipt.get('backup_sources',{}))!=expected_sources:raise ValueError
        if any(not isinstance(value,dict) or set(value)!=RECEIPT_IDENTITY_KEYS
               for value in receipt['backup_sources'].values()):raise ValueError
        if set(receipt.get('database_before',{}))!=set(EXPECTED_PROFILES):raise ValueError
        for inventory in receipt['database_before'].values():
            if (not isinstance(inventory,dict) or set(inventory)!={'objects','counts','incidents'}
                    or not isinstance(inventory['objects'],dict) or not isinstance(inventory['counts'],dict)
                    or inventory['incidents'] is not None):raise ValueError
        content=receipt.get('database_content_before')
        if (not isinstance(content,dict) or set(content)!=set(EXPECTED_PROFILES)
                or any(not isinstance(value,str) or not re.fullmatch('[0-9a-f]{64}',value)
                       for value in content.values())):raise ValueError
        if not isinstance(receipt.get('reconciled_schemas'),dict):raise ValueError
        if set(receipt.get('installed_before',{}))!=hermes.expected_installation_paths():raise ValueError
        if (not isinstance(receipt.get('config_hashes'),dict)
                or any(not isinstance(path,str) or not re.fullmatch('[0-9a-f]{64}',str(digest))
                       for path,digest in receipt['config_hashes'].items())):raise ValueError
        adapters=receipt.get('telegram_adapters')
        if (not isinstance(adapters,list) or not adapters or adapters!=sorted(set(adapters))
                or any(not isinstance(name,str) for name in adapters)):raise ValueError
        if type(receipt.get('gateway_pid')) is not int or receipt['gateway_pid']<=1:raise ValueError
        expected_units={*hermes.CONSUMERS,DEPLOY_TIMER}
        if set(receipt.get('units',{}))!=expected_units:raise ValueError
        for state in receipt['units'].values():
            if (not isinstance(state,dict) or set(state)!={'active_state','unit_file_state'}
                    or state['active_state'] not in TERMINAL_ACTIVE_STATES
                    or not isinstance(state['unit_file_state'],str) or not state['unit_file_state']):raise ValueError
        if type(receipt.get('started_at')) not in {int,float} or receipt['started_at']<=0:raise ValueError
        if receipt.get('migrated_profiles')!=list(EXPECTED_PROFILES):raise ValueError
        if receipt.get('runtime_generation')!=str(runtime_generation_path()):raise ValueError
        if not re.fullmatch('[0-9a-f]{64}',str(receipt.get('runtime_generation_digest',''))):raise ValueError
        if receipt.get('aligned')!=['launcher','gateway','dashboard']:raise ValueError
        if receipt.get('archived')!=['pending','intervention']:raise ValueError
        plans=receipt.get('archive_plan')
        if not isinstance(plans,dict) or set(plans)!={'pending','intervention'}:raise ValueError
        for kind in ['pending','intervention']:
            plan=plans[kind];digest=receipt['spec'][kind+'_sha256']
            if (not isinstance(plan,dict)
                    or set(plan)!={'holder','destination','marker_identity','tombstone_sha256'}
                    or plan['marker_identity']!=receipt[kind+'_identity']
                    or plan['holder']!=str(hermes.BASE/('.'+kind+'.archive.'+digest))
                    or plan['destination']!=str(hermes.BASE/ARCHIVE/(kind+'-'+digest+'.json'))
                    or not re.fullmatch('[0-9a-f]{64}',str(plan['tombstone_sha256']))
                    or not marker_is_archived(hermes.BASE/(kind+'.json'),receipt)):
                raise ValueError
        return receipt,identity
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):
        raise ValueError('completion receipt is missing, unsafe, or incomplete') from None


def finalize(tx,root,journal):
    """Idempotently publish a fully verified post-timer completion receipt."""
    phase=tx['phase']
    if phase=='archived':persist(tx,'resuming-timer');phase='resuming-timer'
    if phase=='resuming-timer':
        before=dict(tx['units'][DEPLOY_TIMER]);after=dict(before);after['active_state']='active'
        current=unit_state(DEPLOY_TIMER)
        if current==before:
            verify_final(tx,root,require_fence=True)
            hermes.command(['systemctl','--user','start',DEPLOY_TIMER])
        elif current!=after:
            raise ValueError('deployment timer is neither verified pre-start nor post-start state')
        # Covers both the normal path and a crash after systemctl returned but
        # before the next journal publication.
        verify_final(tx,root,timer_started=True,require_fence=True)
        persist(tx,'timer-started');phase='timer-started'
    if phase=='timer-started':
        verify_final(tx,root,timer_started=True,require_fence=True)
        common.atomic(hermes.BASE/LAST,completion_receipt(tx))
        verify_completion_receipt(tx)
        persist(tx,'last-published');phase='last-published'
    if phase=='last-published':
        verify_final(tx,root,timer_started=True,require_fence=True)
        verify_completion_receipt(tx)
        journal.unlink()
        hermes.fsync_directory(hermes.BASE)
        return True
    raise ValueError('unknown reconciliation finalization phase')


def tombstone(kind,digest,candidate_sha,run_id):
    return {'schema':TOMBSTONE_SCHEMA,'kind':'hermes-partial-cutover-archived','marker':kind,
            'sha256':digest,'candidate_sha':candidate_sha,'run_id':run_id}


def archive_marker(tx,kind):
    """Archive one marker using the pre-journaled deterministic holder."""
    path=hermes.BASE/(kind+'.json');digest=tx['spec'][kind+'_sha256']
    plan=tx['archive_plan'][kind];holder=Path(plan['holder']);destination=Path(plan['destination'])
    archive=hermes.BASE/ARCHIVE;archive.mkdir(mode=0o700,parents=True,exist_ok=True)
    if not stat.S_ISDIR(archive.lstat().st_mode) or stat.S_IMODE(archive.lstat().st_mode)!=0o700:
        raise ValueError('marker archive is not a private directory')
    expected_tomb=tombstone(kind,digest,tx['spec']['candidate_sha'],tx['spec']['run_id'])
    data=(json.dumps(expected_tomb,sort_keys=True,indent=2)+'\n').encode()
    if hashlib.sha256(data).hexdigest()!=plan['tombstone_sha256']:
        raise ValueError('journaled tombstone plan changed')

    def classify(entry):
        try:identity,raw=marker_digest(entry)
        except ValueError:
            if Path(entry).exists() or Path(entry).is_symlink():raise
            return 'absent',None
        if (hermes.exchanged_identity_matches(identity,plan['marker_identity'])
                and hashlib.sha256(raw).hexdigest()==digest):return 'original',identity
        if hashlib.sha256(raw).hexdigest()==plan['tombstone_sha256'] and json.loads(raw)==expected_tomb:return 'tombstone',identity
        raise ValueError('foreign marker entry during archival')

    destination_state,_=classify(destination)
    marker_state,_=classify(path);holder_state,_=classify(holder)
    if destination_state!='absent':
        if destination_state!='original' or marker_state!='tombstone':
            raise ValueError('archived marker state mismatch')
        return
    if holder_state=='absent':
        if marker_state!='original':raise ValueError('marker changed before archival')
        fd=os.open(holder,os.O_WRONLY|os.O_CREAT|os.O_EXCL|getattr(os,'O_NOFOLLOW',0),0o600)
        with os.fdopen(fd,'wb') as stream:
            stream.write(data);stream.flush();os.fsync(stream.fileno())
        hermes.fsync_directory(hermes.BASE)
        holder_state,_=classify(holder)
    if marker_state=='original' and holder_state=='tombstone':
        # This is the sole safe pre-exchange state.  If renameat2 reports an
        # uncertain error, the next resume classifies both names before acting.
        hermes.rename_exchange(holder,path)
        hermes.fsync_directory(hermes.BASE)
        marker_state,_=classify(path);holder_state,_=classify(holder)
    if marker_state!='tombstone' or holder_state!='original':
        raise ValueError('foreign marker race during archival')
    try:os.link(holder,destination,follow_symlinks=False)
    except FileExistsError:pass
    destination_state,_=classify(destination)
    if destination_state!='original':raise ValueError('archived marker verification failed')
    hermes.fsync_directory(archive);hermes.fsync_directory(hermes.BASE)


def marker_is_archived(path,last):
    try:
        path=Path(path);kind=path.stem;archive_dir=hermes.BASE/ARCHIVE
        directory_before=archive_dir.lstat()
        if (not stat.S_ISDIR(directory_before.st_mode)
                or stat.S_IMODE(directory_before.st_mode)!=0o700):return False
        marker_identity,marker_raw=marker_digest(path)
        if stat.S_IMODE(marker_identity['mode'])!=0o600:return False
        value=json.loads(marker_raw)
        expected=tombstone(kind,last['spec'][kind+'_sha256'],last['spec']['candidate_sha'],last['spec']['run_id'])
        archive=archive_dir/(kind+'-'+expected['sha256']+'.json')
        archive_identity,archive_raw=marker_digest(archive)
        if stat.S_IMODE(archive_identity['mode'])!=0o600:return False
        directory_after=archive_dir.lstat()
        fields=lambda entry:(entry.st_dev,entry.st_ino,entry.st_mode,entry.st_uid,entry.st_gid,
                             entry.st_mtime_ns,entry.st_ctime_ns)
        return (fields(directory_before)==fields(directory_after) and value==expected
                and hashlib.sha256(archive_raw).hexdigest()==expected['sha256']
                and hermes.file_identity(path)==marker_identity
                and hermes.file_identity(archive)==archive_identity)
    except (OSError,ValueError,KeyError,TypeError,json.JSONDecodeError):return False


def reconcile(spec=None):
    journal=hermes.BASE/JOURNAL
    if os.path.lexists(journal):
        tx=load_resume_journal(journal)
        if spec is not None and tx.get('spec')!=spec:raise ValueError('resume arguments differ from durable journal')
    else:
        last=hermes.BASE/LAST
        if spec is not None and os.path.lexists(last):
            completed,_=validated_completion_receipt()
            if completed['spec']==spec:
                root,_=verify_candidate(completed)
                verify_final(completed,root,timer_started=True,require_fence=True)
                verify_completion_receipt(completed)
                hermes.fsync_directory(hermes.BASE)
                return True
        if spec is None:raise ValueError('reconciliation arguments required')
        tx=preflight(spec)  # no live path/service write before this succeeds
        persist(tx)
    root,candidate=verify_candidate(tx)
    phase=tx['phase']
    if phase=='prepared':
        if not common.gateway_quiet():raise ValueError('Hermes became busy before reconciliation stop')
        persist(tx,'stopping');phase='stopping'
    if phase=='stopping':
        enforce_offline(tx)
        persist(tx,'stopped');phase='stopped'
    if phase in {'stopped','migrating'}:
        enforce_offline(tx)
        if phase=='stopped':persist(tx,'migrating')
        for profile,path in expected_databases().items():
            if db_content_digest(path,tx['database_before'][profile]['counts'])!=tx['database_content_before'][profile]:
                raise ValueError('existing cron database contents changed before migration')
            if profile in tx['migrated_profiles']:continue
            current=db_inventory(path)
            if current!=tx['database_before'][profile]:
                # A crash may occur after the official commit but before journal.
                validate_schema_delta(tx['database_before'][profile],current)
            else:
                enforce_offline(tx)
                verify_candidate(tx)
                official_initialize(root,path)
                validate_schema_delta(tx['database_before'][profile],db_inventory(path))
                if db_content_digest(path,tx['database_before'][profile]['counts'])!=tx['database_content_before'][profile]:
                    raise ValueError('existing cron database contents changed during migration')
            tx['migrated_profiles'].append(profile);persist(tx)
        persist(tx,'migrated');phase='migrated'
    if phase in {'migrated','aligning'}:
        enforce_offline(tx)
        if phase=='migrated':persist(tx,'aligning')
        if 'launcher' not in tx['aligned']:
            shim=root/'.hermes/bin/hermes'
            if hermes.file_identity(hermes.SHIM)['sha256']!=tx['shim_identity']['sha256']:
                if hermes.file_identity(hermes.SHIM)!=tx['installed_before'][str(hermes.SHIM)]:
                    raise ValueError('global launcher changed after preflight')
                enforce_offline(tx)
                hermes.publish_file(shim,hermes.SHIM,tx['installed_before'][str(hermes.SHIM)],mode=0o755,
                                    expected_source_identity=tx['shim_identity'])
            hermes.prove_launcher(hermes.SHIM,tx['spec']['candidate_sha'],tx['shim_identity']['sha256'],
                                  root,tx['request']['tag'],sealed_payload=True)
            tx['aligned'].append('launcher');persist(tx)
        if 'gateway' not in tx['aligned']:
            try:hermes.validate_gateway_unit(root,candidate)
            except ValueError:
                gateway=hermes.UNITS/'hermes-gateway.service'
                if hermes.file_identity(gateway)!=tx['installed_before'][str(gateway)]:
                    raise ValueError('gateway unit changed after preflight')
                enforce_offline(tx)
                hermes.align_gateway_unit(root,tx['installed_before'][str(gateway)],candidate)
            hermes.command(['systemctl','--user','daemon-reload']);hermes.validate_gateway_unit(root,candidate)
            tx['aligned'].append('gateway');persist(tx)
        if 'dashboard' not in tx['aligned']:
            try:hermes.validate_dashboard_unit()
            except ValueError:
                dashboard=hermes.UNITS/'hermes-dashboard.service'
                if hermes.file_identity(dashboard)!=tx['installed_before'][str(dashboard)]:
                    raise ValueError('dashboard unit changed after preflight')
                enforce_offline(tx)
                hermes.align_dashboard_unit(tx['installed_before'][str(dashboard)])
            hermes.command(['systemctl','--user','daemon-reload']);hermes.validate_dashboard_unit()
            tx['aligned'].append('dashboard');persist(tx)
        validate_runtime_generation(root,tx['runtime_generation_digest'])
        hermes.validate_gateway_unit(root,candidate)
        persist(tx,'aligned');phase='aligned'
    if phase in {'aligned','starting'}:
        if phase=='aligned':persist(tx,'starting')
        restore_units(tx);hermes.healthy({u:s['active_state']=='active' for u,s in tx['units'].items()},tx['spec']['candidate_sha'])
        verify_units(tx);verify_runtime(tx,root)
        if hermes.live_schemas()!=tx['reconciled_schemas']:
            raise ValueError('post-restart schema receipt mismatch')
        persist(tx,'verified');phase='verified'
    if phase in {'verified','archiving'}:
        if phase=='verified':persist(tx,'archiving')
        # Re-run the complete gate immediately before logically clearing the fence.
        for kind in ['pending','intervention']:
            if kind not in tx['archived']:
                verify_final(tx,root)
                archive_marker(tx,kind);tx['archived'].append(kind);persist(tx)
        persist(tx,'archived');phase='archived'
    if phase in {'archived','resuming-timer','timer-started','last-published'}:
        return finalize(tx,root,journal)
    raise ValueError('unknown reconciliation phase')
