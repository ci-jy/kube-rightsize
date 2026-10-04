#!/usr/bin/env bash
# End to end: recommend from the committed recording, write patches, apply them
# to the kind cluster and replay load, then gate on OOM kills, throttling and
# the frontend error rate. Results go to $OUT (out/validate.json).
#
#   scripts/validate.sh
#
# If the cluster does not exist it is created with `up.sh --squeeze` (the same
# setup the recording came from) and deleted again at the end. If Docker is not
# reachable, a private daemon is started under $TMPDIR (scripts/dockerd.sh) and
# stopped at the end.
#
# Environment: CLUSTER (rightsize), WINDOW per load window (5m),
#              RECORDING (recordings/boutique-30m.json), OUT (out/validate.json),
#              RECORD=10m to record a fresh window from the cluster first
#              (after WARMUP seconds, default 120) and recommend from that
#              instead of RECORDING; it is saved as out/recording.json,
#              PORT for the Prometheus port-forward (29091), KEEP_CLUSTER=1 to
#              leave a cluster this script created running.
set -euo pipefail
cd "$(dirname "$0")/.."

CLUSTER=${CLUSTER:-rightsize}
WINDOW=${WINDOW:-5m}
RECORDING=${RECORDING:-recordings/boutique-30m.json}
OUT=${OUT:-out/validate.json}
RECORD=${RECORD:-}
WARMUP=${WARMUP:-120}
PORT=${PORT:-29091}
KEEP_CLUSTER=${KEEP_CLUSTER:-0}
ctx="kind-$CLUSTER"

work=$(mktemp -d "${TMPDIR:-/tmp}/rightsize-validate.XXXXXX")
started_docker=0
created_cluster=0
pf=""
cleanup() {
  [[ -n $pf ]] && kill "$pf" 2>/dev/null || true
  if [[ $created_cluster == 1 && $KEEP_CLUSTER != 1 ]]; then
    echo "==> deleting cluster $CLUSTER"
    kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
  fi
  [[ $started_docker == 1 ]] && scripts/dockerd.sh stop || true
  rm -rf "$work"
}
trap cleanup EXIT

if ! docker info >/dev/null 2>&1; then
  echo "==> Docker not reachable; starting a private daemon"
  eval "$(scripts/dockerd.sh start)"
  started_docker=1
fi

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  created_cluster=1
  CLUSTER=$CLUSTER NODE_MEMORY=${NODE_MEMORY:-3g} scripts/up.sh --squeeze
fi

kubectl --context "$ctx" -n monitoring port-forward svc/prometheus-server "$PORT:80" >/dev/null &
pf=$!
for _ in $(seq 30); do
  curl -sf "http://127.0.0.1:$PORT/-/ready" >/dev/null && break
  sleep 1
done

if [[ -n $RECORD ]]; then
  secs=$(python3 -c "from rightsize.cli import parse_duration; print(int(parse_duration('$RECORD')))")
  echo "==> recording $RECORD of load after ${WARMUP}s warm-up"
  sleep $((WARMUP + secs))
  RECORDING=out/recording.json
  python3 -m rightsize record --prometheus "http://127.0.0.1:$PORT" --namespace boutique \
    --duration "$RECORD" --out "$RECORDING"
fi

echo "==> recommend and patch"
python3 -m rightsize recommend --from-recording "$RECORDING" --out out/recommendations.json -q

# Render the patches over the live Deployments as a Kustomize overlay first, so a
# patch that does not merge fails here rather than half-applied.
mkdir -p "$work/base"
kubectl --context "$ctx" -n boutique get deployments -o yaml > "$work/base/deployments.yaml"
printf 'resources:\n  - deployments.yaml\n' > "$work/base/kustomization.yaml"
python3 -m rightsize patch --recommendations out/recommendations.json --out "$work/overlay" --base "$work/base" -q
kubectl kustomize "$work/overlay" > "$work/rendered.yaml"
echo "    overlay renders ($(grep -c '^kind: Deployment' "$work/rendered.yaml") deployments)"
python3 -m rightsize patch --recommendations out/recommendations.json --out out/patches -q

echo "==> validate (two ${WINDOW} load windows)"
python3 -m rightsize validate --context "$ctx" --patches out/patches \
  --prometheus "http://127.0.0.1:$PORT" --window "$WINDOW" \
  --recommendations out/recommendations.json --out "$OUT"
