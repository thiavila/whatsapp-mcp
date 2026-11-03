#!/usr/bin/env bash
# Tests the exact commit in a clean archive, attests, then pushes once.
set -Eeuo pipefail
mode="${1:-}"
[[ -z "$mode" || "$mode" == --dry-run || "$mode" == --no-push ]] || exit 2
root=$(git rev-parse --show-toplevel)
cd "$root"
sha=$(git rev-parse HEAD)
branch=$(git branch --show-current)
[[ -n "$branch" && "$branch" != main ]] || { echo 'Use a PR branch'; exit 1; }
[[ $(git remote get-url origin) == *thiavila/whatsapp-mcp.git ]] || exit 1
git fetch --quiet origin main
git merge-base --is-ancestor origin/main "$sha" || { echo 'Rebase onto origin/main'; exit 1; }
work=$(mktemp -d "${TMPDIR:-/tmp}/mcp-ci.XXXXXX")
trap 'rm -rf "$work"' EXIT
# Archive excludes .git, live store, .env and all untracked files.
git archive "$sha" | tar -x -C "$work"
while IFS= read -r line || [[ -n "$line" ]]; do
  [[ -z "$line" || "$line" == \#* ]] && continue
  (cd "$work" && bash -Eeuo pipefail -c "$line" </dev/null)
done < "$work/scripts/ci/local-ci.steps"
[[ "$mode" != --dry-run ]] || exit 0
login=$(gh api user --jq .login)
[[ "$login" == thiavila || "$login" == thiavila-hermes ]] || { echo 'Wrong GitHub account'; exit 1; }
# Non-branch ref makes the commit addressable before status; does not trigger CI.
ref="refs/local-ci/$sha"
git push --quiet origin "$sha:$ref"
gh api -X POST "repos/thiavila/whatsapp-mcp/statuses/$sha" -f state=success -f context=local-ci \
  -f description="code-only-v1 $sha" --silent
if [[ "$mode" != --no-push ]]; then git push origin "HEAD:refs/heads/$branch"; fi
git push --quiet origin ":$ref"
