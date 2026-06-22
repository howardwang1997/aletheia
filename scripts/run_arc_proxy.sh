#!/usr/bin/env bash
# Launch the AUTONOMOUS-DISCOVERY ARC e2e THROUGH the proxy (FlClash ON).
#
# Use this (not run_arc_direct.sh / run_e2e_direct.sh) when you want the live-Claude calls to go
# over FlClash — the proxy fixes the long-stream ECONNRESET/"Connection closed" that flakes the
# survey + authoring on a bare direct link. grok still answers through the proxy; the >=2-vendor
# audit may still starve (accepted). Header prints "discovery_enabled=True" = the right script.
#
# Unlike the direct wrappers, this does NOT strip proxy env vars — FlClash stays in the path
# (TUN/transparent or system-proxy). Turn FlClash ON before running.
#
# Usage:
#   bash scripts/run_arc_proxy.sh
set -euo pipefail
cd "$(dirname "$0")/.."

# pre-flight: prove api.anthropic.com is reachable via the CURRENT path (proxy in place).
# An unauthenticated POST should return 401/400 — that round-trip proves TLS + route work.
echo "[arc-proxy] pre-flight: TLS to api.anthropic.com via the current path (FlClash) ..."
code=$(curl -sS --max-time 25 -o /tmp/aletheia_preflight_body \
            -w '%{http_code}' -X POST https://api.anthropic.com/v1/messages \
            -H 'content-type: application/json' -d '{}' 2>/tmp/aletheia_preflight_err) || {
  echo "[arc-proxy] ✗ PRE-FLIGHT FAILED — cannot reach api.anthropic.com:"
  sed 's/^/      /' /tmp/aletheia_preflight_err
  echo "[arc-proxy]   → Is FlClash ON and a working node selected? Check the FlClash app, then re-run."
  exit 1
}
if [[ "$code" == "401" || "$code" == "400" ]]; then
  echo "[arc-proxy] ✓ reachable via proxy (HTTP $code from Anthropic — TLS + route OK)"
else
  echo "[arc-proxy] ⚠ unexpected HTTP $code — body:"; sed 's/^/      /' /tmp/aletheia_preflight_body
  echo "[arc-proxy]   A non-401/400 may mean the proxy node is intercepting/blocking. Verify, then re-run."
  exit 1
fi

echo "[arc-proxy] launching the autonomous-discovery arc (via proxy) ..."
exec conda run -n aletheia python scripts/real_discovery_campaign_e2e.py "$@"
