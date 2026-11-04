Review the concrete code in this worktree READ ONLY. Requested effective model:
claude-opus-5-5; report the actual model. Cap USD5. No commands, edits, other
agents, network, MCP or production access. Use Read/Glob/Grep only. Do not inspect
.git, .venv, auth/env files, unrelated directories or any real data. Source and
tests here contain no production credentials; review only the files below.

Thiago's security task requires local preparation of the minimal Python MCP
client remediation. Preserve Go/three bridges/current sessions/stores/profiles/
tokens. Root coordinator alone handles push/CI/merge/bootstrap/deploy. A separate
chat owns earlier executor PRs; this branch starts at current main d3f6454 (PR15).
No VM changes were made. New artifact/pipeline must actually deliver Python,
not only update a lock while production uv continues selecting an old venv.

Read docs/maintenance/CLIENT-RUNTIME-20261103.md for design and limits, then audit
the implementation, not just this summary. Review snapshot/fingerprint is in
CLIENT-REVIEW-SNAPSHOT.json. git diff is not available to you as shell; compare
the supplied code directly, including the existing Go executor that is reused.

Primary files:
- scripts/client/{pins.json,build.py,launch.py,canary.py,config.py}
- scripts/maintenance/{client_archive.py,client_executor.py,executor.py,
  control.py,release_ci.py,install_executor.py}
- .github/workflows/{client-release.yml,release.yml,local-ci-gate.yml}
- scripts/ci/check.py
- whatsapp-mcp-server/{pyproject.toml,uv.lock,.python-version,whatsapp.py,
  bridge_auth_probe.py,main.py}
- tests/client/test_runtime.py and whatsapp-mcp-server/tests/test_bridge_auth_probe.py
- tests/maintenance/test_executor.py (existing contract)

Versions: Python3.11.18 (stdlib rejects compromised emSign intermediates),
certifi2026.10.19, pip26.2.2, uv0.13.5. Source archive SHA and uv archive SHA were
verified against GitHub's official release asset digest. Lock preserved all
existing dependency versions except certifi, adding pip and PyYAML for adapter.

New client-release is independent of Go-only release, uses same merged-main/CI
admission and shared external executor lock. It stages a bounded hash-bound
portable runtime, checks relocated real SDK/catalog, then requires a fresh root
proof tied to payload/all8 profile hashes/external-writer fencing before stopping
gateway/dashboard/watch timers. Existing graceful stop refusal remains. Config
adapter only patches recognized command/args YAML spans, never .env/auth/DB or
credential fields; journal holds command fields/hashes only. Rollback preserves
latest other fields and refuses foreign command edits. No bridge restarts,
git-reset or DB/session restoration. Post-cutover examines real /proc executables
and their mapped hashes; old/deleted executables cannot count as new runtime.

Audit deeply: pipeline provenance/hash binding/manifest bounds, tar links and
relocation, actual UV/Python environment behavior, offline bwrap canary, cwd and
store-path compatibility, config parsing/inheritance/plugin gaps, crash windows
before/after each write/stop/resume, idempotent recovery, timers/active work,
foreign changes and unintended notifications, old process proof, proof expiry,
bootstrap/version compatibility, and whether tests really protect those risks.
Auth probe is opt-in, no new MCP tool; assert positive200/negative401 without
body/redirect/proxy, no token generation/rotation or automatic8080enforce.

Validation: 36 client +22 maintenance +17 runtime tests pass; actionlint passes.
Real Mac arm64 SDK probe with pinned Python/uv/deps lists57tools, no tools/call.
Linux x64 build/relocation/netns is NOT locally run: Docker was already open
from other work but daemon timed out; no restart/containers were attempted.
Linux workflow and real VM tools/recovery gates remain required, not a local GO.

Return only actionable findings ordered P0/P1/P2/P3, file:line, concrete scenario,
and minimum fix. Do not demand real production/LLM tests for a local code review;
keep that validation boundary explicit. End GO/NO-GO for CODE (not deployment).
