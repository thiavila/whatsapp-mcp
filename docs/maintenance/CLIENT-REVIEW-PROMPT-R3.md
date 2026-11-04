Final proportional READ-ONLY re-review, effective claude-opus-5-5, capUSD2.1.
Previous actual calls USD1.6710416 +1.1142492 =2.7852908; entire task capUSD5.
Read/Glob/Grep only; no commands/delegation/network/edits/VM/auth/env/.git/.venv.
Current files/hashes: CLIENT-REVIEW-SNAPSHOT-R3.json. Tests in CLIENT-TESTS.json.
Read R2 findings (CLIENT-REVIEW-OPUS55-R2.json) and audit the concrete changed
functions. Avoid re-auditing unchanged code generically. Return actionable new
or unresolved findings, file:line/scenario/minimum fix; end CODE GO/NO-GO.
Linux CI/VM remain unrun, production NO-GO. No live deployment/messages/LLM except
these peer reviews. Parent alone pushes/CI/merges/deploys after bounded lease.

Corrections to all R2 P2/P3:
- P2A: Go source_ready requires ALL configs managed + every present mapped
  executable/hash correct, but NO historical count. Empty/lazy/removed callers
  are safe because every future invocation uses immutable wrapper. Native Go
  hold notifies once per Go SHA. Strict count remains only cutover health, where
  a live GATEWAY baseline exists; zero baseline only stages and never stops.
- P2B: poll sets candidate SHA as soon as known, including pending recoveries;
  poll_safely records candidate/type/safe message and hashes those into dedup key.
  Different future SHA or different actionable ValueError gets a new notice.
  Private subprocess diagnostics are suppressed, not dumped to users.
- P2C: workflow installs bubblewrap/AppArmor explicitly. On ephemeral CI only,
  it loads .github/apparmor/bwrap, the Ubuntu-documented narrow userns exception
  for /usr/bin/bwrap. No global sysctl restriction disable and no sudo canary.
  Same setpriv/NoNewPrivileges+bwrap helper remains; actual CI must pass. No VM
  policy/package changes are installed by this code. Target VM gate is independent.
  This asset is included in push filters and ancestor-equivalence paths.
- P3D: applied rollback refusal catches Deferred, resumes prior consumers, keeps
  journal and retries. poll returns while pending remains, avoiding new tx overwrite.
- P3E: runbook explicitly explains intentional hold on FOREIGN legacy source
  hash change (restarting unfrozen UV would be unsafe). No implicit source reset.
- P3F: installer now REQUIRES --expected-sha and compares HEAD before ANY write.
  Native bootstrap still complete/versioned/atomic under the existing lock.

Additional necessary compatibility fix discovered by actual uv0.13.5 probes:
Original runner stripped UV_ENV_FILE and used --no-env-file; that could silently
drop legitimate product credentials. launch now preserves UV_ENV_FILE and
UV_ENV_FILE_OVERRIDE opaquely and removes --no-env-file. All resolver injection
variables remain stripped; --offline/--no-project/--no-sync/--frozen/--no-config/
--no-python-downloads still literal with absolute packaged uv/Python.
The existing LEGACY cwd is kept, so relative explicit env-file semantics remain.
No auth/env file is read by the adapter or deployer, only normal uv runtime loading.
Bootstrap resets WHATSAPP_BRIDGE_DIR AFTER uv dotenv, preserving legacy default
store semantics. The shell entrypoint discards raw stderr (including dotenv
errors), never logs credential diagnostics. No MCP tool/schema change.

Canary now exercises actual wrapper twice with synthetic project cwd: default
poison.env MUST stay unloaded; explicitly authorized synthetic env-file marker
MUST reach the real child. Child assertions and equal57tool/schema hash prove
both paths. Mac3.11.18 +uv0.13.5 +certifi2026.10.19 +pip26.2.2 passes both, no tools/call.
Final receipt CLIENT-MACOS-CANARY.json. Linux isn't claimed; official workflow
build/relocation/netns and real VM gates remain required.

Focus files:
scripts/maintenance/client_executor.py (mapped/live/source_ready/poll/poll_safely/recover),
executor.py (Go deferral notice), install_executor.py (expected SHA),
scripts/client/{launch.py,canary.py,build.py}, .github/workflows/client-release.yml,
.github/apparmor/bwrap, tests/client/{test_runtime.py,test_transitions.py}.
Read docs/maintenance/CLIENT-ACTIVATION-GATE.md for the updated contract only if needed.
89 tests pass locally (36+22+31) with sanitized private HOME; notification calls
are mocked and no real send occurs. Tests cover zero/lazy Go safety, unique
candidate errors, applied refusal+pending preservation, wrong bootstrap SHA,
explicit opaque env variables, actual full Mac entrypoint.
