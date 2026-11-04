"""Indexa-only auth contract. Secret bytes stay in memory, never in receipts."""
import hashlib
import json
import os
from pathlib import Path
import re
import stat

BASE = Path('/home/thiago/hermes-maintenance/mcp-deploy')
UNIT = 'whatsapp-bridge-indexa.service'
BINARY = '/home/thiago/repos/whatsapp-mcp/whatsapp-bridge/whatsapp-bridge'
WORKFLOW = '.github/workflows/indexa-auth-release.yml'
REPOSITORY = 'thiavila/whatsapp-mcp'
CONFIG = {'schema': 1, 'component': 'indexa-rest', 'port': 8080,
          'auth_mode': 'enforce', 'require_token': True, 'token_ref': 'indexa-rest'}
MODULES = ['indexa_auth_common.py', 'indexa_auth_runtime.py', 'indexa_auth_config.py',
           'indexa_auth_probe.py', 'indexa_auth_executor.py']


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


def fingerprint(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def root_document(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_uid != 0 or st.st_nlink != 1
                or st.st_mode & 0o022 or st.st_size > 65536):
            raise ValueError('unsafe root auth document')
        data = os.read(fd, 65537)
        value = json.loads(data)
        if not isinstance(value, dict):
            raise ValueError('auth document must be an object')
        return value
    finally:
        os.close(fd)


def policy(value):
    fields = {'schema', 'enabled', 'repository', 'workflow', 'activation_sha',
              'token_ref', 'token_file', 'consumers'}
    if (set(value) != fields or type(value.get('schema')) is not int or value.get('schema') != 1 or value.get('enabled') is not True
            or value.get('repository') != REPOSITORY or value.get('workflow') != WORKFLOW
            or value.get('token_ref') != 'indexa-rest'
            or not re.fullmatch('[0-9a-f]{40}', str(value.get('activation_sha', '')))):
        raise ValueError('invalid Indexa auth policy')
    token_path(value['token_file'])
    consumers = value['consumers']
    if (not isinstance(consumers, list) or not consumers
            or any(not isinstance(c, dict) or set(c) != {'profile', 'server'}
                   or not all(isinstance(v, str) and v for v in c.values()) for c in consumers)
            or len({(c['profile'], c['server']) for c in consumers}) != len(consumers)):
        raise ValueError('invalid authorized auth consumers')
    return value


def token_path(value):
    if (not isinstance(value, str) or not re.fullmatch(
            r'/home/thiago/\.config/whatsapp-bridges/[A-Za-z0-9_-]+\.token', value)):
        raise ValueError('token reference outside approved bridge directory')
    return Path(value)


def token(value):
    path = token_path(value)
    # No symlink components, no shared hardlink with another bridge credential.
    for part in [path, *path.parents]:
        if part.is_symlink():
            raise ValueError('symlinked token reference')
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        st = os.fstat(fd)
        if (not stat.S_ISREG(st.st_mode) or st.st_nlink != 1 or st.st_uid not in [0, 1000]
                or st.st_mode & 0o027 or not 32 <= st.st_size <= 4096):
            raise ValueError('unsafe or missing approved token file')
        raw = os.read(fd, 4097).decode('ascii').strip()
        if not 32 <= len(raw) <= 512 or any(ord(c) < 33 or ord(c) > 126 for c in raw):
            raise ValueError('invalid approved token format')
        return raw
    finally:
        os.close(fd)


def client_fields(token_file):
    token_path(token_file)
    return {'WHATSAPP_BRIDGE_TOKEN': '', 'WHATSAPP_BRIDGE_TOKEN_FILE': token_file,
            'WHATSAPP_API_BASE_URL': 'http://127.0.0.1:8080/api',
            'WHATSAPP_BRIDGE_PORT': '8080', 'UV_NO_ENV_FILE': 'true'}


def secure_environment(value, inherited):
    policy(value)
    token(value['token_file'])  # Validate before exec; never put bytes in argv/env.
    env = dict(inherited)
    env.pop('WHATSAPP_BRIDGE_TOKEN', None)
    env.update(WHATSAPP_BRIDGE_TOKEN_FILE=value['token_file'], WHATSAPP_BRIDGE_PORT='8080',
               WHATSAPP_BRIDGE_AUTH_MODE='enforce', WHATSAPP_BRIDGE_REQUIRE_TOKEN='true')
    return env
