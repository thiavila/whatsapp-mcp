"""Versioned ExecStart wrapper. Same binary/store/session; no permissive fallback."""
import os
from pathlib import Path
import sys

# -I deliberately omits the script directory. Import only this installed version.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import indexa_auth_common as a


def main():
    if len(sys.argv) != 2:
        raise ValueError('policy fingerprint required')
    value = a.policy(a.root_document(a.BASE / 'indexa-auth-policy.json'))
    if a.fingerprint(value) != sys.argv[1]:
        raise ValueError('auth policy changed; explicit reconciliation required')
    env = a.secure_environment(value, os.environ)
    os.execve(a.BINARY, [a.BINARY], env)


if __name__ == '__main__':
    try:
        main()
    except Exception:
        raise SystemExit('Indexa authentication prerequisite failed; bridge kept stopped') from None
