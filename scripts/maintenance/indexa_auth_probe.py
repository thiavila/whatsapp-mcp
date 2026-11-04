"""Private bounded Indexa reads. No raw HTTP/RPC body or secret on stdout."""
import asyncio
from datetime import timedelta
import hmac
import http.client
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))
import indexa_auth_common as a


def request(path, secret=None, body=False):
    connection = http.client.HTTPConnection('127.0.0.1', 8080, timeout=5)
    try:
        headers = {'Authorization': 'Bearer ' + secret} if secret else {}
        connection.request('GET', '/api/' + path, headers=headers)
        response = connection.getresponse()
        data = None
        if body and response.status == 200:
            raw = response.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise ValueError('bounded read exceeded')
            data = json.loads(raw)
        return response.status, data
    finally:
        connection.close()


def health(enforced):
    status, data = request('health', body=True)
    if (status != 200 or not isinstance(data, dict)
            or any(data.get(k) is not True for k in ['success', 'connected', 'logged_in'])
            or (enforced and data.get('auth') != 'enforce')):
        raise ValueError('Indexa connected/authenticated health failed')


def rest(secret, enforced):
    status, _ = request('labels')
    if status != (401 if enforced else 200):
        raise ValueError('Indexa negative auth check failed')
    status, value = request('labels', secret, body=True)
    if (status != 200 or not isinstance(value, dict) or value.get('success') is not True
            or not isinstance(value.get('labels'), list)):
        raise ValueError('Indexa authenticated read failed')
    health(enforced)
    return len(value['labels'])


def labels_count(result):
    if result.isError:
        raise ValueError('SDK read failed')
    count = 0
    for item in result.content:
        if item.type != 'text':
            raise ValueError('unexpected SDK content')
        data = json.loads(item.text)
        rows = data if isinstance(data, list) else [data]
        if any(not isinstance(row, dict) or row.get('success') is False
               or any(row.get(k) for k in ['error', 'errors', 'error_message']) for row in rows):
            raise ValueError('SDK read returned error content')
        count += len(rows)
    return count


def groups_proof(result):
    # Unlike list_labels, this existing read-only tool preserves HTTP failures
    # in success:false. Empty labels/groups therefore cannot hide a child 401.
    if result.isError or len(result.content) != 1 or result.content[0].type != 'text':
        raise ValueError('SDK authenticated read failed')
    value = json.loads(result.content[0].text)
    if not isinstance(value, dict) or value.get('success') is not True or not isinstance(value.get('groups'), list):
        raise ValueError('SDK child did not prove authenticated read')


async def sdk(runtime, env):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    params = StdioServerParameters(command=str(runtime / 'run-client'), args=[], env=env,
                                  cwd='/home/thiago/repos/whatsapp-mcp/whatsapp-mcp-server')
    with open(os.devnull, 'w') as sink:
        async with stdio_client(params, errlog=sink) as streams:
            async with ClientSession(*streams, read_timeout_seconds=timedelta(seconds=8)) as session:
                await session.initialize()
                names = set(); cursor = None; seen = set()
                for _ in range(20):
                    page = await session.list_tools(cursor=cursor)
                    names.update(t.name for t in page.tools); cursor = page.nextCursor
                    if not cursor:
                        break
                    if cursor in seen:
                        raise ValueError('SDK tool pagination cycle')
                    seen.add(cursor)
                else:
                    raise ValueError('SDK tool pagination bound')
                if not {'list_labels', 'list_joined_groups'} <= names:
                    raise ValueError('SDK read tool missing')
                groups_proof(await session.call_tool('list_joined_groups', {}))
                return labels_count(await session.call_tool('list_labels', {'include_deleted': False}))


async def probe(home, runtime, value, enforced, c):
    secret = a.token(value['token_file'])
    fields = a.client_fields(value['token_file'])
    for target in value['consumers']:
        servers, _ = c.parse(c.read(c.config_path(home, target['profile'])))
        entry = servers[target['server']]
        if entry.get('command') != str(runtime / 'run-client') or entry.get('args', []) != []:
            raise ValueError('probe requires active managed runner')
        env = dict(entry.get('env') or {})
        if any(not isinstance(k, str) or not isinstance(v, str) for k, v in env.items()):
            raise ValueError('probe requires resolved string environment')
        if enforced and any(env.get(k) != v for k, v in fields.items()):
            raise ValueError('persistent consumer auth config differs')
        env.update(fields)
        if any(env.get(k) for k in ['HTTP_PROXY', 'HTTPS_PROXY', 'ALL_PROXY',
                                    'http_proxy', 'https_proxy', 'all_proxy']):
            raise ValueError('proxy environment requires reconciliation')
        if env.get('MCP_TRANSPORT', 'stdio') != 'stdio':
            raise ValueError('auth probe only supports stdio')
        # Exercise the actual packaged product credential loader with the
        # proposed/persisted consumer fields, without returning the header.
        sys.path.insert(0, str(runtime / 'app'))
        import whatsapp
        old = dict(os.environ)
        try:
            os.environ.clear(); os.environ.update(env)
            if not hmac.compare_digest(whatsapp.bridge_headers().get('Authorization', ''), 'Bearer ' + secret):
                raise ValueError('product client did not resolve approved auth reference')
        finally:
            os.environ.clear(); os.environ.update(old)
        before = rest(secret, enforced)
        count = await sdk(runtime, env)
        if not hmac.compare_digest(a.token(value['token_file']), secret):
            raise ValueError('approved credential changed during proof; preserve current bytes')
        if before != count or rest(secret, enforced) != count:
            raise ValueError('SDK/REST read counts differ; retry after sync settles')
    return {'ok': True, 'authenticated_consumers': len(value['consumers']),
            'unauthenticated_status': 401 if enforced else 200,
            'authenticated_status': 200, 'health_connected': True, 'sdk_read': True}


def main():
    # Product stderr may contain private diagnostics. Suppress both process and
    # child diagnostics before importing SDK/product code.
    fd = os.open(os.devnull, os.O_WRONLY); os.dup2(fd, 2); os.close(fd)
    logging.disable(logging.CRITICAL)
    action, home, runtime = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if action not in ['before', 'after']:
        raise ValueError('unknown auth probe phase')
    sys.path.insert(0, str(runtime))
    import config as c
    value = a.policy(json.load(sys.stdin)['policy'])
    result = asyncio.run(asyncio.wait_for(probe(home, runtime, value, action == 'after', c), timeout=40))
    print(json.dumps(result, sort_keys=True))


if __name__ == '__main__':
    try:
        main()
    except BaseException:
        raise SystemExit('Indexa private auth probe failed') from None
