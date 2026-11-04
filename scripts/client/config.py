"""Patch only recognized MCP command/args spans. Never serialize credentials.

Invoked by the executor with packaged Python/PyYAML. Journal/output contain
command fields and hashes only; .env/auth/SQLite are never read here.
"""
import hashlib
import copy
import json
import os
from pathlib import Path
import re
import sys
import tempfile

import yaml

PROFILES = ['default', 'indexa-roberto', 'melancia-lismeri', 'melancia-manager',
            'melancia-roberto', 'memo-vic', 'revella-carol', 'whats-monica']
LEGACY = '/home/thiago/repos/whatsapp-mcp/whatsapp-mcp-server'
RELEASES = '/home/thiago/hermes-maintenance/mcp-deploy/client-releases/'


def read(path):
    return path.read_bytes().decode('utf-8')  # Preserve CRLF and all unrelated bytes.


def candidate(name, config):
    return bool(('whatsapp' in name.lower() and config.get('command')) or any(
        'whatsapp-mcp' in str(config.get(field, '')) or RELEASES in str(config.get(field, ''))
        for field in ['command', 'args', 'cwd']))


def sha(data):
    return hashlib.sha256(data.encode()).hexdigest()


def mapping(node):
    if not isinstance(node, yaml.MappingNode):
        raise ValueError('expected unique YAML mapping')
    result = {}
    for key, value in node.value:
        if key.value in result:
            raise ValueError('duplicate YAML key')
        result[key.value] = value
    return result


def parse(text):
    # No constructors, interpolation or secret loaders. Require unique keys at
    # every level before safe_load so YAML aliases/duplicates cannot hide writers.
    root = yaml.compose(text)
    if root is None:
        return {}, {}
    seen = set()
    def validate(node):
        if id(node) in seen:
            raise ValueError('YAML alias unsupported in managed MCP config')
        seen.add(id(node))
        if isinstance(node, yaml.MappingNode):
            mapping(node)
            for key, value in node.value:
                validate(key); validate(value)
        elif isinstance(node, yaml.SequenceNode):
            for child in node.value:
                validate(child)
    validate(root)
    top = mapping(root)
    servers = (mapping(top['mcp_servers']) if 'mcp_servers' in top
               and top['mcp_servers'].tag != 'tag:yaml.org,2002:null' else {})
    value = yaml.safe_load(text)
    return value.get('mcp_servers') or {}, {name: mapping(node) for name, node in servers.items()}


def recognized(command, args):
    if re.fullmatch(re.escape(RELEASES) + '[0-9a-f]{40}/runtime/run-client', command or ''):
        return args == []
    return command in ['uv', '/home/thiago/.local/bin/uv'] and args in [
        ['--directory', LEGACY, 'run', 'main.py'],
        ['--directory', LEGACY, 'run', 'python', 'main.py'],
        ['run', '--directory', LEGACY, 'main.py'],
    ]


def replace(text, nodes, values):
    edits = []
    for field in ['command', 'args']:
        node = nodes[field]
        original = text[node.start_mark.index:node.end_mark.index]
        new = json.dumps(values[field], ensure_ascii=False)
        if '\n' in original:
            new += '\n' + ' ' * node.end_mark.column
        edits.append((node.start_mark.index, node.end_mark.index, new))
    for start, end, new in sorted(edits, reverse=True):
        text = text[:start] + new + text[end:]
    return text


def config_path(home, profile):
    if profile not in PROFILES:
        raise ValueError('unknown runtime profile')
    path = home / 'config.yaml' if profile == 'default' else home / 'profiles' / profile / 'config.yaml'
    for parent in [path, *path.parents]:
        if parent == home.parent:
            break
        if parent.is_symlink():
            raise ValueError('symlinked runtime config unsupported')
    return path


def plan(home, runner):
    if not re.fullmatch(re.escape(RELEASES) + '[0-9a-f]{40}/runtime/run-client', runner):
        raise ValueError('runner outside admitted client releases')
    present = {p.name for p in (home / 'profiles').iterdir() if p.is_dir()}
    if present != set(PROFILES[1:]):
        raise ValueError('profile inventory changed; review client admission')
    records = []
    inventory = {}
    references = set()
    for profile in PROFILES:
        path = config_path(home, profile)
        if not path.is_file():
            inventory[profile] = None
            continue
        text = read(path); servers, nodes = parse(text)
        inventory[profile] = sha(text)
        changes = []
        after = text
        for name, config in servers.items():
            if not isinstance(config, dict):
                raise ValueError('MCP entry must be a mapping')
            command, args = config.get('command'), config.get('args', [])
            if not candidate(name, config):
                continue
            if not recognized(command, args) or 'args' not in nodes[name]:
                raise ValueError('unrecognized WhatsApp runner; review metadata-only plan')
            if command.startswith(RELEASES):
                references.add(Path(command).parent.parent.name)
            new = {'command': runner, 'args': []}
            if config.get('command') == runner and args == []:
                continue
            changes.append({'server': name, 'old': {'command': command, 'args': args}, 'new': new})
        # Reparse after each splice; indexes change. No unrelated bytes change.
        for change in changes:
            _, current_nodes = parse(after)
            after = replace(after, current_nodes[change['server']], change['new'])
        if changes:
            # Verify semantics of fields untouched by byte-splicing as well.
            updated, _ = parse(after)
            expected = copy.deepcopy(servers)
            for change in changes:
                expected[change['server']].update(change['new'])
            if updated != expected:
                raise ValueError('config splice changed unrelated values')
            whole = yaml.safe_load(text)
            whole['mcp_servers'] = expected
            if yaml.safe_load(after) != whole:
                raise ValueError('config splice changed unrelated document')
            records.append({'profile': profile, 'before': sha(text), 'after': sha(after), 'changes': changes})
    if not records:
        raise ValueError('no recognized client configuration requires migration')
    return {'inventory': inventory, 'records': records, 'references': sorted(references)}


def managed(home, runner):
    if {p.name for p in (home / 'profiles').iterdir() if p.is_dir()} != set(PROFILES[1:]):
        return {'all_managed': False, 'references': []}
    count = 0; ok = True; references = set()
    for profile in PROFILES:
        path = config_path(home, profile)
        if not path.is_file():
            continue
        servers, _ = parse(read(path))
        for name, config in servers.items():
            if not candidate(name, config):
                continue
            count += 1
            command, args = config.get('command'), config.get('args', [])
            ok = ok and command == runner and args == []
            if recognized(command, args) and command.startswith(RELEASES):
                references.add(Path(command).parent.parent.name)
    return {'all_managed': ok and count > 0, 'servers': count, 'references': sorted(references)}


def write(path, text, expected):
    if path.is_symlink() or sha(read(path)) != expected:
        raise ValueError('foreign config edit before replacement; preserved')
    mode = path.stat().st_mode & 0o777
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.mcp-client-', delete=False) as out:
        temporary = Path(out.name)
        try:
            os.fchmod(out.fileno(), mode)
            out.write(text); out.flush(); os.fsync(out.fileno())
            if path.is_symlink() or sha(read(path)) != expected:
                raise ValueError('foreign config edit before replacement; preserved')
            os.replace(temporary, path)
            fd = os.open(path.parent, os.O_DIRECTORY)
            try: os.fsync(fd)
            finally: os.close(fd)
        finally:
            temporary.unlink(missing_ok=True)


def update(home, document, rollback=False):
    if not rollback:
        present = {p.name for p in (home / 'profiles').iterdir() if p.is_dir()}
        if present != set(PROFILES[1:]):
            raise ValueError('profile inventory changed after staging')
        modified = {r['profile']: r for r in document['records']}
        for profile, before in document['inventory'].items():
            path = config_path(home, profile)
            current = sha(read(path)) if path.is_file() else None
            allowed = [before]
            if profile in modified:
                allowed.append(modified[profile]['after'])
            if current not in allowed:
                raise ValueError('profile configuration changed after staging')
    writes = []
    for record in document['records']:
        path = config_path(home, record['profile'])
        text = read(path); original_sha = sha(text)
        if not rollback and sha(text) == record['after']:
            continue
        if not rollback and sha(text) != record['before']:
            raise ValueError('foreign config edit; preserved')
        for change in record['changes']:
            if any(not recognized(v.get('command'), v.get('args')) for v in [change['old'], change['new']]):
                raise ValueError('unrecognized journal command')
            servers, nodes = parse(text)
            config = servers.get(change['server'], {})
            fields = {key: config.get(key) for key in ['command', 'args']}
            old, new = (change['new'], change['old']) if rollback else (change['old'], change['new'])
            if fields == new:
                continue
            if fields != old:
                raise ValueError('foreign command edit; preserved')
            before = yaml.safe_load(text)
            before['mcp_servers'][change['server']].update(new)
            text = replace(text, nodes[change['server']], new)
            if yaml.safe_load(text) != before:
                raise ValueError('rollback splice changed unrelated document')
        if not rollback and sha(text) != record['after']:
            raise ValueError('config result hash mismatch')
        writes.append((path, text, original_sha))
    # Validate all conflicts before making any write.
    for path, text, expected in writes:
        write(path, text, expected)


def status(home, document):
    old = new = total = 0
    for record in document['records']:
        servers, _ = parse(read(config_path(home, record['profile'])))
        for change in record['changes']:
            current = servers.get(change['server'], {})
            total += 1
            new += all(current.get(k) == v for k, v in change['new'].items())
            old += all(current.get(k) == v for k, v in change['old'].items())
    return {'new_commands': new == total > 0, 'old_commands': old == total > 0,
            'changed': new, 'foreign': total - new - old}


def main():
    action, home = sys.argv[1], Path(sys.argv[2])
    if action == 'plan':
        print(json.dumps(plan(home, sys.argv[3]), sort_keys=True))
    elif action == 'managed':
        print(json.dumps(managed(home, sys.argv[3]), sort_keys=True))
    elif action in ['apply', 'rollback']:
        update(home, json.load(sys.stdin), rollback=action == 'rollback')
    elif action == 'status':
        print(json.dumps(status(home, json.load(sys.stdin))))
    else:
        raise SystemExit('unsupported config action')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        # YAML parser diagnostics may quote a line containing credentials.
        # Keep all raw parser/subprocess diagnostics inside the process.
        raise SystemExit('Client config adapter failed: ' + type(error).__name__) from None
