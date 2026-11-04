Proportional READ ONLY review, effective claude-opus-5-5, capUSD0.8. No shell,
edits, delegation, network, auth/env/.git/.venv/VM. Read/Grep only. Inspect ONLY
scripts/maintenance/client_executor.py processes() and mapped(), plus new
tests/client/test_process_scope.py and CLIENT-PROCESS-SCOPE-FIX.md.

Base main bca156b410432626f341d8ba183cd079bf665811 (PR16 merged). Coordinator has
already passed full CI/release/Linux canary and real VM stage for that source.
No client cutover/policy/readiness; existing bots/data preserved. New real issue:
processes() reads /proc/PID/exe BEFORE filtering cmdline. sd-pam/sshd/gpg-agent
from sameUID1000 have readable irrelevant cmdline but protected exe. This blocks
all client stage processing. Root confirmed those exact operations, no argv or
credential content exported. No VM command by this implementing front.

Minimal fix: path relevance filter immediately after cmdline, before exe/cgroup/
any other attribute. No ad-hoc comm-name exclusions. A positive relevance flag
retains fail-closed on PermissionError even if another UID owns that relevant
process. Unknown same-user inaccessible cmdline keeps the previous fail-closed.
Relevant (deleted) Python/uv remains in records and fails mapped/quiet; relevant
outside gateway/dashboard remains blocked. No bootstrap/launcher/ABI/pin/state
change; new controller will be installed VERSIONED by root after PR/CI.

Six new targeted regressions passed +19existing runtime tests. Actual in-memory
counterproof: load original processes() from base and run irrelevant-protected
regression -> expected ValueError cannot inspect same-user runtime process.
No repeat of full89suite; no notifications/real servers/LLM except this review.
Probe in central directory is OUTSIDE THIS COMMIT and outside review scope.

Return only actionable new/unresolved P1/P2/P3 with line/scenario/minimum fix,
or CODEGO for this narrow delta. Do not demand/repeat already sufficient tests
or treat missing VM rollout as code failure. Confirm effective model.
