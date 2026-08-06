# kube-rightsize

`rightsize` is a small Python CLI that reads real container usage from Prometheus and recommends Kubernetes CPU and memory requests (and memory limits) for each container, with a written reason for every number. It's meant for platform/DevOps engineers and developers who share a cluster and would like their requests based on measurements, not guesses.

## The problem

You deploy a service and have to fill in `resources.requests`. Nobody knows the right number, so you copy the one from the service next door. A week later the pod is OOM-killed during a spike, someone doubles the memory "to be safe", and that number stays forever. Repeat across a few dozen services and most of the cluster's reserved capacity sits idle while the scheduler reports it as full.

That pattern is well documented. SpendArk's [write-up on request sizing](https://spendark.com/blog/kubernetes-request-sizing/) cites survey data that 64% of teams add 2–4x headroom after a single OOM incident, and that roughly 70% of requested CPU and memory goes unused. Its advice is to size from observed percentiles instead: P95 for CPU, P99 + 10% for memory. That's simple to say, but it's still done by hand with PromQL. And it has a trap: if a container was OOM-killed, or was pinned at its limit, the percentile you measure is the *limit*, not the real demand, and sizing from it shrinks the very service that's starving.

## Approach

1. **A real cluster with real load.** `scripts/up.sh` builds a single-node [kind](https://kind.sigs.k8s.io/) cluster, installs the [Prometheus Helm chart](https://github.com/prometheus-community/helm-charts/tree/main/charts/prometheus) (server + kube-state-metrics), and deploys Google's [Online Boutique](https://github.com/GoogleCloudPlatform/microservices-demo) (eleven services, one of them a Locust load generator, plus Redis). With `--squeeze` it gives `recommendationservice` a 40Mi memory limit. That's just under what it actually uses, so the recording contains genuine OOM kills.
2. **`rightsize record`** pulls range queries from the Prometheus HTTP API for one namespace and stores the raw per-pod series in JSON:
   - CPU: `rate(container_cpu_usage_seconds_total)`
   - memory: working set
   - CFS throttling ratio
   - restart counters
   - `last_terminated_reason="OOMKilled"`
   - current requests/limits

   Init containers are dropped, and pods are grouped into workloads by stripping the ReplicaSet/pod hash.
3. **`rightsize recommend`** pools all replicas of a container and applies these rules:

   | Rule | Value |
   |---|---|
   | CPU request | P95 of usage, floor 10m, rounded up to 5m |
   | Memory request | P99 of working set + 10%, floor 32Mi, rounded up to 4Mi |
   | Memory limit | 1.25x request, never below observed peak + 10% |
   | CPU limit | none (the usual advice, since CPU limits mostly cause throttling) |
   | **Censored memory** (OOM kill, or working set ≥ 95% of limit) | the percentile isn't trusted; raised to max(limit × 1.2, limit + 100Mi), the same bump the Vertical Pod Autoscaler applies after an OOM |
   | **Censored CPU** (P95 throttling > 20%, or P95 ≥ 90% of limit) | raised to at least 1.2x the old limit |
   | Restarts | CPU and throttling samples in the 60 s after each start are skipped, so boot bursts don't look like steady demand |
   | Uneven replicas (per-pod percentile differs > 1.5x) | sized for the hungriest pod |
   | Short history (< 10 min) | requests are never lowered |

   Each recommendation gets a confidence. It is `low` if anything was censored or the history is short, `medium` for a clean but under-a-day history, and `high` only with a full day of clean data. In practice a 30-minute recording tops out at `medium`, which seems right to me.

The package has no runtime dependencies (just `urllib`, `json` and `argparse`); pytest and Hypothesis are only needed for the tests.

## What it found on Online Boutique

`recordings/boutique-30m.json` holds 30 minutes of the `boutique` namespace from the kind cluster, recorded after a 2-minute warm-up with the load generator running at its default 10 users. Running the recommender on it:

| Container | Requested CPU / mem | Observed P95 CPU / P99 mem | Recommended CPU / mem | Mem limit | Flags |
|---|---|---|---|---|---|
| adservice | 200m / 180Mi | 11.8m / 142.7Mi | 15m / 160Mi | 200Mi | - |
| cartservice | 200m / 64Mi | 8.8m / 50.0Mi | 10m / 56Mi | 72Mi | - |
| checkoutservice | 100m / 64Mi | 0.9m / 8.3Mi | 10m / 32Mi | 40Mi | - |
| currencyservice | 100m / 64Mi | 10.6m / 38.8Mi | 15m / 44Mi | 56Mi | - |
| emailservice | 100m / 64Mi | 1.7m / 43.4Mi | 10m / 48Mi | 60Mi | - |
| frontend | 100m / 64Mi | 17.5m / 12.1Mi | 20m / 32Mi | 40Mi | - |
| loadgenerator | 300m / 256Mi | 5.4m / 46.6Mi | 10m / 52Mi | 68Mi | - |
| paymentservice | 100m / 64Mi | 1.3m / 28.7Mi | 10m / 32Mi | 40Mi | - |
| productcatalogservice | 100m / 64Mi | 8.9m / 9.3Mi | 10m / 32Mi | 40Mi | - |
| recommendationservice | 100m / 32Mi | 8.9m / 41.2Mi | 10m / 140Mi | 176Mi | memory-censored |
| redis-cart | 70m / 200Mi | 2.8m / 6.1Mi | 10m / 32Mi | 40Mi | - |
| shippingservice | 100m / 64Mi | 1.2m / 8.4Mi | 10m / 32Mi | 40Mi | - |

Across the 12 containers, requested CPU goes from 1570m to 140m (91.1% freed) and requested memory from 1180Mi to 692Mi (41.4% freed). Most of the memory comes from redis-cart and the load generator, which reserve 200Mi and 256Mi and use 6Mi and 47Mi.

The interesting row is `recommendationservice`. It was OOM-killed twice inside the window, and its P99 (41.2Mi) is really just its 40Mi limit plus noise. A naive P99 + 10% would have set it to 48Mi and kept it crash-looping. Instead it's flagged `memory-censored` with `low` confidence and raised to 140Mi:

```json
"reasons": [
  "Memory P99 is 41.2Mi over 132 samples; +10% headroom gives 45.3Mi.",
  "Memory is censored: OOM-killed 2 time(s), so the real peak is unknown. Raised to 140Mi (old limit +20% or +100Mi, whichever is larger) instead of trusting the percentile.",
  "CPU P95 is 8.9m over 108 samples.",
  "CPU raised to the 10m floor."
]
```

(The memory total counts that service at its squeezed 32Mi request. The stock manifest asks for 220Mi, so against the unmodified manifest the saving would be larger.)

## Running it

You need Python 3.10+. For the cluster you also need Docker, [kind](https://kind.sigs.k8s.io/docs/user/quick-start/#installation), kubectl and Helm 3.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'

# tests run offline against the committed recording
python3 -m pytest

# recommendations from the recording: table on stdout, full JSON with reasons in the file
python3 -m rightsize recommend --from-recording recordings/boutique-30m.json --out out/recommendations.json
```

To make your own recording (the node container is capped at 4 GiB; it was using about 1.3 GiB when I checked during my run):

```sh
scripts/up.sh --squeeze                              # kind + Prometheus + Online Boutique, ~6 min
scripts/record.sh recordings/my-run.json             # port-forwards Prometheus, waits warm-up + 30m, records
WARMUP=120 DURATION=30m scripts/record.sh recordings/my-run.json   # the settings the committed recording used
scripts/down.sh                                      # deletes the cluster
```

Against any Prometheus you can reach, you can also skip the recording step:

```sh
kubectl -n monitoring port-forward svc/prometheus-server 29090:80 &
python3 -m rightsize recommend --namespace boutique --duration 10m --out live.json
```

The output JSON has a `summary` block (totals and freed percentages, weighted by replica count) and one entry per container. Each entry has `current`, `recommended`, `observed` (coverage, sample counts, P95/P99/max, throttling, restarts, OOM kills), `flags`, `confidence` and `reasons`.

A note on the host: my machine is a sandboxed VM without `/dev/kmsg` and with a read-only `/proc/sys`, and the kubelet refuses to start in that setup. `scripts/kind-config.yaml` mounts `/dev/null` over `/dev/kmsg` and turns on the `KubeletInUserNamespace` feature gate, which lets it skip those steps. On a normal Linux host both are harmless. I also had to run Docker with `crun` instead of `runc`, because runc failed on a sysctl permission check there.

## Tests

`python3 -m pytest` runs 39 tests in about two seconds:

- `test_stats.py`: the percentile function against hand-worked values and `statistics.quantiles`, plus Hypothesis properties (monotone in q, bounded by min/max).
- `test_recommend.py`: synthetic series for each rule: floors, OOM censoring, pinned-at-limit censoring, throttling, startup throttling *not* counting as a cap, non-OOM restarts, replica pooling and uneven replicas, rollouts, short history, no data. Property tests check that an OOM-killed container is never recommended below max(limit × 1.2, limit + 100Mi), that floors always hold, and that more usage never yields a smaller request.
- `test_boutique_recording.py`: the real recording. It checks that every service has 30 minutes of data, that `recommendationservice` is censored rather than shrunk, and that every recommendation covers its observed percentiles. It also replays the recorded series with twin replicas, a 3x-hungry replica and a history truncated to 5 minutes.
- `test_record.py`: workload naming, init-container filtering and NaN handling, using a fake Prometheus, plus the CLI end to end.

## Known issues and what's next

- **Nothing has been replayed yet.** The numbers above are what the recommender says, not proof that the services survive on them. The next step is to have `rightsize` write the recommendations as manifest patches, apply them to the kind cluster, rerun the load generator and count OOM kills and throttling. Until that exists, "freed" means "reservation the recorder says is unused".
- **30 minutes of 10 simulated users is a light, flat load.** Real services have daily peaks, batch jobs and deploys. The confidence flag never says `high` for this recording, and you shouldn't apply these numbers to production from half an hour of data.
- After that: a GitHub Actions job that runs the tests and the recommender on the recording and posts a before/after report.
- The OOM detection depends on kube-state-metrics' `last_terminated_reason` landing within a couple of scrapes of the restart. cAdvisor's `container_oom_events_total` is recorded as a fallback but needs `/dev/kmsg`, so on this host it was always zero.
- Workloads are found by pod-name patterns (Deployment, StatefulSet, DaemonSet), not owner references. A bare pod whose name happens to end in `-12345` would be mis-grouped.
- The memory floor (32Mi) and CPU floor (10m) are fixed defaults. Several services hit the floor, so for them the recommendation is really "as small as I'm willing to go".
