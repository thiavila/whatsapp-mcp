"""Real SDK initialize/list, no tools/call, real data, secrets, LLM or external network."""
import asyncio
import hashlib
import importlib.metadata as md
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile


async def probe(root, temp, explicit_env=False):
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client
    env = {'PATH': '/usr/bin:/bin', 'HOME': str(temp), 'LANG': 'C.UTF-8',
           'WHATSAPP_STORE_DIR': str(temp / 'nonexistent-store'),
           'WHATSAPP_API_BASE_URL': 'http://127.0.0.1:1/api',
           'MCP_TRANSPORT': 'stdio'}
    project = temp / 'project'; project.mkdir()
    (project / '.maintenance-canary').touch()
    (project / '.python-version').write_text('0.0.0\n')
    (project / 'pyproject.toml').write_text('this is intentionally not valid TOML')
    (project / '.env').write_text('MCP_CLIENT_POISON=must-not-be-loaded\n')
    (project / '.venv/bin').mkdir(parents=True)
    (project / '.venv/bin/python').write_text('#!/bin/sh\nexit 99\n')
    (project / '.venv/bin/python').chmod(0o700)
    if explicit_env:
        (project / 'authorized.env').write_text('MCP_CLIENT_OPAQUE_ENV_PROBE=synthetic-preserved\n')
        env['UV_ENV_FILE'] = str(project / 'authorized.env')
        env['MCP_CLIENT_EXPECT_ENV'] = 'synthetic-preserved'
    # Exercise the actual shell entrypoint -> launch.main -> chdir -> execve
    # -> packaged uv -> packaged Python, inside a hostile project-like cwd.
    args = [str(root / 'run-client'), '--canary-cwd', str(project)]
    with open(os.devnull, 'w') as sink:
        async with stdio_client(StdioServerParameters(command=args[0], args=args[1:],
                                                      env=env, cwd=str(temp)), errlog=sink) as streams:
            async with ClientSession(*streams) as session:
                await session.initialize()
                tools = []
                cursor = None
                while True:
                    page = await session.list_tools(cursor=cursor)
                    tools.extend(t.model_dump(mode='json') for t in page.tools)
                    cursor = page.nextCursor
                    if not cursor:
                        break
    names = {t['name'] for t in tools}
    if not {'list_chats', 'send_message', 'list_labels'} <= names:
        raise ValueError('client tool catalog incomplete')
    if list(temp.iterdir()):
        # No tool was called, so the SDK/server must not create a database.
        forbidden = [p for p in temp.rglob('*') if p.name.endswith('.db')]
        if forbidden:
            raise ValueError('canary unexpectedly created product data')
    return {'tools': len(tools), 'schema_sha256': hashlib.sha256(
        json.dumps(tools, sort_keys=True).encode()).hexdigest()}


def main(root):
    root = Path(root).resolve()
    pins = json.loads((root / 'runtime.json').read_text())['versions']
    if sys.version.split()[0] != pins['python'] or any(
            md.version(name) != pins[name] for name in ['certifi', 'pip']):
        raise ValueError('client Python/TLS package version mismatch')
    if not subprocess.check_output([str(root / 'uv'), '--version'], text=True).startswith(
            'uv ' + pins['uv'] + ' '):
        raise ValueError('client uv version mismatch')
    with tempfile.TemporaryDirectory(prefix='mcp-client-canary-') as temporary:
        implicit = Path(temporary) / 'implicit'; implicit.mkdir()
        explicit = Path(temporary) / 'explicit'; explicit.mkdir()
        result = asyncio.run(asyncio.wait_for(probe(root, implicit), timeout=30))
        authorized = asyncio.run(asyncio.wait_for(probe(root, explicit, explicit_env=True), timeout=30))
        if result != authorized:
            raise ValueError('explicit opaque env changed MCP catalog')
    print(json.dumps({'ok': True, **result, 'versions': pins, 'explicit_env_preserved': True}, sort_keys=True))


if __name__ == '__main__':
    main(sys.argv[1])
