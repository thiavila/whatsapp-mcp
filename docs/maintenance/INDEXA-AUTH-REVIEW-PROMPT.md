Review OpenAI implementation of first Indexa8080 REST authentication transition. Read only, no tools beyond files. No secrets. Repo has no GSD. Personal scope. Base main dd4f7f95b4ec5b70ed3ed631ebb983162b7ccba3. Inspect actual code, not only docs. This is a security-sensitive config-only release lane; root owns push/official CI/bootstrap/VM activation. No production change has occurred. Do not claim live GO. Requested model Opus5.5; give concise concrete P1/P2/P3 findings (path,line,scenario,fix), or CODE GO if no material findings. Focus correctness/recovery/privacy; don't invent hypothetical requirements outside scope. Budget bounded.

Original requirement: clients authenticate before enforce; artifact from official merged main+green CI, contains no token values. Existing approved Indexa file or root pre-provisioning allowed; never use Melancia token or rotate credentials. Journal/recovery only config/refs/current state; preserve autonomous maintenance, guard live work, all original sessions/data/foreign edits. 401without/200with +connected/authenticated health and SDK readonly proof before consumer resume. No UID expansion.

Current facts: live managed immutable MCP client runtime dd4f7f9/Python3.11.18 is already activated, 3Python+3UV mapped; 4real SDK reads passed by root. Bridge8080 authoff; others8083/84enforce. One configured Indexa stdio entry indexa-roberto/whatsapp_indexa, no TOKEN/TOKEN_FILE; unit direct same Go binary/noflags/noenvfile, media-cache dropin. Known token dir only has two Melancia tokens; Indexa token must be reconciled/provisioned by root. Root proof fresh binds actual config inventory/unit/client/binary hashes/effective env/external writers/provenance/tools. No values printed.

Design: initial activation only. Missing policy/proof does not stop anything. staged journal before stops, secure-hold durable before stopping Indexa. From secure-hold no restore authoff ever; recover offline roll-forward or retain stopped. Unit wrapper execs same Go with enforced env after systemd inheritance; fail closed if policy/token missing. YAML fivefields includes UV_NO_ENV_FILE=true only Indexa, explicit gated assumption no dotenv dependency. No other env serialized. Configfield splices preserve unrelated values. Auth lane recovers before other lanes under existing outerlock, pending blocks those lanes. SDK probes readonly only; logs/raw bodies suppressed, counts stay private. Token bytes only memory, never saved backup/hash. Futurepolicyreplacement intentionally outside this first activation; adminUID1000 fullsudo is separate unresolved problem.

Tests currently 22auth (actual synthetic loopback401/200 +negative health),37client,22maintenance pass; noVMwrites/restart, no productLLM/send. Review crosslane recovery, crash windows, auth runtime inheritance, CAS preservation, proof freshness, artifact/provenance, output safety, process cleanup. Important supporting files scripts/client/config.py, scripts/client/launch.py, scripts/maintenance/client_executor.py, control.py/release_ci.py, whatsapp-bridge/config.go/main.go, productwhatsapp.py. New/modified snapshot below:
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/.github/workflows/indexa-auth-release.yml
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/docs/maintenance/INDEXA-AUTH-PLAN.md
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/docs/maintenance/INDEXA-AUTH-RUNBOOK.md
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/ops/indexa-auth.json
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/auth/build.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/ci/check.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/executor.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/indexa_auth_common.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/indexa_auth_config.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/indexa_auth_executor.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/indexa_auth_probe.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/indexa_auth_runtime.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/scripts/maintenance/install_executor.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/tests/auth/test_auth.py
/Users/thiagoavila/Github/Pessoal/whatsapp-mcp-indexa-auth-20261104/tests/client/test_transitions.py

Please inspect all relevant implementation and tests, not just documentation. Return concise actionable findings with severity and code verdict.
