#!/usr/bin/env bash
# Launch the K2-campaign e2e on a DIRECT connection (no FlClash / no proxy).
#
# Why: the proxy was the source of the long-stream ECONNRESETs that kept degrading the
# demonstration-authoring call. A direct link removes that failure mode at the root.
#
# IMPORTANT — FlClash TUN mode: if FlClash runs as a TUN/transparent proxy (a utun* device,
# e.g. gateway 28.0.0.1), it captures ALL traffic at the network layer. Unsetting proxy env
# vars CANNOT bypass that — you must turn FlClash OFF in the app first. This script's pre-flight
# will tell you which case you're in: if it FAILS, FlClash is still in the path → disable it.
#
# Usage:
#   bash scripts/run_e2e_direct.sh                 # fresh run (new run_id)
#   bash scripts/run_e2e_direct.sh --resume <id>   # resume an existing run
set -euo pipefail
cd "$(dirname "$0")/.."

# 1) strip every proxy env var so nothing routes the CLI through FlClash via env.
unset HTTP_PROXY HTTPS_PROXY ALL_PROXY http_proxy https_proxy all_proxy 2>/dev/null || true
export NO_PROXY="*" no_proxy="*"

# 2) pre-flight: prove api.anthropic.com is reachable DIRECTLY (forced no-proxy), fail fast.
#    An unauthenticated POST should return HTTP 401 (authentication_error) — that round-trip
#    proves TLS + route work. A connection/timeout/cert error means direct is still blocked.
echo "[direct] pre-flight: forced-direct TLS to api.anthropic.com ..."
code=$(curl --noproxy '*' -sS --max-time 20 -o /tmp/aletheia_preflight_body \
            -w '%{http_code}' -X POST https://api.anthropic.com/v1/messages \
            -H 'content-type: application/json' -d '{}' 2>/tmp/aletheia_preflight_err) || {
  echo "[direct] ✗ PRE-FLIGHT FAILED — cannot reach api.anthropic.com directly:"
  sed 's/^/      /' /tmp/aletheia_preflight_err
  echo "[direct]   → FlClash is almost certainly still in the path (TUN/transparent mode)."
  echo "[direct]   → Turn FlClash OFF in the app, then re-run this script."
  exit 1
}
if [[ "$code" == "401" || "$code" == "400" ]]; then
  echo "[direct] ✓ reachable directly (HTTP $code from Anthropic — TLS + route OK)"
else
  echo "[direct] ⚠ unexpected HTTP $code — body:"; sed 's/^/      /' /tmp/aletheia_preflight_body
  echo "[direct]   A non-401/400 may mean a captive portal or interceptor is in the path."
  echo "[direct]   Aborting to avoid burning a run. Verify the link, then re-run."
  exit 1
fi

# 3) launch the e2e in the conda env, passing through any args (e.g. --resume <id>).
echo "[direct] launching e2e (direct) ..."
exec conda run -n aletheia python scripts/real_k2_campaign_e2e.py "$@"
