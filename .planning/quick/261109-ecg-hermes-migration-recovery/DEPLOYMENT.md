# Authorized infrastructure installation — 2026-11-09

User explicitly approved merging PR21 and manually installing the controller while keeping updates paused until validation. PR21 merged as 0277d2a82b626d3b653567df556180f10f305fbf with official CI and the reviewed head verified. Main Mac checkout was fast-forwarded immediately. The merged functional code matches the reviewed head byte-for-byte.

On the VM, fetched main without moving the application checkout and created a clean detached infrastructure worktree at /home/thiago/worktrees/whatsapp-mcp-controller-0277d2a. Backed up the previous controller service/timer under /home/thiago/hermes-maintenance/mcp-deploy/bootstrap-backups/20261109-0277d2a. Ran the reviewed install_executor.py with exact expected merged SHA, outside the gateway cgroup. Complete immutable controller generation is installed under control-releases/0277d2a82b626d3b653567df556180f10f305fbf. Deployment timer stayed paused; new independent health timer is enabled/active and has executed successfully.

## Runtime evidence

- Original official action run 42478485171, artifact, candidate receipt, test log digest, installer digest and clean installed source verified through reviewed validation functions. VM GH identity thiavila-hermes; Mac identity thiavila.
- Repeated legacy recovery barrier twice with service operations prohibited in the validation process: both attempts returned intervention, without stopping the gateway. Original pending journal and backup preserved. Durable intervention reason schema-incompatible.
- Snapshot/integrity checks succeeded for 113 SQLite databases (2,552,958,976 bytes). Full candidate canary ran on copies in private /dev/shm, then removed them. It correctly BLOCKED before any service operation on profiles/melancia-lismeri/cron/executions.db: that store still needs a schema migration. The earlier four-root-store probe was bounded evidence and did not cover this profile. Do not claim the full baseline canary passed.
- No live database was migrated/restored. No historical auth or config restored. Legacy transaction was NOT marked accepted or silently deleted. A future controlled schema-acceptance operation still requires current backup, migration assessment and the runbook exit checklist.
- Resumed gateway/dashboard and the original four watch timers using the preserved consumer states and the reviewed resume helper. Seven Telegram adapters connected; gateway remains active with zero restarts. Watchdog reports no health problem. Deployment timer remains inactive.

## Disk pressure

VM root volume had fallen from 1.2 GiB free to about 318 MB free during this session. Private canary copies were in RAM and have been removed. Only the dedicated regenerable Go CI build/module caches were cleaned via go clean -cache -modcache, after checking no Go compilation was active. No user data, model, database, paired session or old backup was deleted. Final free space about 1,000,374,272 bytes (~954 MiB), still below the update snapshot reserve (1 GiB plus live database sizes). Do not re-enable updates or bypass that gate.

## Remaining boundary

Controller fix is integrated and installed; health monitoring is active. Automatic promotion remains deliberately suspended until the profile migration is handled with a validated current backup and adequate disk reserve. Bridge/client-only legacy recovery policies are unchanged outside a Hermes intervention. No new LLM calls or Docker were used during installation.

Canonical VM evidence: /home/thiago/hermes-maintenance/mcp-deploy/validation-0277d2a.json; local copy is DEPLOYMENT.json beside this record. This documentation-only follow-up is kept on its dedicated local branch; it changes no reviewed runtime code.
