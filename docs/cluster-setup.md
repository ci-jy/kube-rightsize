# Test cluster setup

`scripts/up.sh` builds a single-node kind cluster, installs the prometheus-community Prometheus Helm chart (server and kube-state-metrics) into `monitoring`, and deploys Online Boutique into `boutique`. The frontend's LoadBalancer service is removed because kind can't serve it.

## up.sh

- `--squeeze`: give `recommendationservice` a 32Mi request and 40Mi limit so it gets OOM-killed under load. It is expected to crash-loop; the script tolerates that.
- `CLUSTER`: kind cluster name (default `rightsize`).
- `NODE_MEMORY`: memory cap for the kind node container (default `4g`). kind has no memory flag, so the script uses `docker update`. The node was using about 1.3 GiB during my run.
- `BOUTIQUE_REF`: microservices-demo release tag (default `v0.10.3`).
- `USERS`: simulated load generator users (default 10).

It takes around 6 minutes.

## record.sh

`scripts/record.sh OUT.json` port-forwards Prometheus, waits for the warm-up plus the duration so the whole window is post-warm-up load, then records the `boutique` namespace.

- `DURATION` (default `30m`), `WARMUP` in seconds (default 300), `PORT` for the port-forward (default 29090), `CLUSTER` (default `rightsize`).

The committed recording used `WARMUP=120 DURATION=30m`.

## validate.sh

`scripts/validate.sh` is the end-to-end check. It recommends from the committed recording, writes the patches, renders them as a Kustomize overlay over the live Deployments (so a patch that doesn't merge fails before anything is applied), port-forwards Prometheus and runs `rightsize validate`.

If the cluster doesn't exist it runs `up.sh --squeeze` first and deletes the cluster again on exit. A cluster that was already there is left running, patched.

- `WINDOW`: length of the baseline and replay windows (default `5m`).
- `RECORDING` (default `recordings/boutique-30m.json`), `OUT` (default `out/validate.json`), `PORT` for the port-forward (default 29091), `CLUSTER`, `NODE_MEMORY` (default `3g` here).
- `RECORD=10m`: record a fresh window from the cluster (after `WARMUP` seconds, default 120) and recommend from that instead of `RECORDING`. The recording is saved as `out/recording.json`. This is what CI does.
- `KEEP_CLUSTER=1`: keep a cluster the script created.

On a fresh machine it takes about 20 minutes: around 6 for the cluster, two 5-minute windows and the rollouts in between.

## dockerd.sh

`eval "$(scripts/dockerd.sh start)"` starts a private Docker daemon with its data under `$TMPDIR` and exports `DOCKER_HOST`; `scripts/dockerd.sh stop` stops it. It needs sudo. `validate.sh` uses it on its own when `docker info` fails, or when building the cluster on the reachable daemon fails (as it does with runc on my VM). It runs without the default bridge and with its own address pool so it can sit next to a system Docker, and uses `crun` if it's installed (see below).

## down.sh

Deletes the cluster.

## Host workarounds

My machine is a sandboxed VM without `/dev/kmsg` and with a read-only `/proc/sys`, and the kubelet refuses to start there. `scripts/kind-config.yaml` mounts `/dev/null` over `/dev/kmsg` and turns on the `KubeletInUserNamespace` feature gate so it can skip those steps. OOM kills are still reported through container status. On a normal Linux host both settings are harmless.

I also had to run Docker with `crun` instead of `runc`, because runc failed on a sysctl permission check on that host. `scripts/dockerd.sh` does that for its private daemon.
