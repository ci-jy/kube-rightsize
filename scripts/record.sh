#!/usr/bin/env bash
# Port-forward Prometheus from the kind cluster and record the boutique
# namespace. Waits out WARMUP first so the window only holds steady load.
#
#   scripts/record.sh recordings/boutique-30m.json
#
# Environment: CLUSTER (rightsize), DURATION (30m), WARMUP seconds (300),
#              PORT for the local port-forward (29090).
set -euo pipefail
out=${1:?usage: record.sh OUT.json}
CLUSTER=${CLUSTER:-rightsize}
DURATION=${DURATION:-30m}
WARMUP=${WARMUP:-300}
PORT=${PORT:-29090}

kubectl --context "kind-$CLUSTER" -n monitoring port-forward svc/prometheus-server "$PORT:80" >/dev/null &
pf=$!
trap 'kill $pf 2>/dev/null || true' EXIT

for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:$PORT/-/ready" >/dev/null && break
  sleep 1
done

# The recording window ends now, so sleep until DURATION of post-warmup data exists.
secs=$(python3 -c "from rightsize.cli import parse_duration; print(int(parse_duration('$DURATION')))")
echo "waiting $((WARMUP + secs))s for ${WARMUP}s warmup + $DURATION of load"
sleep $((WARMUP + secs))
python3 -m rightsize record --prometheus "http://127.0.0.1:$PORT" --namespace boutique \
  --duration "$DURATION" --out "$out"
