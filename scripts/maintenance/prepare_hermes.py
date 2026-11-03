"""Generate a commit-bound test receipt using Hermes' official per-file runner.

Call only after staging dependencies with the official installer in a dedicated
worktree. Test HOME is private; never copies live auth/config into tests.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import tempfile
from control import require_sha
from hermes_control import candidate_root

TESTS=['tests/agent/test_codex_xai_oauth_recovery.py',
       'tests/agent/test_credential_pool_profile_oauth_fork.py',
       'tests/tools/test_tool_result_storage.py',
       'tests/agent/test_turn_context_overflow_warning.py',
       'tests/tools/test_budget_config.py',
       'tests/hermes_cli/test_auth_profile_fallback.py',
       'tests/agent/test_credential_pool_oauth_writethrough.py',
       'tests/agent/test_retry_utils.py']
BASE=Path('/home/thiago/hermes-maintenance/hermes-deploy')


def prepare(root, tag, upstream):
    root=candidate_root(root)
    sha=require_sha(subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip())
    require_sha(upstream)
    if subprocess.check_output(['git','-C',str(root),'status','--porcelain','--untracked-files=no']).strip():
        raise ValueError('dirty candidate')
    subprocess.run(['git','-C',str(root),'merge-base','--is-ancestor',upstream,sha],check=True)
    if any(not (root/p).is_file() for p in TESTS):raise ValueError('test coverage changed; adapt runner before upgrade')
    os.umask(0o077)
    destination=BASE/'receipts'/sha;destination.mkdir(parents=True,mode=0o700,exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='hermes-tests-') as temporary:
        # PM acquires its already staged tools before switching to a private test HOME.
        env={'HOME':'/home/thiago','PATH':'/home/thiago/.local/bin:/usr/local/bin:/usr/bin:/bin','LANG':'C.UTF-8'}
        command=['bash',str(root/'scripts/run-in-hermes-env'),'env',
                 'HOME='+temporary,'HERMES_HOME='+temporary,'GIT_CONFIG_GLOBAL=/dev/null',
                 'GIT_CONFIG_NOSYSTEM=1','GIT_AUTHOR_NAME=Maintenance Test',
                 'GIT_AUTHOR_EMAIL=test@example.invalid','GIT_COMMITTER_NAME=Maintenance Test',
                 'GIT_COMMITTER_EMAIL=test@example.invalid','bash',str(root/'scripts/run_tests.sh'),
                 '-j','2','--file-timeout','90','--file-retries','0',*TESTS]
        log=destination/'tests.log'
        with log.open('wb') as stream:
            subprocess.run(command,cwd=root,env=env,stdin=subprocess.DEVNULL,stdout=stream,stderr=subprocess.STDOUT,check=True,timeout=900)
    # Never infer success from agent prose; only a successful bounded runner creates this receipt.
    if subprocess.check_output(['git','-C',str(root),'rev-parse','HEAD'],text=True).strip()!=sha:
        raise ValueError('candidate changed during tests')
    receipt={'schema':1,'candidate_sha':sha,'upstream_sha':upstream,'tag':tag,'root':str(root),
             'tests':TESTS,'tests_log_sha256':hashlib.sha256(log.read_bytes()).hexdigest(),
             'result':'passed','runner':'official-per-file-private-home',
             'installer_sha256':hashlib.sha256((root/'scripts/install.sh').read_bytes()).hexdigest()}
    path=destination/'receipt.json';path.write_text(json.dumps(receipt,sort_keys=True))
    print(json.dumps({'candidate_sha':sha,'receipt_sha256':hashlib.sha256(path.read_bytes()).hexdigest(),'receipt':str(path)}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--root',required=True);p.add_argument('--tag',required=True);p.add_argument('--upstream-sha',required=True)
    a=p.parse_args();prepare(a.root,a.tag,a.upstream_sha)
