# kube-rightsize

`rightsize` is a small Python CLI for platform engineers and developers on shared Kubernetes clusters. It reads real container usage from Prometheus and suggests CPU and memory requests, plus a memory limit, for each container. Every number comes with a written reason. Requests usually get copied from the service next door and then doubled after the first OOM kill, and I wanted them based on measurements instead, without being fooled by containers that were already starving.

A recommendation is only a claim, though, and nobody should merge a 90% cut in CPU requests on a claim. So `rightsize` also writes the numbers out as small Kustomize patches with a diff you can review, applies them to a kind cluster, replays the same load and fails if anything gets OOM-killed, throttled or starts returning errors.

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

`recordings/boutique-30m.json` is 30 minutes of Google's Online Boutique demo on a local kind cluster, with its load generator running. To make your own recording you also need Docker, kind, kubectl and Helm 3:

```sh
scripts/up.sh --squeeze                                            # kind + Prometheus + Online Boutique
WARMUP=120 DURATION=30m scripts/record.sh recordings/my-run.json   # the settings the committed recording used
scripts/down.sh                                                    # deletes the cluster
```

To check the patches under load, run the end-to-end script. It needs the same tools and about 20 minutes. It creates the squeezed cluster if there isn't one, applies the patches, runs two 5-minute load windows and deletes the cluster again:

```sh
scripts/validate.sh            # writes out/validation.json, exits 1 if a gate fails
```

If Docker isn't usable as your user, the script starts a private daemon under `$TMPDIR` through `scripts/dockerd.sh` (needs sudo). That's how I ran it.

Or skip the recording and point it at any Prometheus you can reach:

```sh
kubectl -n monitoring port-forward svc/prometheus-server 29090:80 &
python3 -m rightsize recommend --namespace boutique --duration 10m --out live.json
```

Flags and file formats are in [docs/cli.md](docs/cli.md).
Cluster setup and the host workarounds I needed are in [docs/cluster-setup.md](docs/cluster-setup.md).

## What it found on Online Boutique

Across the 12 containers, requested CPU drops from 1570m to 140m and requested memory from 1180Mi to 692Mi. A lot of the memory is redis-cart and the load generator, which reserve 200Mi and 256Mi but use about 6Mi and 47Mi. The per-container numbers are in [docs/boutique-results.md](docs/boutique-results.md).

The row I actually care about is `recommendationservice`. `up.sh --squeeze` gives it a 40Mi memory limit, a bit under what it needs, so the recording has real OOM kills in it. Its P99 of 41.2Mi is just the limit plus noise. A plain "P99 plus 10%" rule would have set it to 48Mi and kept it crash-looping. Instead it gets flagged and raised:

```json
"reasons": [
  "Memory P99 is 41.2Mi over 132 samples; +10% headroom gives 45.3Mi.",
  "Memory is censored: OOM-killed 2 time(s), so the real peak is unknown. Raised to 140Mi (old limit +20% or +100Mi, whichever is larger) instead of trusting the percentile.",
  "CPU P95 is 8.9m over 108 samples.",
  "CPU raised to the 10m floor."
]
```

## Do the services survive on it?

`rightsize patch` turns each recommendation into a strategic-merge patch that only touches the container's `resources`, and prints a diff like this one for the squeezed service:

```diff
--- a/deployment/recommendationservice container server
+++ b/deployment/recommendationservice container server
@@ -1,7 +1,7 @@
 resources:
   requests:
-    cpu: 100m
-    memory: 32Mi
+    cpu: 10m
+    memory: 140Mi
   limits:
     cpu: 200m
-    memory: 40Mi
+    memory: 176Mi
# confidence low; memory-censored
```

`scripts/validate.sh` then runs the load generator for 5 minutes on the cluster as it is, applies all 12 patches, waits for the rollouts and runs another 5 minutes. From [results/boutique-validation.json](results/boutique-validation.json):

| | before patching | after patching |
|---|---|---|
| requests sent by the load generator | 679 | 638 |
| failed requests | 0 | 0 |
| OOM kills | 4 (recommendationservice) | 0 |
| worst throttled share of CFS periods | 3.0% (currencyservice) | 3.2% (currencyservice) |

All four gates passed: every patched Deployment rolled out, nothing was OOM-killed, no container went above 25% throttling, and the error rate didn't rise. That's with 140m of CPU and 692Mi of memory requested instead of 1570m and 1180Mi. I was surprised the baseline had no failed requests while recommendationservice was crash-looping. The frontend seems to just render the page without recommendations when that call fails, so the error gate alone wouldn't have caught it. The OOM gate did.

To make sure the gates aren't decorative, I also fed it a deliberately bad patch that gives adservice (a JVM using about 144Mi) an 80Mi limit. It failed, as it should: the new adservice pod was OOM-killed and crash-looped, so the `rollout` and `oom` gates failed and the command exited 1 ([results/boutique-validation-undersized.json](results/boutique-validation-undersized.json), 3-minute windows). The error gate passed there too, because Kubernetes kept the old adservice pod serving while the new one crashed.

## How it works

There are two steps. `rightsize record` runs range queries against the Prometheus HTTP API for one namespace (CPU rate, memory working set, CFS throttling, restarts, OOM-kill reasons, current requests and limits) and saves the raw per-pod series as JSON. Keeping the raw series means everything after that can be recomputed offline, which is also how the tests work. Init containers are dropped, and pods are grouped into workloads by stripping the hash off the pod name.

`rightsize recommend` pools all replicas of a container and applies a fairly plain rule: P95 of CPU, P99 of memory plus some headroom, a small floor, a memory limit a bit above the request, and no CPU limit.

The hard part was censored data. If a container was OOM-killed or sat at its limit, the percentile you measure is the limit, not the demand, and sizing from it shrinks exactly the service that is starving. Those containers get flagged and raised the way the Vertical Pod Autoscaler reacts to an OOM kill. CPU pinned at its limit is handled the same way. My first version also mistook startup CPU bursts for throttling, so samples right after a container starts are now skipped.

Each recommendation also gets a confidence level. A clean 30-minute recording tops out at `medium`, which feels right. The full rules are in [docs/rules.md](docs/rules.md).

`rightsize patch` writes one patch per workload, a `kustomization.yaml` and `diff.txt`. The recommender wants no CPU limits, but removing a limit is a different decision from resizing a request, so existing CPU limits are kept unless you pass `--cpu-limits drop`. If a kept limit would end up below the new request it's raised to match, since the API server rejects that.

`rightsize validate` restarts the load generator at the start of each window, so Locust's request and failure totals cover exactly that window. OOM kills come from the pods' restart counts and last termination reason, and throttling from a Prometheus `increase()` over the window. The gate logic is a plain function over two window summaries, which is what `tests/test_validate_gates.py` tests.

## Known issues and next steps

- A 5-minute replay at the same light load is a smoke test, not proof. Memory that creeps up over hours, or a spike when a cache fills, won't show up. Longer windows and a higher `USERS` count for the replay would be the next step.
- Throttling barely shows up because the Online Boutique CPU limits are generous and stay in place. With `--cpu-limits drop` the gate has nothing to measure at all.
- The patches are Kustomize only. Online Boutique also has a Helm chart, and writing a values file for it would be a natural addition.
- The error gate trusts Locust's totals. A service that fails quietly, like recommendationservice above, needs the OOM gate or a real frontend metric.
- Half an hour of 10 simulated users is a light, flat load. Real services have daily peaks, batch jobs and deploys, so don't take a recording like this to production.
- OOM detection relies on kube-state-metrics' `last_terminated_reason`. The cAdvisor fallback needs `/dev/kmsg`, which my VM didn't have, so it always read zero.
- Workloads are found by pod-name patterns, not owner references. A bare pod whose name happens to end in something like `-12345` gets grouped wrongly.

Project period: 2026-08-01 to 2026-08-06.
