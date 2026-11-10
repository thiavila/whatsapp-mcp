---
quick_id: 261110-cli
status: complete
---
# Hermes CLI/dashboard atomic cutover summary

- Added fail-closed launcher identity validation for version, checkout root, and SHA, with full receipt/git revalidation after installer stages.
- Added fsynced same-directory publication with atomic replace, digest ownership checks, and symlink/non-regular refusal.
- Cutover now publishes and re-proves the candidate official launcher, rewrites the backed-up dashboard unit to the global launcher, and journals fingerprints only after all three installation files are aligned.
- Rollback restores the saved launcher and service units through the same durable publication primitive.
- Added regressions for expected previous-launcher takeover, foreign-launcher refusal without mutation, dashboard/fingerprint alignment, and retained rollback behavior.

Independent review correction:
- Initial review failed: the same-candidate shortcut accepted partial installations; rollback lacked journal-bound target fingerprints; launcher ownership executed self-reported code; publication checks left identity/use gaps; and unit rewrites were not semantically validated.
- Corrected the shortcut to require candidate launcher, gateway unit, and dashboard unit alignment or fail closed. This intentionally identifies the observed gateway-candidate/global-shim-old state as partial; reconciling that live state remains a separate bounded migration task.
- Bound rollback and publication to durable digest plus inode metadata, fenced changed targets before stopping/restoring, established old-launcher ownership from canonical gateway checkout/git/digest metadata without execution, and added semantic gateway/dashboard `ExecStart` validation.
- Added regressions for the current partial state, spoofed launcher non-execution, rollback target mutation, and invalid unit commands.

Verification:
- Focused Hermes suites: 40 tests passed.
- Final full maintenance suite: 55 tests passed.
- Python compile and `git diff --check`: passed.
- Canonical `python3 scripts/ci/check.py` before the final two unit-only regressions: Go passed and all 153 Python tests passed (server 36, maintenance 53, client 37, auth 27); the runner then exited nonzero during temporary-directory cleanup because a background cache writer recreated entries. The final maintenance rerun passed all 55 tests.
- Production, profiles, user data, remotes, and services were not modified.

Second review correction:
- Removed old-launcher execution from ownership proof. The prior checkout and launcher bytes must now match the trusted local per-SHA receipt (including its launcher digest), canonical receipt root, exact clean git HEAD, effective gateway command, and global shim bytes.
- Replaced pathname precheck/rename publication with Linux `renameat2(RENAME_EXCHANGE)`: the displaced target is compared with the expected journal identity, and a mismatch is atomically exchanged back before failure. Unsupported exchange fails closed.
- Journaled exact backup-source identities after copying and require the same source identity immediately before rollback publication; an altered backup now fences rollback.
- Made `systemctl --user show ... -p ExecStart --value` authoritative after daemon reload, so effective drop-ins are included. Only the fixed no-space argv form is accepted; the base-file parser is limited to the dashboard rewrite and no longer uses `shlex`.
- Added regressions for effective/drop-in mismatch, changed backup source, exchange mismatch restoration, receipt-bound old-launcher ownership without execution, and the existing rollback/cutover paths.

Second review verification:
- Focused Hermes maintenance suites: 41 tests passed.
- Full maintenance suite: 56 tests passed.
- Python compile and `git diff --check`: passed.
- No live migration reconciliation, deployment, push, service mutation, or user/profile data mutation was performed.

Third review correction:
- Reworked `atomic_publish_bytes` so every exchanged entry is fingerprinted without following symlinks, restoration occurs only when both the displaced entry and candidate still match the proven exchange pair, and a foreign target is never blindly exchanged or unlinked.
- Removed unconditional cleanup of the exchanged path. The same-directory `.quarantine.*` entry is retained as recoverable evidence because Linux has no compare-and-unlink operation that could safely delete it after a concurrent replacement.
- Added deterministic regressions for symlink and directory replacement immediately after the first exchange, an exception raised after the kernel swap, a post-exchange durability exception, and successful-publication quarantine retention.

Third review verification:
- Focused `test_hermes*.py` maintenance suites: 45 tests passed.
- Full maintenance suite: 60 tests passed.
- `python3 -m py_compile scripts/maintenance/hermes_executor.py tests/maintenance/test_hermes_control.py`: passed.
- `git diff --check`: passed.
- No production service, data, credential, deployment, push, or commit action was performed.

Final adversarial correction:
- Removed automatic restoration after `RENAME_EXCHANGE`: a second pathname exchange cannot be a true CAS because either entry may change after validation. Success and ambiguous failure now retain both names without unlinking or moving any newly foreign entry.
- Added best-effort directory durability on exception paths and a hard limit of eight quarantines per target.
- Promotion reserves two launcher/dashboard slots plus one gateway rollback slot before journal creation, service stops, or installer execution. Recovery rechecks rollback capacity before arming or stopping consumers, so retention pressure is a preflight refusal rather than an outage.
- Added deterministic coverage for post-exchange symlink/directory substitution, wrapper and durability exceptions, no-second-exchange behavior, bounded retention, and promotion/rollback slot reservation.

Final verification:
- Full maintenance suite: 63 tests passed.
- Python compilation and `git diff --check`: passed.
- Independent final review: PASS, with no remaining correctness or safety blocker.
