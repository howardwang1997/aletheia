#!/usr/bin/env bash
# Launch the AUTONOMOUS-DISCOVERY ARC e2e on a DIRECT connection (no FlClash / no proxy).
#
# Identical ergonomics + pre-flight to run_e2e_direct.sh, but runs the ARC
# (scripts/real_discovery_campaign_e2e.py, discovery_enabled=True) instead of the cuprate
# campaign. The discovery loop itself is Claude-FREE (grok ideator + grok novelty gate) and a
# banked survivor carries its own verified demo code (reused without Claude authoring), so DIRECT
# egress is fine for exercising the discover->demonstrate->belief arc — only the >=2-vendor audit
# will starve on a grok-only floor (accepted).
#
# IMPORTANT — FlClash TUN mode: if FlClash runs as a TUN/transparent proxy (a utun* device,
# e.g. gateway 28.0.0.1), it captures ALL traffic at the network layer. Unsetting proxy env
# vars CANNOT bypass that — you must turn FlClash OFF in the app first. This script's pre-flight
# will tell you which case you're in: if it FAILS, FlClash is still in the path → disable it.
#
# Usage:
#   bash scripts/run_arc_direct.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) strip every proxy env var so nothing routes the CLI through FlClash via env.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy 2>/dev/null || true
export NO_PROXY="*" no_proxy="*"

# 2) pre-flight: prove api.anthropic.com is reachable DIRECTLY (forced no-proxy), fail fast.
echo "[arc] pre-flight: forced-direct TLS to api.anthropic.com ..."
code=$(curl --noproxy '*' -sS --max-time 20 -o /tmp/aletheia_preflight_body \
            -w '%{http_code}' -X POST https://api.anthropic.com/v1/messages \
            -H 'content-type: application/json' -d '{}' 2>/tmp/aletheia_preflight_err) || {
  echo "[arc] ✗ PRE-FLIGHT FAILED — cannot reach api.anthropic.com directly:"
  sed 's/^/      /' /tmp/aletheia_preflight_err
  echo "[arc]   → FlClash is almost certainly still in the path (TUN/transparent mode)."
  echo "[arc]   → Turn FlClash OFF in the app, then re-run this script."
  exit 1
}
if [[ "$code" == "401" || "$code" == "400" ]]; then
  echo "[arc] ✓ reachable directly (HTTP $code from Anthropic — TLS + route OK)"
else
  echo "[arc] ⚠ unexpected HTTP $code — body:"; sed 's/^/      /' /tmp/aletheia_preflight_body
  echo "[arc]   A non-401/400 may mean a captive portal or interceptor is in the path."
  echo "[arc]   Aborting to avoid burning a run. Verify the link, then re-run."
  exit 1
fi

# 3) launch the ARC in the conda env. The header prints "discovery_enabled=True" — that is how you
#    know the RIGHT script is running (the cuprate campaign does not print it).
echo "[arc] launching the autonomous-discovery arc (direct) ..."
exec conda run -n aletheia python scripts/real_discovery_campaign_e2e.py "$@"
