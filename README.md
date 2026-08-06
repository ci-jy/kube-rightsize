# kube-rightsize

`rightsize` is a small Python CLI that reads real container usage from Prometheus and recommends Kubernetes CPU and memory requests (plus a memory limit) for each container, with a written reason for every number. Most requests get copied from the service next door and then doubled after the first OOM kill. I wanted them based on measurements instead, without being fooled by containers that were already starving.

## Running it

You need Python 3.10+. The package has no runtime dependencies; pytest and Hypothesis are only for the tests.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'

# tests run offline against the committed recording
python3 -m pytest

# table on stdout, full JSON with reasons in the file
python3 -m rightsize recommend --from-recording recordings/boutique-30m.json --out out/recommendations.json
```

`recordings/boutique-30m.json` is 30 minutes of Google's Online Boutique demo running on a local kind cluster, with its load generator switched on. To make your own recording you also need Docker, kind, kubectl and Helm 3:

```sh
scripts/up.sh --squeeze                                            # kind + Prometheus + Online Boutique
WARMUP=120 DURATION=30m scripts/record.sh recordings/my-run.json   # the settings the committed recording used
scripts/down.sh                                                    # deletes the cluster
```

You can also skip the recording and point it at any Prometheus you can reach:

```sh
kubectl -n monitoring port-forward svc/prometheus-server 29090:80 &
python3 -m rightsize recommend --namespace boutique --duration 10m --out live.json
```

Flags and the JSON formats are in [docs/cli.md](docs/cli.md). Cluster setup, script settings and the workarounds my host needed are in [docs/cluster-setup.md](docs/cluster-setup.md).

## What it found on Online Boutique

Across the 12 containers, requested CPU goes from 1570m to 140m and requested memory from 1180Mi to 692Mi. Most of the memory comes from redis-cart and the load generator, which reserve 200Mi and 256Mi and use about 6Mi and 47Mi. The per-container numbers are in [docs/boutique-results.md](docs/boutique-results.md).

The row I actually care about is `recommendationservice`. `up.sh --squeeze` gives it a 40Mi memory limit, just under what it uses, so the recording contains real OOM kills. Its P99 of 41.2Mi is really just the limit plus noise, and a naive P99 + 10% would have set it to 48Mi and kept it crash-looping. Instead it gets flagged and raised:

```json
"reasons": [
  "Memory P99 is 41.2Mi over 132 samples; +10% headroom gives 45.3Mi.",
  "Memory is censored: OOM-killed 2 time(s), so the real peak is unknown. Raised to 140Mi (old limit +20% or +100Mi, whichever is larger) instead of trusting the percentile.",
  "CPU P95 is 8.9m over 108 samples.",
  "CPU raised to the 10m floor."
]
```

## How it works

`rightsize record` runs range queries against the Prometheus HTTP API for one namespace (CPU rate, memory working set, CFS throttling, restarts, OOM-kill reasons, current requests and limits) and saves the raw per-pod series as JSON. Keeping the raw series means everything after that can be recomputed offline, which is also how the tests work. Init containers are dropped, and pods are grouped into workloads by stripping the hash off the pod name.

`rightsize recommend` pools all replicas of a container. The basic rule is simple: P95 of CPU, P99 of memory plus 10%, a small floor, a memory limit a bit above the request, and no CPU limit.

The hard part is censored data. If a container was OOM-killed or sat at its limit, the percentile you measure is the limit, not the demand, and sizing from it shrinks the service that is starving. Those containers are flagged and raised the way the Vertical Pod Autoscaler reacts to an OOM kill, and CPU pinned at its limit is treated the same way. The first version also mistook startup CPU bursts for throttling, so samples right after a container starts are now skipped.

Every recommendation gets a confidence level. A clean 30-minute recording tops out at `medium`, which seems right to me. The full rule set is in [docs/rules.md](docs/rules.md).

## Known issues and next steps

- Nothing has been replayed yet. The numbers are what the recommender says, not proof the services survive on them. Next I want `rightsize` to write manifest patches, apply them to the kind cluster, rerun the load and count OOM kills and throttling.
- 30 minutes of 10 simulated users is a light, flat load. Real services have daily peaks, batch jobs and deploys, so don't apply these numbers to production from half an hour of data.
- OOM detection relies on kube-state-metrics' `last_terminated_reason`. The cAdvisor fallback needs `/dev/kmsg`, which my VM didn't have, so it was always zero.
- Workloads are found by pod-name patterns, not owner references. A bare pod whose name happens to end in `-12345` would be grouped wrongly.

Project period: 2026-08-01 to 2026-08-06.
