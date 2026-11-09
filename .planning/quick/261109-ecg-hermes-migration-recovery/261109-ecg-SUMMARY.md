---
status: incomplete
quick_id: 261109-ecg
code_status: reviewed-go
production_status: installed-updates-fenced
---
# Hermes recovered; controller installed; updates deliberately fenced

## Delivered

Existing Hermes 0.22.6 gateway recovered without data/auth/config rollback. Seven Telegram adapters connected; systemd active with zero restarts. Deploy timer remains enabled but stopped. The existing pending transaction and backup are preserved. The bot resumed executing tasks after recovery.

OpenAI implemented source commits b0b10b9, c7ccb5c, 167bf3c and d1b39d7 from origin/main 2f337f6 in an isolated worktree. Code covers expanded private canary, pre-stop schema/integrity comparison, durable intervention and one-attempt rollback, receipt/config-bound completion/resumption, earliest cross-lane barrier and independent watchdog. Installer preserves operator timer state. Automatic migrations remain conservatively blocked before cutover.

Claude Pessoal Opus5.5 reviewed the actual source: R1 NO-GO, R2 conditional GO, R3 final GO at d1b39d7. Relevant findings corrected and re-reviewed. Raw reviews and token/cost use retained in this directory. No functional code changed after final GO.

## Verification

47 maintenance tests pass, including actual synthetic SQLite migration/data preservation and full apply failure transition. Prior clean canonical CI passed all Go and Python groups; final 147-test clean-commit attestation is required before PR publication. VM temporary-store tests proved official initializer API availability, reproduced cron migration in the old backup and showed no schema/inventory changes in four consistent current-store copies. No other lane pending journals exist in the inspected VM. No Docker was started.

## Remaining work and decision

Publish reviewed PR via scripts/ci/local-ci.sh and check official GitHub code-CI result. No new controller code has been installed in the VM. Existing application pipelines do not install the infrastructure controller; the documented bootstrap is manual and requires explicit authorization under the user's production protocol. Do not treat code GO or tests as rollout approval.

After authorization, install a complete immutable reviewed merged controller generation using the existing installer from a separate clean infrastructure checkout, preserving the paused deploy timer and enabling independent health monitoring. Reconcile the legacy journal using the runbook's validated acceptance/archive checklist. Never restore historic data/auth, silently delete the journal, or re-enable the old controller. Leave deployments paused until baseline/canary/consumer checks pass.

Separate debt: bridge/client-only stop-first recovery paths outside a Hermes intervention remain unchanged; no such pending transaction was found. Watchdog uses existing CLI/Telegram delivery and does not prove model/tool conversational health. VM disk remains 97% full; snapshot disk reserves apply.

## Authorized installation follow-up

PR21 merged and controller 0277d2a installed after explicit user approval. See DEPLOYMENT.md/DEPLOYMENT.json. Independent health monitor and original consumers are active; seven bots connected. Full 113-store canary correctly rejected a pending Lismeri cron migration before touching services. Updates stay paused for that migration and insufficient disk reserve; no schema acceptance or data/auth restore was performed.
