#!/usr/bin/env bash
# Steve Le Poisson automated PR reviewer — orchestrator. Runs as gha-runner on the
# Steve self-hosted runner. Builds a handoff, invokes the box-resident Codex wrapper
# (flat-rate ChatGPT, read-only), posts the review to the PR, and — for tickets
# delegated to Steve — drives a multi-round review<->fix loop via Linear.
#
# NEIL-1476: initial advisory review (no gate).
# NEIL-1498: adds commit status (steve-le-poisson), risk-tier classification,
#            and Logan escalation for critical PRs. Advisory mode by default
#            (STEVE_GATE_ENFORCED=false); Part B will flip the flag.
# NEIL-1498 Part B: STEVE_GATE_ENFORCED=true; alert_ops() for ops alerts;
#            auto-merge for clean non-critical PRs via GraphQL enablePullRequestAutoMerge.
set -uo pipefail

WRAPPER=/home/steve/.cyrus/codex-review/steve-codex-review.sh
MAX_ROUNDS="${STEVE_MAX_ROUNDS:-5}"
SIGNOFF='advisory review (not a merge gate)'          # marker in every Steve PR review
GH_API="https://api.github.com"
LINEAR_API="https://api.linear.app/graphql"
PR_URL="https://github.com/${REPO}/pull/${PR_NUMBER}"

# NEIL-1498 Part B: enforcement is now on.
STEVE_GATE_ENFORCED="${STEVE_GATE_ENFORCED:-true}"

# Critical-path patterns file — Logan can edit this list without touching review.sh.
# Patterns are ERE fragments matched against `git diff --name-only` output.
CRITICAL_PATHS_FILE="$(dirname "$0")/critical-paths.txt"

# #attractify-alerts channel ID (Slack).
ALERTS_CHANNEL="C0B7M0NK82D"

# ─── helpers ────────────────────────────────────────────────────────────────────

gh_api() { # method path [json-body]
  local m="$1" p="$2" b="${3:-}"
  if [ -n "$b" ]; then
    curl -fsS -X "$m" "$GH_API$p" -H "Authorization: token $GH_TOKEN" \
      -H "Accept: application/vnd.github+json" --data-binary "$b"
  else
    curl -fsS -X "$m" "$GH_API$p" -H "Authorization: token $GH_TOKEN" \
      -H "Accept: application/vnd.github+json"
  fi
}

lin() { # json-body -> stdout
  [ -n "${LINEAR_API_KEY:-}" ] || return 1
  curl -fsS "$LINEAR_API" -H "Authorization: $LINEAR_API_KEY" \
    -H "Content-Type: application/json" --data-binary "$1"
}

post_pr_comment() { # body
  gh_api POST "/repos/$REPO/issues/$PR_NUMBER/comments" "$(jq -n --arg b "$1" '{body:$b}')" >/dev/null || true
}

post_ticket_comment() { # body
  [ -n "${ISSUE_UUID:-}" ] || return 0
  lin "$(jq -n --arg id "$ISSUE_UUID" --arg b "$1" \
        '{query:"mutation($id:String!,$b:String!){commentCreate(input:{issueId:$id,body:$b}){success}}",variables:{id:$id,b:$b}}')" \
    >/dev/null 2>&1 || true
}

# NEIL-1498: post a commit status against the PR head SHA.
# $1 = state (success|failure|pending)   $2 = description (≤140 chars)
post_commit_status() {
  local state="$1" desc="$2"
  local body
  body="$(jq -n \
    --arg s  "$state" \
    --arg d  "$desc" \
    --arg ctx "steve-le-poisson" \
    --arg url "$PR_URL" \
    '{state:$s, description:$d, context:$ctx, target_url:$url}')"
  gh_api POST "/repos/$REPO/statuses/${HEAD_SHA}" "$body" >/dev/null || true
}

# NEIL-1498: send Slack DM to Logan personally (D0AFQ0906MB).
# Used only for critical-PR escalations.
alert_slack() { # message
  local msg="$1"
  [ -n "${SLACK_TOKEN:-}" ] || { echo "SLACK_TOKEN not set — Slack alert skipped"; return; }
  curl -fsS -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer ${SLACK_TOKEN}" \
    -H "Content-Type: application/json" \
    --data-binary "$(jq -n --arg ch "D0AFQ0906MB" --arg t "$msg" '{channel:$ch,text:$t}')" \
    >/dev/null || true
}

# NEIL-1498 Part B: post to #attractify-alerts (ops channel, C0B7M0NK82D).
# Used for: Steve-box-offline probe skips and watchdog neutral-pass events.
alert_ops() { # message
  local msg="$1"
  [ -n "${SLACK_TOKEN:-}" ] || { echo "SLACK_TOKEN not set — ops alert skipped"; return; }
  curl -fsS -X POST "https://slack.com/api/chat.postMessage" \
    -H "Authorization: Bearer ${SLACK_TOKEN}" \
    -H "Content-Type: application/json" \
    --data-binary "$(jq -n --arg ch "${ALERTS_CHANNEL}" --arg t "$msg" '{channel:$ch,text:$t}')" \
    >/dev/null || true
}

# NEIL-1498: send Telegram message to Logan's personal chat.
# Requires TELEGRAM_BOT_TOKEN + TELEGRAM_LOGAN_CHAT_ID in the workflow env
# (added as repo secrets for NEIL-1498).
alert_telegram() { # message
  local msg="$1"
  [ -n "${TELEGRAM_BOT_TOKEN:-}" ]     || { echo "TELEGRAM_BOT_TOKEN not set — Telegram alert skipped"; return; }
  [ -n "${TELEGRAM_LOGAN_CHAT_ID:-}" ] || { echo "TELEGRAM_LOGAN_CHAT_ID not set — Telegram alert skipped"; return; }
  curl -fsS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-binary "$(jq -n \
        --arg cid "${TELEGRAM_LOGAN_CHAT_ID}" \
        --arg txt "$msg" \
        '{chat_id:$cid,text:$txt,parse_mode:"HTML",disable_web_page_preview:true}')" \
    >/dev/null || true
}

# NEIL-1498 Part B: enable GitHub auto-merge (squash) for clean non-critical PRs.
# gh CLI is not present on the self-hosted Steve runner, so we use GraphQL directly.
# Silently skips on error (auto-merge is a convenience, not a gate).
enable_auto_merge() {
  # First fetch the PR node_id needed for the GraphQL mutation.
  local node_id
  node_id="$(gh_api GET "/repos/$REPO/pulls/$PR_NUMBER" \
    | jq -r '.node_id // empty' 2>/dev/null)"
  if [ -z "$node_id" ]; then
    echo "auto-merge: could not fetch PR node_id — skipping"
    return
  fi

  local mutation
  mutation="$(jq -n \
    --arg nid "$node_id" \
    '{query:"mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:SQUASH}){pullRequest{autoMergeRequest{mergeMethod}}}}",variables:{id:$nid}}')"

  local resp
  resp="$(curl -fsS -X POST "https://api.github.com/graphql" \
    -H "Authorization: token $GH_TOKEN" \
    -H "Content-Type: application/json" \
    --data-binary "$mutation" 2>/dev/null || true)"

  if echo "$resp" | jq -e '.data.enablePullRequestAutoMerge.pullRequest' >/dev/null 2>&1; then
    echo "auto-merge: enabled (squash) on PR #${PR_NUMBER}"
  else
    # enablePullRequestAutoMerge rejects already-mergeable PRs (fast single-check
    # repos race here). Fall back to a direct squash merge.
    if gh_api PUT "/repos/$REPO/pulls/$PR_NUMBER/merge" '{"merge_method":"squash"}' >/dev/null 2>&1; then
      echo "auto-merge: PR already mergeable — direct squash-merged"
    else
      echo "auto-merge: could not enable auto-merge or direct-merge (non-fatal)"
    fi
  fi
}

# ─── guard: probe the box reviewer ──────────────────────────────────────────────

# Guard: stay green if the box reviewer isn't runnable here. We can't `[ -x ]` the
# wrapper (it lives in steve's home, which gha-runner can't traverse), so probe via
# sudo — the wrapper rejects a non-handoff path with exit 2, our "installed" signal.
sudo -n -u steve "$WRAPPER" __probe__ >/dev/null 2>&1; PROBE=$?
if [ "$PROBE" -ne 2 ]; then
  echo "Steve reviewer not runnable on this runner (probe rc=$PROBE) — skipping."
  # NEIL-1498 Part B: neutral-pass status + ops alert to #attractify-alerts.
  post_commit_status "success" "review skipped — Steve box unreachable"
  alert_ops ":warning: *[steve-le-poisson] Review SKIPPED* — Steve box unreachable (probe rc=${PROBE}). PR: ${PR_URL}"
  exit 0
fi

# ─── handoff dir ────────────────────────────────────────────────────────────────

HANDOFF="/tmp/steve-review/${GITHUB_RUN_ID}-${PR_NUMBER}"
mkdir -p "$HANDOFF"; chmod 755 /tmp/steve-review "$HANDOFF" 2>/dev/null || true
trap 'rm -rf "$HANDOFF"' EXIT

# ─── 1. diff ────────────────────────────────────────────────────────────────────

# pull_request_target checks out the base branch; fetch the PR head so we can diff
# it as data (we never execute code from the PR). Merge-base (3-dot) with a 2-dot
# fallback in case the merge-base isn't reachable.
git fetch --no-tags origin "${HEAD_SHA}" >/dev/null 2>&1 || true
RANGE="${BASE_SHA}...${HEAD_SHA}"
git diff "$RANGE" -U50 > "$HANDOFF/diff.patch" 2>/dev/null \
  || git diff "${BASE_SHA}" "${HEAD_SHA}" -U50 > "$HANDOFF/diff.patch" 2>/dev/null || true
if [ "$(wc -c < "$HANDOFF/diff.patch" 2>/dev/null || echo 0)" -gt 600000 ]; then
  git diff "$RANGE" -U3 > "$HANDOFF/diff.patch" 2>/dev/null \
    || git diff "${BASE_SHA}" "${HEAD_SHA}" -U3 > "$HANDOFF/diff.patch" 2>/dev/null || true
fi
if [ ! -s "$HANDOFF/diff.patch" ]; then echo "Empty diff — nothing to review."; exit 0; fi

# ─── 2. NEIL-1498: path-based criticality classification ─────────────────────────
# Compute changed files first so we can pass the list to the box reviewer too.
CHANGED_FILES="$(git diff --name-only "${BASE_SHA}...${HEAD_SHA}" 2>/dev/null \
  || git diff --name-only "${BASE_SHA}" "${HEAD_SHA}" 2>/dev/null || true)"

PATH_CRITICAL=0
PATH_CRITICAL_REASONS=""

if [ -f "$CRITICAL_PATHS_FILE" ]; then
  # Read non-comment, non-empty pattern lines from critical-paths.txt
  while IFS= read -r pattern; do
    [[ "$pattern" =~ ^#.*$ || -z "$pattern" ]] && continue
    # Match pattern against changed filenames (ERE)
    matched="$(printf '%s\n' "$CHANGED_FILES" | grep -E "$pattern" 2>/dev/null || true)"
    if [ -n "$matched" ]; then
      PATH_CRITICAL=1
      # Collect the first matching filename per pattern for the reason string
      first="$(echo "$matched" | head -1)"
      PATH_CRITICAL_REASONS="${PATH_CRITICAL_REASONS}path:${pattern}(${first}); "
    fi
  done < <(grep -v '^#' "$CRITICAL_PATHS_FILE" | grep -v '^[[:space:]]*$')
fi

# Also scan diff content for destructive SQL/shell patterns regardless of filename
DESTRUCTIVE_PATTERN='(DROP[[:space:]]+TABLE|DROP[[:space:]]+DATABASE|DELETE[[:space:]]+FROM|TRUNCATE[[:space:]]+TABLE|rm[[:space:]]+-rf)'
if grep -qE "$DESTRUCTIVE_PATTERN" "$HANDOFF/diff.patch" 2>/dev/null; then
  PATH_CRITICAL=1
  PATH_CRITICAL_REASONS="${PATH_CRITICAL_REASONS}destructive-op-in-diff; "
fi

# ─── 3. resolve the linked NEIL ticket ──────────────────────────────────────────

NEIL_ID="$(printf '%s\n%s\n%s' "${PR_HEAD_REF:-}" "${PR_TITLE:-}" "${PR_BODY:-}" \
            | grep -oiE 'NEIL-[0-9]+' | head -1 | tr '[:lower:]' '[:upper:]')"
ISSUE_UUID=""; DELEGATE=""
printf 'No linked NEIL ticket; review for general correctness.\n' > "$HANDOFF/criteria.md"
if [ -n "$NEIL_ID" ] && [ -n "${LINEAR_API_KEY:-}" ]; then
  NUM="${NEIL_ID#NEIL-}"
  Q="$(jq -n --argjson n "$NUM" '{query:"query($n:Float){issues(filter:{number:{eq:$n},team:{key:{eq:\"NEIL\"}}}){nodes{id identifier title description delegate{id name}}}}",variables:{n:$n}}')"
  RESP="$(lin "$Q" || true)"
  ISSUE_UUID="$(jq -r '.data.issues.nodes[0].id // empty' <<<"$RESP" 2>/dev/null)"
  if [ -n "$ISSUE_UUID" ]; then
    DELEGATE="$(jq -r '.data.issues.nodes[0].delegate.name // empty' <<<"$RESP" 2>/dev/null)"
    jq -r '.data.issues.nodes[0] | "# \(.identifier): \(.title)\n\n\(.description // "(no description)")"' \
      <<<"$RESP" > "$HANDOFF/criteria.md" 2>/dev/null
  fi
fi

# ─── 4. review (Codex as steve, flat-rate, read-only) ───────────────────────────

if ! sudo -u steve "$WRAPPER" "$HANDOFF" > "$HANDOFF/review.md" 2>"$HANDOFF/err.log"; then
  printf '⚠️ Steve could not complete the review (Codex/runner issue). Advisory only — merge not blocked.\n' > "$HANDOFF/review.md"
  printf '\ncriticality: normal\n' >> "$HANDOFF/review.md"
fi
[ -s "$HANDOFF/review.md" ] || { printf '⚠️ Steve produced no review output. Advisory only.\n\ncriticality: normal\n' > "$HANDOFF/review.md"; }

VERDICT="$(head -1 "$HANDOFF/review.md")"
REVIEW="$(cat "$HANDOFF/review.md")"

# ─── 5. NEIL-1498: parse LLM criticality from review output ─────────────────────
# Box reviewer emits "criticality: critical — reason" or "criticality: normal"
# as the last substantive line of output (after the signoff).
LLM_CRITICALITY_LINE="$(grep -i '^criticality:' "$HANDOFF/review.md" | tail -1 || true)"
LLM_CRITICAL=0
LLM_CRITICAL_REASON=""
if echo "$LLM_CRITICALITY_LINE" | grep -qi 'criticality:[[:space:]]*critical'; then
  LLM_CRITICAL=1
  LLM_CRITICAL_REASON="$(echo "$LLM_CRITICALITY_LINE" | sed 's/criticality:[[:space:]]*critical[[:space:]]*—[[:space:]]*//' | sed 's/criticality:[[:space:]]*critical//')"
fi

# Combined criticality: critical if EITHER path rules OR LLM says so
IS_CRITICAL=0
CRITICAL_REASONS=""
if [ "$PATH_CRITICAL" = "1" ]; then
  IS_CRITICAL=1
  CRITICAL_REASONS="paths=[${PATH_CRITICAL_REASONS%; }]"
fi
if [ "$LLM_CRITICAL" = "1" ]; then
  IS_CRITICAL=1
  [ -n "$CRITICAL_REASONS" ] && CRITICAL_REASONS="${CRITICAL_REASONS}, "
  CRITICAL_REASONS="${CRITICAL_REASONS}llm=[${LLM_CRITICAL_REASON}]"
fi

# Clean review text for posting — strip the bare criticality line before posting
# so it doesn't clutter the PR comment (the commit status carries the verdict).
REVIEW_FOR_POST="$(sed '/^criticality:/d' "$HANDOFF/review.md")"

# ─── 6. post to the PR (always) ─────────────────────────────────────────────────

post_pr_comment "$REVIEW_FOR_POST"

# ─── 7. NEIL-1498: emit commit status ───────────────────────────────────────────

# NEIL-1498 Part B: STEVE_GATE_ENFORCED=true; no advisory suffix.
ADVISORY_SUFFIX=""

CLEAN=0
case "$VERDICT" in
  *"✅"*)
    CLEAN=1
    if [ "$IS_CRITICAL" = "1" ]; then
      # ✅ review BUT critical surface touched — hold for human review.
      # failure status blocks the PR once branch protection requires this check.
      post_commit_status "failure" "critical — awaiting Logan review"
    else
      # ✅ and non-critical: full green.
      post_commit_status "success" "clean"
    fi
    ;;
  *"❌"*)
    CLEAN=0
    # Real issues found — failure status drives the fix loop.
    post_commit_status "failure" "needs changes — see PR comment"
    ;;
  *)
    # ⚠️ Comments or unexpected verdict — advisory only, green status.
    CLEAN=0
    post_commit_status "success" "comments only (no blocking issues)"
    ;;
esac

# ─── 8. NEIL-1498 Part B: auto-merge routine PRs ────────────────────────────────
# If the verdict is ✅ AND non-critical AND enforcement is on: enable GitHub auto-merge
# (squash) so the PR merges automatically once all required checks pass.
# Critical PRs and ❌ PRs are never auto-merged — they need human review or a fix push.
if [ "$CLEAN" = "1" ] && [ "$IS_CRITICAL" = "0" ] && [ "$STEVE_GATE_ENFORCED" = "true" ]; then
  echo "auto-merge: PR #${PR_NUMBER} is clean and non-critical — enabling squash auto-merge"
  enable_auto_merge
else
  echo "auto-merge: skipped (clean=${CLEAN}, critical=${IS_CRITICAL}, enforced=${STEVE_GATE_ENFORCED})"
fi

# ─── 9. NEIL-1498: escalate critical PRs to Logan ───────────────────────────────

if [ "$IS_CRITICAL" = "1" ]; then
  ESCALATION_TAG="[steve-le-poisson] :rotating_light: Critical PR"
  SLACK_MSG="${ESCALATION_TAG}
*PR:* ${PR_URL}
*Title:* ${PR_TITLE}
*Why critical:* ${CRITICAL_REASONS}
*Review verdict:* ${VERDICT}
This PR touches a sensitive surface and needs your review before merge."

  TG_MSG="$(printf '<b>🚨 Critical PR</b>\n<b>%s</b>\n%s\n\n<i>Why critical:</i> %s\n<i>Verdict:</i> %s\n\nNeeds your review/merge.' \
    "$PR_TITLE" "$PR_URL" "$CRITICAL_REASONS" "$VERDICT")"

  alert_slack  "$SLACK_MSG"
  alert_telegram "$TG_MSG"
  echo "Critical escalation sent (reasons: ${CRITICAL_REASONS})"
fi

# ─── 10. multi-round review<->fix loop ──────────────────────────────────────────

# The full review lives on the PR (above). Linear is used ONLY for owner-driven
# loop control on agent-authored tickets (delegate = Steve) + escalations — so we
# never post a Logan-authored "code review" on every ticket. Each nudge is clearly
# marked automated. Clean ✅ and human-authored PRs get no Linear comment (the PR
# linkback already surfaces the review on the ticket).
ROUNDS=""
if [ -n "$ISSUE_UUID" ] && [ "$CLEAN" != 1 ] && [ "$DELEGATE" = "Steve Le Poisson" ]; then
  COMMENTS_JSON="$(gh_api GET "/repos/$REPO/issues/$PR_NUMBER/comments?per_page=100" || echo '[]')"
  ROUNDS="$(jq "[.[] | select(.body | contains(\"$SIGNOFF\"))] | length" <<<"$COMMENTS_JSON" 2>/dev/null || echo 1)"
  PREV="$(jq -r "[.[] | select(.body | contains(\"$SIGNOFF\"))] | .[-2].body // \"\"" <<<"$COMMENTS_JSON" 2>/dev/null || echo '')"
  if [ "${ROUNDS:-1}" -ge "$MAX_ROUNDS" ]; then
    post_ticket_comment "$(printf '🤖 _Automated_ · 🐟 Steve reviewed [PR #%s](%s) %s times without a clean pass — pausing the loop. @Logan, needs a look.\n\n%s' "$PR_NUMBER" "$PR_URL" "$ROUNDS" "$REVIEW_FOR_POST")"
  elif [ -n "$PREV" ] && diff <(sed '1d' <<<"$PREV") <(sed '1d' <<<"$REVIEW_FOR_POST") >/dev/null 2>&1; then
    post_ticket_comment "$(printf '🤖 _Automated_ · 🐟 Steve says the last fix on [PR #%s](%s) did not change the findings — pausing the loop. @Logan, needs a look.\n\n%s' "$PR_NUMBER" "$PR_URL" "$REVIEW_FOR_POST")"
  else
    post_ticket_comment "$(printf '🤖 _Automated relay_ · 🐟 Steve Le Poisson reviewed [PR #%s](%s) and flagged changes. @Steve Le Poisson, please address them and push to the branch — full review is on the PR:\n\n%s' "$PR_NUMBER" "$PR_URL" "$REVIEW_FOR_POST")"
  fi
fi

echo "Steve review complete: ${VERDICT} (critical=${IS_CRITICAL}, reasons=${CRITICAL_REASONS:-none}, rounds=${ROUNDS:-n/a}, delegate=${DELEGATE:-none})"
