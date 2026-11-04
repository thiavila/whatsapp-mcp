"""Official config-only Indexa auth lane, called under executor.lock.

Staging never stops a service. After durable secure-hold, recovery can only
roll forward to enforce; never restore an open bridge or old credential bytes.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import tempfile
import time
import zipfile

import executor as e
import client_executor as c
import indexa_auth_common as a
from control import admit_run, require_sha
from release_ci import verify

ARTIFACT = 'indexa-auth-release'
CONTROL = Path(__file__).resolve().parent
PYTHON = '/home/thiago/.hermes/tools/python-3.14.9+20261021-linux-x64/bin/python3'
DROPIN = Path('/home/thiago/.config/systemd/user/whatsapp-bridge-indexa.service.d/90-indexa-auth.conf')
PENDING = 'indexa-auth-pending.json'


def module_hashes():
    return {name: e.digest(CONTROL / name) for name in [*a.MODULES, 'executor.py']}


def manifest_valid(manifest, sha):
    if (set(manifest) != {'schema', 'repository', 'sha', 'config_sha256', 'control_sha256'}
            or type(manifest['schema']) is not int or manifest['schema'] != 1 or manifest['repository'] != a.REPOSITORY
            or manifest['sha'] != require_sha(sha)
            or manifest['config_sha256'] != a.fingerprint(a.CONFIG)
            or manifest['control_sha256'] != module_hashes()):
        raise ValueError('auth manifest/control mismatch; versioned bootstrap required')


def stage(run_id, sha):
    folder = e.BASE / 'indexa-auth-releases' / require_sha(sha)
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    receipt = folder / 'stage.json'
    if receipt.exists():
        record = json.loads(receipt.read_text()); manifest_valid(record['manifest'], sha)
        if a.canonical(json.loads((folder / 'auth.json').read_text())) != a.canonical(a.CONFIG):
            raise ValueError('cached auth config changed; preserved')
        return record
    records = e.api(f'repos/{a.REPOSITORY}/actions/runs/{int(run_id)}/artifacts')['artifacts']
    matches = [v for v in records if v['name'] == ARTIFACT and not v['expired']]
    if len(matches) != 1 or not 0 < matches[0]['size_in_bytes'] < 65536:
        raise ValueError('one bounded official auth artifact required')
    item = matches[0]
    raw = subprocess.check_output(['gh', 'api', f'repos/{a.REPOSITORY}/actions/artifacts/{int(item["id"])}/zip'], timeout=45)
    if len(raw) > 65536 or item.get('digest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('auth artifact digest mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sorted(archive.namelist()) != ['auth.json', 'manifest.json'] or any(
                v.file_size > 32768 for v in archive.infolist()):
            raise ValueError('unexpected auth artifact members')
        manifest = json.loads(archive.read('manifest.json')); config = json.loads(archive.read('auth.json'))
    manifest_valid(manifest, sha)
    if a.canonical(config) != a.canonical(a.CONFIG):
        raise ValueError('unexpected auth config')
    record = {'schema': 1, 'sha': sha, 'run_id': run_id, 'manifest': manifest}
    e.atomic(folder / 'auth.json', config); e.atomic(receipt, record)
    return record


def adapter(runtime, script, action, data):
    process = subprocess.Popen([str(runtime / 'python/bin/python3'), '-I', '-B',
                                str(CONTROL / script), action, str(e.HERMES), str(runtime)],
                               stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                               stderr=subprocess.DEVNULL, start_new_session=True,
                               env={'HOME': '/nonexistent', 'PATH': '/usr/bin:/bin', 'LANG': 'C.UTF-8',
                                    'PYTHONDONTWRITEBYTECODE': '1'})
    try:
        output, _ = process.communicate(json.dumps(data).encode(), timeout=50)
        if process.returncode != 0 or len(output) > 65536:
            raise ValueError('private auth adapter failed')
        return json.loads(output)
    finally:
        # Bound/reap only this newly created probe process group, including
        # orphan stdio children after a timeout. Never touch existing clients.
        try: os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError: pass
        try: process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            try: os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError: pass
            process.wait(timeout=1)


def managed_runtime():
    previous = c.last_success()
    if not previous or (e.BASE / 'client-pending.json').exists():
        raise c.Deferred('managed client activation required before auth')
    c.verify_local(previous)
    runtime = c.release(previous['sha']) / 'runtime'
    if c.config(runtime, 'managed').get('all_managed') is not True or not c.mapped(runtime):
        raise c.Deferred('managed client configuration/processes required before auth')
    return runtime, previous


def unit_snapshot():
    files = {}
    fragment = e.run(['systemctl', '--user', 'show', a.UNIT, '-p', 'FragmentPath', '--value'])
    dropins = e.run(['systemctl', '--user', 'show', a.UNIT, '-p', 'DropInPaths', '--value']).split()
    for raw in [fragment, *dropins]:
        path = Path(raw)
        if not raw.startswith('/home/thiago/.config/systemd/user/') or path.is_symlink():
            raise ValueError('unrecognized Indexa unit source')
        files[raw] = e.digest(path)
    command = e.run(['systemctl', '--user', 'show', a.UNIT, '-p', 'ExecStart', '--value'])
    # Strip dynamic pid/timing fields; never request/export Environment values.
    match = re.fullmatch(r'\{ path=([^;]+) ; argv\[\]=([^;]+) ; ignore_errors=.*\}', command)
    if not match:
        raise ValueError('unrecognized Indexa ExecStart metadata')
    return {'files': files, 'path': match[1].strip(), 'argv': match[2].strip()}


def dropin(value):
    return dropin_for_hash(a.fingerprint(value))


def dropin_for_hash(policy_hash):
    if not re.fullmatch('[0-9a-f]{64}', policy_hash):
        raise ValueError('invalid journal policy fingerprint')
    # No user-supplied unit syntax: only fixed paths and a hexadecimal hash.
    return ('[Service]\nExecStart=\nExecStart=' + PYTHON + ' -I -B ' +
            str(CONTROL / 'indexa_auth_runtime.py') + ' ' + policy_hash + '\n')


def unit_matches(tx, current):
    old = tx['unit_baseline']; expected = dict(old['files'])
    expected[str(DROPIN)] = hashlib.sha256(tx['dropin'].encode()).hexdigest()
    if current == old:
        return 'old'
    if (current['files'] == expected and current['path'] == PYTHON and
            current['argv'] == PYTHON + ' -I -B ' + str(CONTROL / 'indexa_auth_runtime.py') + ' ' + tx['policy_sha256']):
        return 'new'
    raise ValueError('foreign Indexa unit edit preserved; secure hold required')


def install_dropin(tx):
    # An existing foreign file, even if systemd has not loaded it yet, is never
    # overwritten. CAS with O_EXCL protects the first persistent unit writer.
    content = tx['dropin'].encode()
    if DROPIN.is_symlink():
        raise ValueError('foreign auth dropin symlink preserved')
    if DROPIN.exists():
        if DROPIN.read_bytes() != content:
            raise ValueError('foreign auth dropin preserved')
    else:
        if any(p.is_symlink() for p in [DROPIN.parent, *DROPIN.parent.parents]):
            raise ValueError('foreign auth dropin directory symlink')
        DROPIN.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=DROPIN.parent, prefix='.indexa-auth-', delete=False) as stream:
            temporary = Path(stream.name)
            try:
                os.fchmod(stream.fileno(), 0o600)
                stream.write(content); stream.flush(); os.fsync(stream.fileno())
                os.link(temporary, DROPIN)  # Atomic publication, refuses an existing writer.
                c.sync(DROPIN.parent)
            finally:
                temporary.unlink(missing_ok=True)
    e.run(['systemctl', '--user', 'daemon-reload'])


def readiness(record, value, plan, unit, client):
    metadata = {'sha': record['sha'], 'config_sha256': record['manifest']['config_sha256'],
                'control_sha256': record['manifest']['control_sha256'],
                'policy_sha256': a.fingerprint(value), 'config_inventory': plan['inventory'],
                'unit_sha256': a.fingerprint(unit), 'client_sha': client['sha'],
                'client_sha256': client['client_sha256'], 'binary_sha256': e.digest(e.BINARY)}
    e.atomic(e.BASE / 'indexa-auth-stage.json', metadata)
    try:
        proof = a.root_document(e.BASE / 'indexa-auth-readiness.json')
    except (OSError, ValueError):
        return False
    now = time.time()
    return (all(proof.get(k) == v for k, v in metadata.items())
            and all(proof.get(k) is True for k in ['external_consumers_fenced',
                    'effective_environment_verified', 'token_provenance_indexa', 'profile_tools_compatible'])
            and all(type(proof.get(k)) in [int, float] for k in ['checked_at', 'expires_at'])
            and now - 3600 <= proof['checked_at'] <= now < proof['expires_at'] <= proof['checked_at'] + 3600)


def document(name):
    path = e.BASE / name
    return json.loads(path.read_text()) if path.exists() else None


def no_other_pending():
    return not any((path).exists() for path in [e.BASE / 'pending.json', e.BASE / 'client-pending.json',
                      e.BASE.parent / 'hermes-deploy/pending.json'])


def finish(tx):
    pending = e.BASE / PENDING
    # Arming may have crashed before the first stop. Close the old process first.
    e.ctl('stop', a.UNIT)
    if module_hashes() != tx['control_sha256'] or tx['dropin'] != dropin_for_hash(tx['policy_sha256']):
        raise ValueError('auth control/journal changed during recovery; preserved')
    unit_matches(tx, unit_snapshot())
    # Make the boot path fail closed before calling this secure-hold. An arming
    # journal is also non-reversible; it must never resume legacy consumers.
    install_dropin(tx)
    if unit_matches(tx, unit_snapshot()) != 'new':
        raise ValueError('secure Indexa ExecStart not effective')
    tx['phase'] = 'secure-hold'; e.atomic(pending, tx)
    c.stop(tx['consumers'])
    # Offline recovery does not require an unexpired readiness attestation; the
    # admission and secure-hold were durable before changing consumers.
    value = a.policy(a.root_document(e.BASE / 'indexa-auth-policy.json'))
    if a.fingerprint(value) != tx['policy_sha256']:
        raise ValueError('auth policy/control changed during recovery; preserved')
    a.token(value['token_file'])
    current = document('client-last-success.json')
    if not current or current['sha'] != tx['client_sha'] or current['client_sha256'] != tx['client_sha256']:
        raise ValueError('foreign client version during auth recovery')
    c.verify_local(current)
    if e.digest(e.BINARY) != tx['binary_sha256']:
        raise ValueError('foreign bridge binary change preserved; secure hold required')
    runtime = c.release(current['sha']) / 'runtime'
    adapter(runtime, 'indexa_auth_config.py', 'apply', {'policy': value, 'plan': tx['plan']})
    e.ctl('start', a.UNIT)
    e.healthy(a.UNIT, 8080)
    result = adapter(runtime, 'indexa_auth_probe.py', 'after', {'policy': value})
    if result.get('ok') is not True or result.get('unauthenticated_status') != 401:
        raise ValueError('real auth/SDK proof failed')
    # Durable validated phase before resume permits crash recovery without
    # stopping a new turn which started after successful enforcement.
    tx['phase'] = 'validated'; tx['proof'] = result; e.atomic(pending, tx)
    complete_validated(tx, verified=True)


def complete_validated(tx, verified=False):
    value = a.policy(a.root_document(e.BASE / 'indexa-auth-policy.json'))
    if a.fingerprint(value) != tx['policy_sha256'] or unit_matches(tx, unit_snapshot()) != 'new':
        raise ValueError('validated auth refs changed; preserve secure hold')
    if module_hashes() != tx['control_sha256'] or e.digest(e.BINARY) != tx['binary_sha256']:
        raise ValueError('validated auth code changed; preserve secure hold')
    runtime, client = managed_runtime()
    if client['sha'] != tx['client_sha']:
        raise ValueError('validated client changed; preserve secure hold')
    # Read again before any resume after a crash; no config or credential undo.
    if not verified:
        if not e.active(a.UNIT):
            e.ctl('start', a.UNIT)
        e.healthy(a.UNIT, 8080)
        result = adapter(runtime, 'indexa_auth_probe.py', 'after', {'policy': value})
        if result.get('ok') is not True:
            raise ValueError('validated auth no longer healthy')
    c.resume(tx['consumers'])
    e.atomic(e.BASE / 'indexa-auth-last-success.json', tx)
    c.unlink(e.BASE / PENDING)
    e.notify('indexa-auth-success-' + tx['sha'], 'Indexa: autenticação REST ativada pelo pipeline oficial. 401 sem token, 200 autenticado, health conectado e leitura MCP comprovados. Sessão, bancos e credenciais atuais preservados.')


def recover():
    tx = document(PENDING)
    if not tx:
        return
    if not no_other_pending():
        raise ValueError('concurrent deployment journals require operator reconciliation')
    if tx['phase'] == 'staged':
        c.resume(tx['consumers']); c.unlink(e.BASE / PENDING)
    elif tx['phase'] == 'validated':
        complete_validated(tx)
    elif tx['phase'] in ['arming', 'secure-hold']:
        finish(tx)
    else:
        raise ValueError('unknown auth transaction phase')


def deploy(record):
    if not no_other_pending():
        return
    try:
        value = a.policy(a.root_document(e.BASE / 'indexa-auth-policy.json'))
    except (OSError, ValueError):
        return  # Missing operator approval is a stage, not an autonomy failure.
    if value['activation_sha'] != record['sha']:
        return
    a.token(value['token_file'])
    runtime, client = managed_runtime()
    plan = adapter(runtime, 'indexa_auth_config.py', 'plan', {'policy': value})
    unit = unit_snapshot()
    if unit['path'] != a.BINARY or unit['argv'] != a.BINARY or DROPIN.exists() or DROPIN.is_symlink():
        raise ValueError('initial Indexa unit baseline requires reconciliation')
    if not readiness(record, value, plan, unit, client) or not e.gateway_quiet():
        return
    # Demonstrate proposed clients possess the approved header and can read
    # through the actual immutable stdio SDK runner BEFORE enforcing the bridge.
    if adapter(runtime, 'indexa_auth_probe.py', 'before', {'policy': value}).get('ok') is not True:
        raise ValueError('consumer compatibility proof failed before enforce')
    tx = {'schema': 1, 'sha': record['sha'], 'run_id': record['run_id'], 'phase': 'staged',
          'policy_sha256': a.fingerprint(value), 'control_sha256': module_hashes(),
          'client_sha': client['sha'], 'client_sha256': client['client_sha256'],
          'binary_sha256': e.digest(e.BINARY),
          'unit_baseline': unit, 'dropin': dropin(value), 'plan': plan,
          'consumers': {u: e.active(u) for u in c.consumers()}}
    e.atomic(e.BASE / PENDING, tx)
    try:
        c.stop(tx['consumers'])
        if (a.fingerprint(a.policy(a.root_document(e.BASE / 'indexa-auth-policy.json'))) != tx['policy_sha256']
                or unit_snapshot() != unit or adapter(runtime, 'indexa_auth_config.py', 'plan', {'policy': value}) != plan
                or not readiness(record, value, plan, unit, client)):
            raise c.Deferred('auth inputs changed before secure hold')
        # Never remove/undo this phase on failure. It gates ALL other lanes.
        tx['phase'] = 'arming'; e.atomic(e.BASE / PENDING, tx)
        finish(tx)
    except c.Deferred:
        if tx['phase'] == 'staged':
            recover()
        else:
            raise
    except BaseException:
        if tx['phase'] == 'staged':
            recover()
        raise


def poll_safely():
    """Return False only while an admitted auth journal fences other lanes."""
    try:
        recover()
        if (e.BASE / PENDING).exists():
            return False
        workflow = e.api(f'repos/{a.REPOSITORY}/actions/workflows/indexa-auth-release.yml')
        if workflow.get('path') != a.WORKFLOW:
            raise ValueError('auth workflow identity mismatch')
        runs = e.api(f'repos/{a.REPOSITORY}/actions/workflows/{workflow["id"]}/runs?branch=main&per_page=1')['workflow_runs']
        if not runs or runs[0]['status'] != 'completed' or runs[0]['conclusion'] != 'success':
            return True
        candidate = runs[0]; sha = require_sha(candidate['head_sha'])
        if (document('indexa-auth-last-success.json') or {}).get('sha') == sha:
            return True
        run_id = int(candidate['id'])
        run = e.api(f'repos/{a.REPOSITORY}/actions/runs/{run_id}')
        admit_run(run, workflow['id'], sha, workflow=a.WORKFLOW); verify(sha)
        # Require current main's auth inputs; later unrelated commits are fine.
        e.run(['git', '-C', str(e.REPO), 'fetch', '--quiet', 'origin', 'main'])
        paths = ['ops/indexa-auth.json', a.WORKFLOW, *['scripts/maintenance/' + n for n in a.MODULES], 'scripts/maintenance/executor.py']
        if subprocess.run(['git', '-C', str(e.REPO), 'merge-base', '--is-ancestor', sha, 'origin/main'],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
            return True
        if subprocess.run(['git', '-C', str(e.REPO), 'diff', '--quiet', sha, 'origin/main', '--', *paths],
                          stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode != 0:
            return True
        record = stage(run_id, sha); deploy(record)
    except Exception as error:
        # No parser/network/subprocess body in disk receipts or notifications.
        e.atomic(e.BASE / 'indexa-auth-last-error.json', {'error_type': type(error).__name__})
        tx = document(PENDING)
        if tx and tx.get('phase') in ['arming', 'secure-hold', 'validated']:
            # Best-effort close even when a changed root policy or failed health
            # prevented recovery. Native guard still protects unrelated turns.
            try:
                e.ctl('stop', a.UNIT)
                c.stop(tx['consumers'])
            except Exception:
                pass
        try:
            e.notify('indexa-auth-blocked-' + (document(PENDING) or {}).get('sha', 'preflight'),
                     'Transição de autenticação Indexa aguardando pré-requisito ou recuperação. Nenhum segredo/dado foi restaurado. Após secure-hold, bridge/consumidores permanecem cercados até prova segura; demais filas aguardam esse journal.')
        except Exception:
            pass
    return not (e.BASE / PENDING).exists()
