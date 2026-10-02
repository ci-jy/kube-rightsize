# Validation runs on Online Boutique

`scripts/validate.sh` recommends from `recordings/boutique-30m.json`, writes the patches, runs the load generator for one window on the cluster as it is, applies all 12 patches, waits for the rollouts and runs a second window of the same length. See [cli.md](cli.md) for the flags and [cluster-setup.md](cluster-setup.md) for the script's settings.

## What a patch looks like

`rightsize patch` prints a diff of each container's `resources` block. This is the one for the squeezed service:

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

If a kept CPU limit would end up below the new request it's raised to match, since the API server rejects that.

## Gates

- `rollout`: every patched Deployment became available.
- `oom`: nothing was OOM-killed during the replay. OOM kills come from the pods' restart counts and last termination reason.
- `throttling`: no container went above 25% throttled CFS periods unless it was already above that in the baseline. Measured with a Prometheus `increase()` over the window, and only for containers that have a CPU limit.
- `errors`: Locust's failure rate didn't rise by more than one percentage point, and the replay sent at least 50 requests. The load generator is restarted at the start of each window so its totals cover just that window.

The gate logic is a plain function over two window summaries, tested in `tests/test_validate_gates.py`.

## The recommended patches

From [results/boutique-validation.json](../results/boutique-validation.json), 5-minute windows:

| | before patching | after patching |
|---|---|---|
| requests sent by the load generator | 679 | 638 |
| failed requests | 0 | 0 |
| OOM kills | 4 (recommendationservice) | 0 |
| worst throttled share of CFS periods | 3.0% (currencyservice) | 3.2% (currencyservice) |

All four gates passed, with 140m of CPU and 692Mi of memory requested instead of 1570m and 1180Mi.

The baseline had no failed requests even though recommendationservice was crash-looping. The frontend seems to render the page without recommendations when that call fails, so the error gate alone wouldn't have caught it.

## A deliberately bad patch

To check the gates aren't decorative, I gave adservice (a JVM using about 144Mi) an 80Mi limit. From [results/boutique-validation-undersized.json](../results/boutique-validation-undersized.json), 3-minute windows: the new adservice pod was OOM-killed and crash-looped, so `rollout` and `oom` failed and the command exited 1.

The error gate passed there too, because Kubernetes kept the old adservice pod serving while the new one crashed.

## Limits of this check

Throttling barely shows up because the Online Boutique CPU limits are generous and stay in place. With `--cpu-limits drop` the throttling gate has nothing to measure at all. The error gate trusts Locust's totals, so a service that fails quietly needs the OOM gate or a real frontend metric.
