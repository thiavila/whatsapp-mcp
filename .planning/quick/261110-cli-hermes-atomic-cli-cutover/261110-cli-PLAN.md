---
quick_id: 261110-cli
status: in-progress
---
# Atomically align Hermes CLI and dashboard during cutover

Implement the bounded controller correction from origin/main 0277d2a in an isolated PR branch. Do not deploy or mutate production.

1. Add fail-closed launcher identity checks that execute the staged official launcher and prove its version, candidate SHA, and install root. Admit takeover only when the existing global launcher identifies the expected previous SHA.
2. Publish the candidate launcher with a same-directory temporary file, file fsync, atomic replace, and directory fsync. Preserve the existing launcher backup and restore it through the rollback path.
3. After candidate `gateway install`, publish and re-prove the global launcher, align the backed-up dashboard unit ExecStart to that launcher, and fingerprint the resulting launcher plus gateway/dashboard units before service restart.
4. Add regressions for expected-owner takeover, foreign-launcher refusal without mutation, candidate/dashboard alignment, and rollback compatibility. Run focused tests, canonical code-only CI where feasible, static review, and commit locally without push.

Acceptance: no unrelated launcher is overwritten; candidate identity is proven before and after publication; dashboard starts through the candidate-bound global launcher; rollback restores the saved launcher/unit; journal installation fingerprints represent the aligned state; no production/user/profile mutation.
