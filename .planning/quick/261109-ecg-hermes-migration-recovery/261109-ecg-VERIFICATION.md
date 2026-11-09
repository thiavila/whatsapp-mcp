# Verification

- Recovery: existing 0.22.6 gateway active, zero restarts; seven Telegram bots connected in the aggregate gateway state. Maintenance timer enabled but stopped. No database/auth/config rollback or app redeploy.
- Clean-commit canonical CI passed at b0b10b9 and 167bf3c (Go plus 133/143 Python tests respectively). Final d1b39d7 adds four tests; the final commit will be attested by scripts/ci/local-ci.sh before publishing.
- Current maintenance tests: 47 passed. Tests use actual temporary SQLite for schema drift, preservation of post-start messages, full apply/start/fail/recover, safe rollback, repeated poll/crash handling and notification failure. Metadata/receipts/service operations are synthetic; no LLM production probes.
- Official 0.22.6 initialization in empty private VM HOME passed; created only private state/shared/cron/notepad stores.
- Replay using private copies of four retained pre-update stores: exit0; cron/executions.db schema changed, proving the broadened pre-cutover canary would reject this incident. Copies removed.
- Replay using online SQLite backups of the corresponding four CURRENT stores: exit0; no changed schema or added store. Copies removed. This is bounded store/API evidence, not arbitrary-future-version migration support.
- VM bridge/client/auth pending journals: all absent. Existing Hermes legacy pending+backup preserved.
- Claude Opus5.5 Pessoal R1: NO-GO; relevant findings fixed. R2: conditional GO; A1/A2 and other small recommendations fixed. R3: GO on source d1b39d7 with no open blocking findings. Exact source tips and raw reviews/usage retained with this task.
- No Docker containers started. No deployment of modified code performed. Infrastructure bootstrap requires explicit authorization; deploy timer remains paused and intervention reconciliation is still required.
