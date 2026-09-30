# Changelog

## 0.2.0 - Manifest patches validated by replayed load

- `rightsize patch` turns recommendations into one Kustomize strategic-merge patch per workload, plus a unified diff of every container's resources block. Existing CPU limits are kept unless `--cpu-limits drop` is given, and a kept limit is raised if the new request would exceed it.
- `rightsize validate` measures a baseline load window, applies the patches, waits for the rollouts and replays the same window. It fails on any OOM kill, on CPU throttling that rises above a threshold, or on a frontend error rate more than one percentage point above the baseline.
- `scripts/validate.sh` runs the whole thing on the kind cluster, creating and deleting it if needed, and writes `out/validation.json`. The run on Online Boutique is committed as `results/boutique-validation.json`.
- `scripts/dockerd.sh` starts a private Docker daemon under `$TMPDIR` for hosts where the system one isn't usable.
- Recommendations now carry the workload kind, guessed from pod names.

## 0.1.0 - Recommendations from recorded Prometheus usage

- `rightsize record` and `rightsize recommend`, the kind cluster scripts, and a 30-minute Online Boutique recording with tests against it.
