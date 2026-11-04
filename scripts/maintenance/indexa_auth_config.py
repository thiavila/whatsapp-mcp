"""Packaged-PyYAML adapter. Patch only five approved Indexa env fields.

All other bytes remain unchanged. Journals contain safe refs/presence, never
inline credentials or complete env mappings. Recovery rolls forward only.
"""
import json
from pathlib import Path
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indexa_auth_common as a


def endpoint(env):
    port = env.get('WHATSAPP_BRIDGE_PORT') or '8080'
    url = env.get('WHATSAPP_API_BASE_URL') or 'http://localhost:' + str(port) + '/api'
    parsed = urlsplit(url)
    if (parsed.scheme != 'http' or parsed.hostname not in ['localhost', '127.0.0.1', '::1']
            or parsed.username or parsed.password or parsed.query or parsed.fragment
            or parsed.path.rstrip('/') != '/api' or parsed.port not in [8080, 8083, 8084]):
        raise ValueError('unrecognized bridge endpoint; reconcile consumers')
    return parsed.port


def old_fields(env, new):
    result = {}
    allowed = {'WHATSAPP_BRIDGE_TOKEN': [''], 'WHATSAPP_BRIDGE_TOKEN_FILE': ['', new['WHATSAPP_BRIDGE_TOKEN_FILE']],
               'WHATSAPP_BRIDGE_PORT': ['', '8080'], 'UV_NO_ENV_FILE': ['', 'true', 'false'],
               'WHATSAPP_API_BASE_URL': ['', 'http://localhost:8080/api', 'http://127.0.0.1:8080/api']}
    for key in new:
        if key in env and (not isinstance(env[key], str) or env[key] not in allowed[key]):
            raise ValueError('existing auth setting requires explicit reconciliation')
        result[key] = {'present': key in env, 'value': env.get(key)}
    return result


def splice(text, server, values, c):
    """Insert at mapping starts or replace scalar spans; no YAML round-trip."""
    newline = '\r\n' if '\r\n' in text else '\n'
    for key, value in values.items():
        servers, nodes = c.parse(text)
        fields = nodes[server]
        expected = c.yaml.safe_load(text)
        entry = expected['mcp_servers'][server]
        if entry.get('env') is None:
            entry['env'] = {}
        entry['env'][key] = value
        rendered = json.dumps(value, ensure_ascii=False)
        if 'env' not in fields:
            node = c.mapping(c.mapping(c.yaml.compose(text))['mcp_servers'])[server]
            if node.flow_style:
                start = node.start_mark.index + 1
                addition = '"env": {' + json.dumps(key) + ': ' + rendered + '}, '
            else:
                start = node.start_mark.index
                addition = 'env: {' + json.dumps(key) + ': ' + rendered + '}' + newline + ' ' * node.start_mark.column
            text = text[:start] + addition + text[start:]
        else:
            node = fields['env']
            if node.tag == 'tag:yaml.org,2002:null':
                text = text[:node.start_mark.index] + '{' + json.dumps(key) + ': ' + rendered + '}' + text[node.end_mark.index:]
            else:
                envnodes = c.mapping(node)
                if key in envnodes:
                    item = envnodes[key]
                    original = text[item.start_mark.index:item.end_mark.index]
                    if '\n' in original:
                        rendered += newline + ' ' * item.end_mark.column
                    text = text[:item.start_mark.index] + rendered + text[item.end_mark.index:]
                else:
                    start = node.start_mark.index + (1 if node.flow_style else 0)
                    addition = json.dumps(key) + ': ' + rendered
                    addition += (', ' if node.value else '') if node.flow_style else newline + ' ' * node.start_mark.column
                    text = text[:start] + addition + text[start:]
        if c.yaml.safe_load(text) != expected:
            raise ValueError('auth splice changed unrelated config')
    return text


def inventory(home, value, runtime, c):
    authorized = {(t['profile'], t['server']) for t in value['consumers']}
    if any(p not in c.PROFILES for p, _ in authorized):
        raise ValueError('unknown authorized profile')
    if {p.name for p in (home / 'profiles').iterdir() if p.is_dir()} != set(c.PROFILES[1:]):
        raise ValueError('profile inventory changed')
    hashes = {}; targets = []; records = []
    new = a.client_fields(value['token_file'])
    for profile in c.PROFILES:
        path = c.config_path(home, profile)
        if not path.is_file():
            hashes[profile] = None
            continue
        text = c.read(path); hashes[profile] = c.sha(text)
        servers, _ = c.parse(text)
        for name, config in servers.items():
            if not isinstance(config, dict):
                raise ValueError('invalid MCP mapping')
            if not c.candidate(name, config):
                continue
            if config.get('command') != str(runtime / 'run-client') or config.get('args', []) != []:
                raise ValueError('auth requires currently managed client runners')
            env = config.get('env') or {}
            if not isinstance(env, dict):
                raise ValueError('invalid MCP environment')
            if endpoint(env) != 8080:
                # Compare only references; never read another bridge's token.
                if env.get('WHATSAPP_BRIDGE_TOKEN_FILE') == value['token_file']:
                    raise ValueError('approved Indexa file shared with another bridge')
                continue
            targets.append((profile, name))
            if (profile, name) not in authorized:
                raise ValueError('unapproved Indexa consumer')
            old = old_fields(env, new)
            after = splice(text, name, new, c)
            records.append({'profile': profile, 'server': name, 'old': old,
                            'before': c.sha(text), 'after': c.sha(after)})
    if set(targets) != authorized or len(targets) != len(authorized):
        raise ValueError('approved Indexa consumers not found')
    # A minimal rollout supports one entry per file; refuse ambiguous composite
    # writes rather than journaling an incorrect partial-file hash.
    if len({r['profile'] for r in records}) != len(records):
        raise ValueError('multiple Indexa entries in one file require reviewed support')
    return {'inventory': hashes, 'records': records, 'new': new}


def apply(home, value, runtime, document, c):
    # Rediscover consumers even during offline recovery. New/foreign endpoints
    # cannot silently acquire the approved token.
    inventory(home, value, runtime, c)
    if document['new'] != a.client_fields(value['token_file']):
        raise ValueError('journal policy mismatch')
    writes = []
    for record in document['records']:
        path = c.config_path(home, record['profile']); text = c.read(path)
        servers, _ = c.parse(text)
        env = servers[record['server']].get('env') or {}
        for key, new in document['new'].items():
            current = {'present': key in env, 'value': env.get(key)}
            if current not in [record['old'][key], {'present': True, 'value': new}]:
                raise ValueError('foreign auth edit preserved; secure hold required')
        after = splice(text, record['server'], document['new'], c)
        writes.append((path, after, c.sha(text)))
    for path, after, before in writes:
        if c.sha(after) != before:
            c.write(path, after, before)


def main():
    action, home, runtime = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    # Verified packaged config.py provides the strict parser and CAS writer.
    sys.path.insert(0, str(runtime))
    import config as c
    data = json.load(sys.stdin); value = a.policy(data['policy'])
    if action == 'plan':
        print(json.dumps(inventory(home, value, runtime, c), sort_keys=True))
    elif action == 'apply':
        apply(home, value, runtime, data['plan'], c)
        print('{"ok":true}')
    else:
        raise ValueError('unknown auth config action')


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Indexa config prerequisite failed; private diagnostic suppressed') from None
