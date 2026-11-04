"""Opt-in REST auth proof. No message bodies, writes, token output or new MCP tool."""
import json
from urllib.parse import urlsplit

import requests
import whatsapp


def probe(base_url=None, token=None):
    base_url = whatsapp.WHATSAPP_API_BASE_URL if base_url is None else base_url
    token = whatsapp.bridge_token() if token is None else token
    parsed = urlsplit(base_url)
    if (parsed.scheme != 'http' or parsed.hostname not in ['localhost', '127.0.0.1', '::1']
            or parsed.port not in [8080, 8083, 8084] or parsed.path.rstrip('/') != '/api'
            or parsed.username or parsed.password or parsed.query or parsed.fragment):
        raise ValueError('approved local bridge endpoint required')
    if not token:
        raise ValueError('existing bridge credential required; none was provisioned')
    statuses = []
    # Do not allow a redirect/proxy to receive an existing bridge credential.
    with requests.Session() as session:
        session.trust_env = False
        for headers in [{}, {'Authorization': 'Bearer ' + token}]:
            with session.get(base_url.rstrip('/') + '/chats', headers=headers,
                             stream=True, allow_redirects=False, timeout=(3, 5)) as response:
                statuses.append(response.status_code)  # Never read/parse the body.
            if len(statuses) == 1 and statuses[0] != 401:
                return {'auth_enforced': False, 'unauthenticated_status': statuses[0],
                        'authenticated_status': None}  # Never send token to auth-off endpoint.
    return {'auth_enforced': statuses == [401, 200],
            'unauthenticated_status': statuses[0], 'authenticated_status': statuses[1]}


if __name__ == '__main__':
    try:
        result = probe()
    except Exception:
        raise SystemExit('Bridge auth probe failed; private errors and credentials suppressed')
    print(json.dumps(result, sort_keys=True))
    raise SystemExit(0 if result['auth_enforced'] else 1)
