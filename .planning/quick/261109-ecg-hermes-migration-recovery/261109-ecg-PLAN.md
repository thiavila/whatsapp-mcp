---
quick_id: 261109-ecg
status: in-progress
---
# Prevent Hermes migration rollback outages

User authorized service recovery, permanent correction, regression tests and Claude review. OpenAI implements inline; Claude Pessoal reviews the exact diff. This repository had no GSD state; bootstrap this bounded maintenance scope. Worktree is isolated from origin/main 2f337f6. No unrelated application edits.

1. Recover existing installed Hermes 0.22.6 without database rollback. Pause repeating maintenance timer; verify all Telegram profiles connected and no restarts. Preserve pending journal and backup for analysis.
2. Correct scripts/maintenance/hermes_executor.py: cover gateway-owned state stores in private canary; retain conservative pre-cutover migration gate until full migration coverage is proven; schema-incompatible recovery must not stop healthy candidate, must persist intervention and never repeat destructive work. Gate all other deploy lanes while intervention remains. Add independent read-only gateway health alert through external executor.
3. Reproduce incident with real synthetic SQLite migrations and mocked service transitions; test repeated polls, unhealthy candidate, safe rollback, pre-cutover failures and notification failures. Run full canonical CI, Claude review and resolve findings. Record reviewed diff, usage and deployment limits; publish PR via local CI attestation.

Acceptance: never downgrade across changed schema; never restore old data/auth; never repeatedly stop a healthy running candidate; migrations not covered/proven by canary fail BEFORE installation; no unreviewed app deployment. Record any remaining deployment or monitor limits explicitly.
