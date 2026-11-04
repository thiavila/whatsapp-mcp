"""Client-only official release consumer; called under executor.lock, never standalone.

No bridge/binary/database/token changes. Root preflight attests external writers
and profile tool compatibility. Only command/args fields of recognized configs
are migrated; recovery keeps all current business state and unrelated config.
"""
import hashlib
import io
import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
import time
import zipfile
import re

import executor as e
from control import admit_run, require_sha
from release_ci import verify
from client_archive import digest, extract, verify_tree, sync_tree, MAX_ARCHIVE, MAX_EXPANDED

WORKFLOW = '.github/workflows/client-release.yml'
ARTIFACT = 'mcp-client-release'
PROFILES = ['default', 'indexa-roberto', 'melancia-lismeri', 'melancia-manager',
            'melancia-roberto', 'memo-vic', 'revella-carol', 'whats-monica']
VERSIONS = {'python': '3.11.18', 'uv': '0.13.5', 'certifi': '2026.10.19', 'pip': '26.2.2'}
LEGACY = '/home/thiago/repos/whatsapp-mcp/whatsapp-mcp-server'
DASHBOARD = 'hermes-dashboard.service'
_candidate_sha = None


class Deferred(Exception):
    """No config change was made; preserve autonomy and retry on a later tick."""


def release(sha):
    return e.BASE / 'client-releases' / require_sha(sha)


def command(action, unit):
    if unit == DASHBOARD:
        return e.run(['systemctl', '--user', action, unit], timeout=60)
    return e.ctl(action, unit)


def consumers():
    return [*e.WATCH_TIMERS, 'hermes-gateway.service', DASHBOARD]


def resume(states):
    for unit in ['hermes-gateway.service', DASHBOARD, *e.WATCH_TIMERS]:
        if states.get(unit):
            command('start', unit)


def unlink(path):
    path.unlink()
    fd = os.open(path.parent, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def config(runtime, action, document=None):
    args = [str(runtime / 'python/bin/python3'), '-I', '-B', str(runtime / 'config.py'), action, str(e.HERMES)]
    if action in ['plan', 'managed']:
        args.append(str(runtime / 'run-client'))
    result = subprocess.run(args, input=json.dumps(document).encode() if document else b'',
                            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                            env={'PATH': '/usr/bin:/bin', 'HOME': '/nonexistent',
                                 'LANG': 'C.UTF-8', 'PYTHONDONTWRITEBYTECODE': '1'},
                            timeout=30, check=True)
    return json.loads(result.stdout) if action in ['plan', 'status', 'managed'] else None


def artifact(run_id, sha, destination):
    records = e.api(f'repos/{e.REPOSITORY}/actions/runs/{int(run_id)}/artifacts')['artifacts']
    found = [r for r in records if r['name'] == ARTIFACT and not r['expired']]
    if len(found) != 1 or not 0 < found[0]['size_in_bytes'] < MAX_ARCHIVE + 1024**2:
        raise ValueError('one bounded client artifact required')
    record = found[0]
    raw = subprocess.check_output(['gh', 'api',
        f'repos/{e.REPOSITORY}/actions/artifacts/{int(record["id"])}/zip'], timeout=120)
    if len(raw) > MAX_ARCHIVE + 1024**2 or record.get('digest') != 'sha256:' + hashlib.sha256(raw).hexdigest():
        raise ValueError('client GitHub artifact digest mismatch')
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if sorted(archive.namelist()) != ['client.tar.gz', 'manifest.json']:
            raise ValueError('unexpected client artifact members')
        if archive.getinfo('manifest.json').file_size > 65536 or archive.getinfo('client.tar.gz').file_size > MAX_ARCHIVE:
            raise ValueError('expanded client zip size')
        manifest = json.loads(archive.read('manifest.json'))
        payload = archive.read('client.tar.gz')
    expected = {'schema', 'repository', 'sha', 'versions', 'lock_sha256', 'source_sha256', 'client_sha256'}
    if (set(manifest) != expected or manifest['schema'] != 1
            or manifest['repository'] != e.REPOSITORY or manifest['sha'] != require_sha(sha)
            or manifest['versions'] != VERSIONS
            or manifest['client_sha256'] != hashlib.sha256(payload).hexdigest()):
        raise ValueError('client manifest mismatch')
    if destination.exists() and digest(destination) != manifest['client_sha256']:
        raise ValueError('existing client payload differs; preserved')
    if not destination.exists():
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.client-', delete=False) as out:
            temporary = Path(out.name)
            try:
                os.fchmod(out.fileno(), 0o600)
                out.write(payload); out.flush(); os.fsync(out.fileno())
                os.replace(temporary, destination)
                sync(destination.parent)
            finally:
                temporary.unlink(missing_ok=True)
    return manifest


def preflight(runtime):
    # Isolated user/mount/net namespace; no HOME, profiles, credentials or stores.
    # Runtime files read-only, Python/uv same paths; only synthetic /tmp writable.
    args = ['setpriv', '--no-new-privs', 'bwrap', '--unshare-all', '--die-with-parent', '--new-session',
            '--tmpfs', '/tmp', '--ro-bind', str(runtime), str(runtime), '--proc', '/proc', '--dev', '/dev',
            '--dir', '/nonexistent', '--chdir', '/tmp',
            '--clearenv', '--setenv', 'HOME', '/nonexistent', '--setenv', 'PATH', '/usr/bin:/bin',
            '--setenv', 'LANG', 'C.UTF-8']
    for directory in ['/usr', '/lib', '/lib64', '/bin']:
        if Path(directory).exists():
            args += ['--ro-bind', directory, directory]
    args += [str(runtime / 'python/bin/python3'), '-I', '-B', str(runtime / 'canary.py'), str(runtime)]
    result = json.loads(e.run(args, timeout=75))
    if result.get('ok') is not True or result.get('versions') != VERSIONS:
        raise ValueError('isolated client canary failed')
    return result


def root_document(name):
    try:
        fd = os.open(e.BASE / name, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_mode & 0o022
                or st.st_nlink != 1 or st.st_size > 65536):
            return None
        value = json.loads(os.read(fd, 65537))
        return value if isinstance(value, dict) else None
    except (ValueError, OSError):
        return None
    finally:
        os.close(fd)


def readiness(sha, manifest, plan):
    proof = root_document('client-readiness.json'); now = time.time()
    if not proof:
        return False
    if any(type(proof.get(k)) not in [int, float] for k in ['checked_at', 'expires_at']):
        return False
    if (proof.get('sha') != sha or proof.get('client_sha256') != manifest['client_sha256']
            or proof.get('config_inventory') != plan['inventory']
            or set(proof.get('config_inventory', {})) != set(PROFILES)
            or proof.get('external_consumers_fenced') is not True
            or proof.get('profile_tools_compatible') is not True
            or not now - 3600 <= proof.get('checked_at', 0) <= now
            or not now < proof.get('expires_at', 0) <= proof['checked_at'] + 3600):
        return False
    return True


def activation(sha, manifest, plan, expected):
    """First migration needs fresh proof; already-managed updates stay autonomous."""
    policy = root_document('client-policy.json') or {}
    if (policy.get('schema') != 1 or policy.get('enabled') is not True
            or policy.get('repository') != e.REPOSITORY or policy.get('workflow') != WORKFLOW
            or policy.get('allow_automatic_managed_updates') is not True):
        return None
    if not re.fullmatch('[0-9a-f]{40}', policy.get('activation_sha', '')):
        return None
    activation_sha = policy['activation_sha']
    previous = last_success()
    if previous and previous.get('activation_sha') == activation_sha:
        verify_local(previous)
        old = release(previous['sha']) / 'runtime'
        runner = str(old / 'run-client')
        if (all(change['old'] == {'command': runner, 'args': []}
                for record in plan['records'] for change in record['changes'])
                and config(old, 'managed').get('all_managed') is True and live(old, expected)):
            return activation_sha
    if activation_sha == sha and not (e.REPO / 'scripts/client/pins.json').exists() and readiness(sha, manifest, plan):
        return activation_sha
    return None


def legacy_change(plan):
    return any(change['old']['command'] in ['uv', '/home/thiago/.local/bin/uv']
               for record in plan.get('records', []) for change in record['changes'])


def source_fingerprint():
    result = {}
    for name in ['.python-version', 'pyproject.toml', 'uv.lock']:
        path = e.REPO / 'whatsapp-mcp-server' / name
        if path.is_symlink():
            raise ValueError('legacy source input symlink unsupported')
        result[name] = digest(path)
    return result


def processes():
    records = []
    for path in Path('/proc').iterdir():
        if not path.name.isdigit():
            continue
        try:
            args = (path / 'cmdline').read_bytes().split(b'\0')
            # readlink retains '(deleted)' mapped executables; strict resolve
            # would silently hide precisely the stale children we must fence.
            exe = Path(os.readlink(path / 'exe'))
            if not any(LEGACY.encode() in a or b'/mcp-deploy/client-releases/' in a for a in args):
                continue
            if 'python' not in exe.name and not exe.name.startswith('uv'):
                continue
            group = (path / 'cgroup').read_text()
            if not any(unit in group for unit in ['hermes-gateway.service', DASHBOARD]):
                raise ValueError('unmanaged WhatsApp client process; externally fence first')
            records.append((int(path.name), str(exe)))
        except PermissionError:
            try:
                if path.stat().st_uid == os.getuid():
                    raise ValueError('cannot inspect same-user runtime process')
            except FileNotFoundError:
                continue
        except (FileNotFoundError, ProcessLookupError):
            continue
    return records


def mapped(runtime, records=None):
    records = processes() if records is None else records
    python = str((runtime / 'python/bin/python3').resolve())
    uv = str((runtime / 'uv').resolve())
    try:
        return all(exe in [python, uv] and digest(Path('/proc') / str(pid) / 'exe') == digest(exe)
                   for pid, exe in records)
    except OSError:
        return False


def live(runtime, expected):
    records = processes()
    python = str((runtime / 'python/bin/python3').resolve())
    return (gateway_count([(pid, exe) for pid, exe in records if exe == python]) >= expected > 0
            and mapped(runtime, records))


def gateway_count(records):
    count = 0
    for pid, exe in records:
        try:
            if 'python' in Path(exe).name and 'hermes-gateway.service' in (Path('/proc') / str(pid) / 'cgroup').read_text():
                count += 1
        except FileNotFoundError:
            pass
    return count


def stop(states):
    for unit in e.WATCH_TIMERS:
        command('stop', unit)
    if any(e.busy_service(unit) for unit in e.WATCH_SERVICES):
        raise Deferred('watcher active; client cutover deferred')
    try:
        command('stop', 'hermes-gateway.service')  # Native live-work refusal retained.
    except subprocess.CalledProcessError:
        if e.active('hermes-gateway.service'):
            raise Deferred('native live-work stop refused') from None
        raise
    command('stop', DASHBOARD)
    if any(e.busy_service(unit) for unit in e.WATCH_SERVICES):
        raise Deferred('watcher started while stopping consumers')
    if processes():
        raise ValueError('old client children remain; no configuration changed')


def admitted(run_id, sha):
    workflow = e.api(f'repos/{e.REPOSITORY}/actions/workflows/client-release.yml')
    if workflow.get('path') != WORKFLOW:
        raise ValueError('client workflow identity mismatch')
    run = e.api(f'repos/{e.REPOSITORY}/actions/runs/{int(run_id)}')
    admit_run(run, workflow['id'], sha, workflow=WORKFLOW)
    verify(sha)


def sync(directory):
    fd = os.open(directory, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)


def last_success():
    path = e.BASE / 'client-last-success.json'
    return json.loads(path.read_text()) if path.exists() else None


def verify_local(tx):
    folder = release(tx['sha'])
    if digest(folder / 'client.tar.gz') != tx['client_sha256']:
        raise ValueError('local client recovery payload changed')
    verify_tree(folder / 'client.tar.gz', folder / 'runtime')


def success(tx):
    history = e.BASE / 'client-successes'; history.mkdir(mode=0o700, exist_ok=True)
    e.atomic(history / (tx['sha'] + '.json'), tx)
    e.atomic(e.BASE / 'client-last-success.json', tx)
    pending = e.BASE / 'client-pending.json'
    if pending.exists():
        unlink(pending)
    e.notify('client-success-' + tx['sha'], 'Clientes MCP atualizados pelo pipeline oficial: Python/TLS e runner congelados. Bridges, sessões, bancos e credenciais preservados.')


def recover():
    pending = e.BASE / 'client-pending.json'
    if not pending.exists():
        return
    tx = json.loads(pending.read_text()); runtime = release(tx['sha']) / 'runtime'
    if legacy_change(tx.get('plan', {})) and source_fingerprint() != tx.get('legacy_inputs'):
        raise ValueError('foreign legacy source change; keep recovery pending, never expose unfrozen uv')
    if tx.get('phase') == 'staged':
        # No configuration writer has run, even if the native stop refused.
        resume(tx['consumers']); unlink(pending)
        return 'deferred'
    # Recovery is completely offline. Admission was durable before any stop.
    verify_local(tx)
    status = config(runtime, 'status', tx['plan'])
    if status.get('old_commands') is True:
        resume(tx['consumers']); unlink(pending)
        return 'deferred'
    if (status.get('new_commands') is True
            and live(runtime, tx['expected_clients']) and e.active('hermes-gateway.service')):
        resume(tx['consumers'])
        success(tx)
        return 'completed'
    try:
        stop(tx['consumers'])
    except Deferred:
        resume(tx['consumers'])
        return 'recovery-deferred'  # Keep journal, let active work finish, retry later.
    config(runtime, 'rollback', tx['plan'])
    resume(tx['consumers'])
    previous = tx.get('previous_success_sha')
    current = last_success()
    if current and current['sha'] == tx['sha']:
        if previous:
            old = json.loads((e.BASE / 'client-successes' / (require_sha(previous) + '.json')).read_text())
            e.atomic(e.BASE / 'client-last-success.json', old)
        else:
            unlink(e.BASE / 'client-last-success.json')
    e.atomic(e.BASE / 'client-last-rollback.json', tx)
    e.atomic(e.BASE / 'client-blocked.json', {'sha': tx['sha']})
    unlink(pending)
    e.notify('client-rollback-' + tx['sha'], 'Atualização dos clientes MCP revertida. Comandos anteriores e consumidores retomados; dados, sessões e credenciais atuais preservados. Candidato bloqueado até correção.')
    return 'rolled-back'


def current_source(sha):
    if subprocess.run(['git', '-C', str(e.REPO), 'merge-base', '--is-ancestor', sha, 'origin/main'],
                      stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode:
        return False
    paths = ['whatsapp-mcp-server', 'scripts/client', 'scripts/maintenance/client_executor.py',
             'scripts/maintenance/client_archive.py', '.github/workflows/client-release.yml', '.github/apparmor']
    return subprocess.run(['git', '-C', str(e.REPO), 'diff', '--quiet', sha, 'origin/main', '--', *paths],
                          stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode == 0


def stage(run_id, sha):
    target = release(sha); target.mkdir(mode=0o700, parents=True, exist_ok=True)
    runtime = target / 'runtime'; receipt = target / 'stage.json'
    if receipt.exists():
        cached = json.loads(receipt.read_text())
        if cached.get('sha') != sha or cached.get('canary', {}).get('ok') is not True:
            raise ValueError('invalid cached client stage')
        verify_local(cached)
        return cached
    manifest = artifact(run_id, sha, target / 'client.tar.gz')
    if shutil.disk_usage(e.BASE).free < 2 * MAX_EXPANDED + 1024**3:
        raise ValueError('insufficient space for isolated client runtime')
    if not runtime.exists():
        temporary = Path(tempfile.mkdtemp(prefix='.runtime-', dir=target))
        temporary.rmdir()  # extract requires a new destination.
        try:
            extract(target / 'client.tar.gz', temporary)
            verify_tree(target / 'client.tar.gz', temporary)
            sync_tree(temporary)
            os.rename(temporary, runtime); sync(target)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
    verify_tree(target / 'client.tar.gz', runtime)
    if json.loads((runtime / 'runtime.json').read_text()) != {k: v for k, v in manifest.items() if k != 'client_sha256'}:
        raise ValueError('inner client manifest mismatch')
    canary = preflight(runtime)
    cached = {'sha': sha, 'run_id': run_id, 'client_sha256': manifest['client_sha256'],
              'manifest': manifest, 'canary': canary}
    e.atomic(receipt, cached)
    return cached


def source_ready(sha):
    """Go must not expose new Python pins to an unfrozen legacy uv runner."""
    require_sha(sha)
    exists = bool(subprocess.check_output(['git', '-C', str(e.REPO), 'ls-tree', '--name-only',
                                          sha, '--', 'scripts/client/pins.json'], stderr=subprocess.PIPE))
    changed = subprocess.run(['git', '-C', str(e.REPO), 'diff', '--quiet', 'HEAD', sha, '--',
                              'whatsapp-mcp-server/.python-version', 'whatsapp-mcp-server/pyproject.toml',
                              'whatsapp-mcp-server/uv.lock'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL).returncode
    if changed not in [0, 1]:
        raise ValueError('cannot compare client inputs before Go source fast-forward')
    if not exists and changed == 0:
        return True  # Pre-client source; backward compatible Go controller.
    if (e.BASE / 'client-pending.json').exists():
        return False
    previous = last_success()
    if not previous:
        return False
    verify_local(previous)
    runtime = release(previous['sha']) / 'runtime'
    return (config(runtime, 'managed').get('all_managed') is True
            and mapped(runtime))


def deploy(run_id, sha):
    require_sha(sha)
    e.run(['git', '-C', str(e.REPO), 'fetch', '--quiet', 'origin', 'main'])
    if not current_source(sha):
        return
    staged = stage(run_id, sha); manifest = staged['manifest']; runtime = release(sha) / 'runtime'
    plan = config(runtime, 'plan')
    e.atomic(e.BASE / 'client-stage.json', {'sha': sha, 'client_sha256': manifest['client_sha256'],
             'config_inventory': plan['inventory'], 'versions': VERSIONS})
    if not e.gateway_quiet() or any(e.busy_service(u) for u in e.WATCH_SERVICES):
        return
    expected = gateway_count(processes())
    if not expected:
        return  # Stage remains reviewable; no eager-start assumption/stop on zero baseline.
    activation_sha = activation(sha, manifest, plan, expected)
    if not activation_sha:
        return
    if not e.gateway_quiet():
        return
    previous = last_success()
    tx = {'schema': 1, 'phase': 'staged', 'sha': sha, 'run_id': staged['run_id'],
          'client_sha256': manifest['client_sha256'], 'activation_sha': activation_sha,
          'previous_success_sha': previous['sha'] if previous else None,
          'expected_clients': expected, 'plan': plan,
          'legacy_inputs': source_fingerprint() if legacy_change(plan) else None,
          'consumers': {u: e.active(u) for u in consumers()}}
    e.atomic(e.BASE / 'client-pending.json', tx)
    try:
        if activation(sha, manifest, plan, expected) != activation_sha:
            raise Deferred('activation changed during staging')
        stop(tx['consumers'])
        if legacy_change(plan) and source_fingerprint() != tx['legacy_inputs']:
            raise ValueError('legacy source changed before config apply')
        tx['phase'] = 'applying'; e.atomic(e.BASE / 'client-pending.json', tx)
        config(runtime, 'apply', plan)
        tx['phase'] = 'applied'; e.atomic(e.BASE / 'client-pending.json', tx)
        resume(tx['consumers'])
        for _ in range(45):
            if e.active('hermes-gateway.service') and live(runtime, expected):
                break
            time.sleep(2)
        else:
            raise ValueError('new pinned client processes did not resume')
        success(tx)
        prune(plan, sha)
    except Deferred:
        recover()
        return
    except BaseException:
        recover()
        raise


def poll():
    global _candidate_sha
    pending = e.BASE / 'client-pending.json'
    _candidate_sha = json.loads(pending.read_text()).get('sha') if pending.exists() else None
    recover()
    if pending.exists():
        return
    workflow = e.api(f'repos/{e.REPOSITORY}/actions/workflows/client-release.yml')
    if workflow.get('path') != WORKFLOW:
        raise ValueError('client workflow identity mismatch')
    runs = e.api(f'repos/{e.REPOSITORY}/actions/workflows/{workflow["id"]}/runs?branch=main&per_page=1')['workflow_runs']
    if not runs or runs[0]['status'] != 'completed' or runs[0]['conclusion'] != 'success':
        return
    sha = require_sha(runs[0]['head_sha'])
    _candidate_sha = sha
    for name in ['client-last-success.json', 'client-blocked.json']:
        path = e.BASE / name
        if path.exists() and json.loads(path.read_text()).get('sha') == sha:
            return
    run_id = int(runs[0]['id']); admitted(run_id, sha)
    deploy(run_id, sha)


def poll_safely():
    try:
        poll()
    except Exception as error:
        # A client blocker must not disable autonomous Hermes/Go maintenance.
        # Go has an independent source_ready guard before exposing Python pins.
        message = str(error)[:512] if isinstance(error, (ValueError, Deferred)) else 'private operational diagnostic suppressed'
        e.atomic(e.BASE / 'client-last-error.json', {'type': type(error).__name__, 'message': message,
                 'sha': _candidate_sha, 'checked_at': time.time()})
        try:
            pending = e.BASE / 'client-pending.json'
            key = json.loads(pending.read_text()).get('sha') if pending.exists() else _candidate_sha
            fingerprint = hashlib.sha256((str(key) + type(error).__name__ + message).encode()).hexdigest()[:24]
            e.notify('client-error-' + fingerprint,
                     'Manutenção do cliente MCP bloqueada ou em recuperação. Dados/credenciais preservados; nenhuma restauração antiga. Vou manter o gate seguro e aguardar correção, sem interromper a fila independente das bridges.')
        except Exception:
            pass


def prune(plan, current):
    """Bound public runtime retention; never delete referenced or modified trees."""
    folder = e.BASE / 'client-releases'
    protected = {current, *plan.get('references', [])}
    done = last_success()
    if done:
        protected.update(v for v in [done['sha'], done.get('previous_success_sha')] if v)
    try:
        for _, exe in processes():
            match = re.search(r'/client-releases/([0-9a-f]{40})/', exe)
            if match: protected.add(match[1])
    except ValueError:
        return  # An external client must be reconciled before any retention change.
    candidates = sorted([p for p in folder.iterdir() if re.fullmatch('[0-9a-f]{40}', p.name)
                         and p.is_dir() and not p.is_symlink()], key=lambda p: p.stat().st_mtime, reverse=True)
    protected.update(p.name for p in candidates[:3])
    for path in candidates:
        if path.name in protected:
            continue
        try:
            document = json.loads((path / 'stage.json').read_text())
            if document['sha'] != path.name:
                continue
            verify_local(document)
            shutil.rmtree(path)
        except (OSError, ValueError, KeyError):
            pass  # Preserve foreign/incomplete trees for explicit operator review.
