# Hermes migration recovery

## Incident and recovery (2026-11-09)

Hermes 0.22.6 was installed by the existing maintenance pipeline on November 8. Telegram connected, then the post-start schema comparison failed. Recovery stopped consumers before checking compatibility; the incompatible schema blocked rollback and every subsequent poll repeated the stops.

User explicitly authorized recovery. The operator stopped `mcp-maintenance-deploy.timer` and its service, then started the existing `hermes-gateway.service`. All seven Telegram adapters connected. Gateway state was running, with zero service restarts. No database, credential, paired session, source version, or launcher was restored. The existing pending transaction and retained backup were preserved. Timer remains enabled but stopped; do not start the OLD controller again.

## Corrected policy

- A private pre-cutover canary exercises session state, shared hosted-room state, cron execution/incidents and notepad initializers. Store inventory and schema changes block automatic promotion before stopping live services. This is conservative: it does NOT automatically authorize migrations, including legitimate upstream migrations. New store APIs fail closed and require an update/review of this adapter.
- A real backup reproduction of the incident ran these official initializers against private copies of four retained default stores. It detected a cron/executions.db schema migration. No live database content was exported or printed. Temporary copies were removed.
- Recovery compares current schemas and integrity BEFORE any stop. An incompatible or unreadable schema writes a durable intervention marker before attempting notification. Old code is never restored over incompatible data. A second comparison after stopping closes the check/stop race; a change there deliberately leaves stopped services for operator intervention.
- Once intervention is recorded, subsequent polls perform no service/code/database operations. They may retry the deduplicated intervention notice. The pending journal and backup remain intact. Other deployment lanes must remain fenced until reconciliation.
- An unhealthy current candidate is not automatically declared successful or restarted. The watchdog reports lost health independently; operators must decide how to recover with the current data.
- The independent `hermes-health-watch.timer` runs every two minutes, reports after five minutes of observed failure and deduplicates by outage. It checks systemd ownership, the gateway's real heartbeat and Telegram adapter metadata. It does not send a conversational LLM probe and cannot guarantee that model credentials or every tool work. It uses the existing principal CLI to notify the operator; Telegram/CLI delivery failure remains a limitation.

## Installation and reconciliation gates

Modified controller code has not been installed in the VM merely by opening/merging its PR. Use the approved infrastructure-control installation process after Claude review and official code CI; application deploys remain on their official pipeline. Do not copy individual modules into the live control directory. Installation must publish a complete immutable control generation using `install_executor.py --expected-sha <reviewed merged SHA>` from a clean separate infrastructure checkout. Its new watchdog is installed with that generation.

There is no dedicated GitHub Actions transport for installing this infrastructure controller in this repository. Existing runbooks describe a manual infrastructure bootstrap. Obtain explicit authorization for that manual installation under the user's Git/production protocol; do not treat the application release workflow or a passed local test as deployment permission.

The old pending journal must NOT be deleted or rewritten to pretend the failed transaction succeeded. After installing the correction, it should be reconciled into intervention without stopping the already recovered gateway. Keep automatic deployments fenced until an operator validates the installed version, current schema/data and all affected consumers, archives the old transaction with a durable decision, and deliberately releases the fence. Never restore old auth or databases as part of this acceptance.

Known resource issue: VM root volume was 97% full (about 2.4 GiB free) after recovery. Snapshot reserve checks remain mandatory; this task did not delete user/runtime data to make room.

## Review corrections and exit checklist

The installer now preserves a paused deployment timer and starts only the independent health timer. The Hermes recovery barrier runs before auth or bridge recovery. If other deployment journals coexist, it records intervention without invoking any recovery lane. VM check found no bridge, client, or auth pending journals.

A rollback attempt is journaled before the first stop. Interrupted/failed rollback cannot repeat service stops. A healthy candidate may complete a new receipt-bound transaction only when current schemas, install fingerprints, original config fingerprints, fresh heartbeat, process ownership and Telegram status agree. Legacy transactions cannot take this automatic completion shortcut.

If schema changes during stop, only an unchanged, receipt-bound candidate installation may be resumed without restoring any old code. Unreadable/corrupt schema, legacy journals or foreign installations remain deliberately stopped for operator assessment; the notice explicitly says the gateway may be stopped. This exception prevents restarting unproven code over damaged data.

To leave the current legacy intervention, an authorized operator must:

1. Keep deployment timer stopped; obtain executor.lock and confirm all other transaction journals absent. Leave the health timer running.
2. Validate the original official run/candidate receipt and installed source/version/launcher/units. Check all seven Telegram adapters, current database integrity and affected consumers. Review and approve the actual migrated schema against the candidate; do not infer approval merely from schema drift or one connected bot.
3. Create a fresh consistent private backup of current data (respect free-space reserve). Preserve the old backup and original pending journal byte-for-byte.
4. Write an acceptance record under a private timestamped intervention archive with operator authorization, original run/candidate, original pending/backup hashes, installed file fingerprints, current schema fingerprints and health evidence. Copy the pending/intervention/failed receipts there. Fsync the archive before removing the active pending/intervention marker. No auth/config/data restoration, no fabricated success for the original failed deployment.
5. Record the accepted current baseline separately, restore only the affected consumers approved by the operator, and verify health. Release the deployment timer only after the new canary has passed against the accepted current baseline and all other lane gates are reconciled. A fresh promotion remains subject to the official release pipeline.

This checklist is an operator procedure, not automatic acceptance logic. No acceptance or journal archival has been performed by this patch.

Real replay evidence: prior backup's default state/shared/cron/notepad copies passed official initialization and changed cron/executions.db as expected. Repeating the initializer against consistent copies of the current four stores returned no schema changes and no added stores. Replays used private VM temporary directories; copies were removed. Only filenames/statuses, never row contents, were reported. This is bounded evidence for these stores on 0.22.6, not a claim of coverage for every future upstream initializer.

Final review adjustments: post-stop resumption also requires unchanged configuration fingerprints, and completing a healthy candidate resumes its original watch timers first. New Hermes promotion refuses any other pending transaction. The installer preserves both the active and enabled states of the deployment timer, including a deliberately disabled timer; only the health timer is unconditionally enabled/started.

Separate legacy debt (outside this Hermes updater change): bridge/client recoveries without a Hermes journal retain their existing recovery policies, including stop-first paths. No such journal exists in the inspected VM. Do not claim this patch proves every independent deployment controller safe against every future migration. The Hermes barrier fences those lanes during a Hermes intervention.

A fresh installation with no previous deployment timer intentionally leaves deployments disabled; enabling them is an explicit operator step. This differs from upgrading the existing enabled-but-paused timer in this VM.
