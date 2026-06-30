#!/usr/bin/env bash
# Re-apply local Hermes code customizations after `hermes update`, which checks
# out `main` and reverts them. Safe + idempotent: rebases the
# local-customizations branch onto the new main, re-asserts config/code patches,
# then restarts the gateway(s). Run this AFTER every `hermes update`.
#
# Sanitized excerpt of a personal Hermes customization, published as a showcase
# of keeping a fork alive across upstream resets. Replace the placeholders
# (<CHANNEL_ID>, profile/service names) and the example "is my patch present?"
# checks with your own.
set -euo pipefail

HHOME="$HOME/.hermes"
AGENT="$HHOME/hermes-agent"
PROFILE="${HERMES_PROFILE:-main}"         # your ~/.hermes/profiles/<PROFILE>
GATEWAYS="hermes-gateway.service"          # space-separated if you run several
cd "$AGENT"

echo "[restore] current branch: $(git branch --show-current)"
if ! git rev-parse --verify local-customizations >/dev/null 2>&1; then
  echo "[restore] ERROR: local-customizations branch is gone. Recover from reflog." >&2
  exit 1
fi

# Stash any stray working-tree changes so checkout/rebase is clean.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "[restore] stashing stray working-tree changes"; git stash push -u -m "restore_after_update autostash"
fi

git checkout local-customizations
echo "[restore] rebasing onto updated main..."
if git rebase main; then
  echo "[restore] rebase OK. our commits:"; git log --oneline main..local-customizations
else
  echo "[restore] REBASE CONFLICT — resolve manually (git status), then 'git rebase --continue'." >&2
  exit 2
fi

# Verify your code customizations are actually present before restarting.
# NOTE: the kimchi/0.1.13 WAF User-Agent is NO LONGER a code patch — it now lives
# in config.yaml `model.default_headers` (data dir → survives `hermes update`) and
# is re-asserted by heal_kimchi.py in the invariants block below. So it is
# intentionally NOT verified against the agent code here anymore.
# (Replace these greps with checks for whatever your branch actually adds.)
grep -q "def _mirror_to_agent_logs" cron/jobs.py \
  && grep -q "default_scope" plugins/memory/holographic/store.py \
  && echo "[restore] code customizations present ✓" \
  || { echo "[restore] ERROR: customizations missing after rebase" >&2; exit 3; }

# Per-channel Discord models (discord.channel_models) are a code patch to
# gateway/run.py that is NOT carried on the local-customizations branch (that
# branch is based on an older main; re-inserting via a stable-anchor script is
# safer than a rebase). Idempotent + fail-safe: no-op if present, never leaves
# run.py broken. Done BEFORE the restart so the gateway loads the patched code.
echo "[restore] re-asserting discord.channel_models patch..."
python3 "$(dirname "$0")/apply_channel_models_patch.py" 2>&1 \
  | sed 's/^/[restore]   /' \
  || echo "[restore]   ⚠ channel_models re-apply reported an issue (see above)"

echo "[restore] restarting gateway(s)..."
# shellcheck disable=SC2086
systemctl --user restart $GATEWAYS
sleep 8
for g in $GATEWAYS; do
  echo "[restore] $g = $(systemctl --user is-active "$g")"
done

# ---------------------------------------------------------------------------
# Config/data invariants (NON-FATAL). These live in ~/.hermes (the data dir),
# OUTSIDE the hermes-agent git repo, so `hermes update` does NOT revert them —
# this block only flags accidental drift, it never edits anything.
#   • Gateway lifecycle pings route to a status channel (not the daily channel)
#   • Morning brief self-delivers to a daily channel (deliver=local in cron)
# ---------------------------------------------------------------------------
STATUS_CH="<CHANNEL_ID>"   # your gateway status channel id
echo "[restore] --- config invariants (informational) ---"
for envf in "$HHOME/.env" "$HHOME/profiles/$PROFILE/.env"; do
  val=$(grep -E '^DISCORD_HOME_CHANNEL=' "$envf" 2>/dev/null | head -1 | cut -d= -f2)
  if [ "$val" = "$STATUS_CH" ]; then
    echo "[restore]   ✓ $(basename "$(dirname "$envf")")/.env home-channel → status channel"
  else
    echo "[restore]   ⚠ $envf DISCORD_HOME_CHANNEL=$val (expected $STATUS_CH)"
  fi
done
if grep -q "def send_discord" "$HHOME/scripts/morning-brief.py" 2>/dev/null; then
  echo "[restore]   ✓ morning-brief.py self-delivers to Discord"
else
  echo "[restore]   ⚠ morning-brief.py missing send_discord() self-delivery"
fi
# kimchi default + WAF User-Agent. Config-based (survives `hermes update`), but
# re-assert immediately so a config reset / fresh `hermes setup` self-corrects.
echo "[restore]   re-asserting kimchi default config..."
python3 "$(dirname "$0")/heal_kimchi.py" 2>&1 | sed 's/^/[restore]   /' \
  || echo "[restore]   ⚠ heal_kimchi reported an issue (see above)"
echo "[restore] -------------------------------------------"
