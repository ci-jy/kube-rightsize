#!/usr/bin/env bash
# Create a single-node kind cluster running Online Boutique (with its
# loadgenerator) and the prometheus-community Prometheus chart.
#
#   scripts/up.sh            # create everything
#   scripts/up.sh --squeeze  # also give one service a too-small memory limit
#                            # so the recording contains real OOM kills
#
# Environment:
#   CLUSTER       kind cluster name               (default: rightsize)
#   NODE_MEMORY   memory cap for the kind node    (default: 4g)
#   BOUTIQUE_REF  microservices-demo release tag  (default: v0.10.3)
#   USERS         simulated loadgenerator users   (default: 10)
set -euo pipefail

CLUSTER=${CLUSTER:-rightsize}
NODE_MEMORY=${NODE_MEMORY:-4g}
BOUTIQUE_REF=${BOUTIQUE_REF:-v0.10.3}
USERS=${USERS:-10}
SQUEEZE=0
[[ "${1:-}" == "--squeeze" ]] && SQUEEZE=1

here=$(cd "$(dirname "$0")" && pwd)
ctx="kind-$CLUSTER"

fail() {
  echo "up.sh failed; deleting cluster $CLUSTER" >&2
  kind delete cluster --name "$CLUSTER" >/dev/null 2>&1 || true
}
trap fail ERR

if ! kind get clusters 2>/dev/null | grep -qx "$CLUSTER"; then
  kind create cluster --name "$CLUSTER" --config "$here/kind-config.yaml" --wait 120s
fi
# kind has no memory flag; cap the node container directly.
docker update --memory "$NODE_MEMORY" --memory-swap "$NODE_MEMORY" "$CLUSTER-control-plane" >/dev/null

echo "==> Prometheus"
helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null
helm upgrade --install prometheus prometheus-community/prometheus \
  --kube-context "$ctx" --namespace monitoring --create-namespace \
  --values "$here/prometheus-values.yaml" --wait --timeout 10m

echo "==> Online Boutique $BOUTIQUE_REF"
kubectl --context "$ctx" create namespace boutique --dry-run=client -o yaml | kubectl --context "$ctx" apply -f -
kubectl --context "$ctx" -n boutique apply -f \
  "https://raw.githubusercontent.com/GoogleCloudPlatform/microservices-demo/$BOUTIQUE_REF/release/kubernetes-manifests.yaml"
# The release manifest exposes the frontend as a LoadBalancer, which kind cannot serve.
kubectl --context "$ctx" -n boutique delete service frontend-external --ignore-not-found
kubectl --context "$ctx" -n boutique set env deployment/loadgenerator USERS="$USERS"

if [[ $SQUEEZE == 1 ]]; then
  # recommendationservice settles around 41Mi on kind; a 40Mi limit makes the kernel
  # OOM-kill it under load, which is exactly the censored case rightsize flags.
  kubectl --context "$ctx" -n boutique set resources deployment/recommendationservice \
    --requests=memory=32Mi --limits=memory=40Mi
fi

kubectl --context "$ctx" -n boutique wait --for=condition=available deployment --all --timeout=15m || {
  kubectl --context "$ctx" -n boutique get pods
  # A squeezed service is expected to crash-loop; anything else is a real failure.
  [[ $SQUEEZE == 1 ]] || exit 1
}
trap - ERR
kubectl --context "$ctx" -n boutique get pods
cat <<MSG

Cluster is up. Reach Prometheus with:
  kubectl --context $ctx -n monitoring port-forward svc/prometheus-server 29090:80
then record with:
  python3 -m rightsize record --prometheus http://127.0.0.1:29090 --namespace boutique --duration 30m --out recordings/boutique-30m.json
MSG
