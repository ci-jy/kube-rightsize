# kube-rightsize

`rightsize` is a small Python CLI that reads real container usage from Prometheus and suggests CPU and memory requests, plus a memory limit, for every container in a namespace. Each number comes with a written reason. On shared clusters requests tend to get copied from the service next door and then doubled after the first OOM kill, and I wanted them based on measurements instead, without being fooled by containers that were already starving.

## Running it

You need Python 3.10 or newer. There are no runtime dependencies; pytest and Hypothesis are only for the tests.

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'

# tests run offline against the committed recording
python3 -m pytest

# table on stdout, full JSON with reasons in the file
python3 -m rightsize recommend --from-recording recordings/boutique-30m.json --out out/recommendations.json

# one patch per Deployment in out/patches/, plus diff.txt
python3 -m rightsize patch
```

`recordings/boutique-30m.json` is 30 minutes of Google's Online Boutique demo on a local kind cluster with its load generator running. To make your own recording, or to check the patches under load, you also need Docker, kind, kubectl and Helm 3:

```sh
scripts/up.sh --squeeze                                            # kind + Prometheus + Online Boutique
WARMUP=120 DURATION=30m scripts/record.sh recordings/my-run.json   # the settings the committed recording used
scripts/validate.sh                                                # applies the patches, replays load, exits 1 if a gate fails
scripts/down.sh                                                    # deletes the cluster
```

`validate.sh` takes about 20 minutes and builds the cluster itself if there isn't one. You can also skip recordings and point `recommend` at any Prometheus you can reach:

```sh
kubectl -n monitoring port-forward svc/prometheus-server 29090:80 &
python3 -m rightsize recommend --namespace boutique --duration 10m --out live.json
```

Flags and file formats are in [docs/cli.md](docs/cli.md).
Cluster setup and the host workarounds I needed are in [docs/cluster-setup.md](docs/cluster-setup.md).

## What it found on Online Boutique

Across the 12 containers, requested CPU drops from 1570m to 140m and requested memory from 1180Mi to 692Mi. A lot of the memory is redis-cart and the load generator, which reserve 200Mi and 256Mi but use a small fraction of that. Per-container numbers are in [docs/boutique-results.md](docs/boutique-results.md).

The row I actually care about is `recommendationservice`. `up.sh --squeeze` gives it a 40Mi memory limit, a bit under what it needs, so the recording has real OOM kills in it. Its P99 of 41.2Mi is just the limit plus noise. A plain "P99 plus 10%" rule would have kept it crash-looping. Instead it gets flagged and raised:

```json
"reasons": [
  "Memory P99 is 41.2Mi over 132 samples; +10% headroom gives 45.3Mi.",
  "Memory is censored: OOM-killed 2 time(s), so the real peak is unknown. Raised to 140Mi (old limit +20% or +100Mi, whichever is larger) instead of trusting the percentile.",
  "CPU P95 is 8.9m over 108 samples.",
  "CPU raised to the 10m floor."
]
```

With all 12 patches applied, a 5-minute replay had no failed requests and no OOM kills, where the unpatched baseline had 4 OOM kills in recommendationservice. What surprised me is that the baseline had no failed requests either. The frontend just renders the page without recommendations when that call fails, so only the OOM gate noticed. I also fed it a deliberately undersized patch to make sure the gates can fail, and they did. Both runs are written up in [docs/validation.md](docs/validation.md).

## How it works

`rightsize record` runs range queries against the Prometheus HTTP API for one namespace (CPU, memory working set, CFS throttling, restarts, OOM-kill reasons, current requests and limits) and saves the raw per-pod series as JSON. Everything after that can be recomputed offline, which is also how the tests work.

`rightsize recommend` pools all replicas of a container and applies a fairly plain rule: P95 of CPU, P99 of memory plus headroom, a small floor, a memory limit a bit above the request, and no CPU limit. The hard part was censored data. If a container was OOM-killed or sat at its limit, the percentile you measure is the limit, not the demand, and sizing from it shrinks exactly the service that is starving. Those get raised the way the Vertical Pod Autoscaler reacts to an OOM kill. My first version also mistook startup CPU bursts for throttling, so samples right after a container starts are now skipped. The full rules are in [docs/rules.md](docs/rules.md).

`rightsize patch` writes strategic-merge patches that only touch `resources`. Existing CPU limits are kept unless you pass `--cpu-limits drop`, because removing a limit is a different decision from resizing a request. `rightsize validate` then compares a baseline load window with a replay after the rollout and checks four gates: rollout, OOM kills, throttling and error rate.

## Known issues

- A 5-minute replay at light load is a smoke test, not proof. Memory that creeps up over hours won't show up, and half an hour of 10 simulated users is nothing like a real daily peak.
- OOM detection relies on kube-state-metrics' `last_terminated_reason`. The cAdvisor fallback needs `/dev/kmsg`, which my VM didn't have, so it always read zero.
- Workloads are found by stripping the hash off pod names, not by owner references, so a bare pod whose name happens to end in something like `-12345` gets grouped wrongly.
- Patches are Kustomize only. Online Boutique also ships a Helm chart, and writing a values file for it would be a natural next step.

Project period: 2026-08-01 to 2026-10-02.
