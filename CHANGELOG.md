# Changelog

## 0.3.0 - CI workflow and before/after report

- `rightsize report` renders a validation run as one static HTML page: requested, used and recommended CPU and memory per container, total capacity freed, the gates and both load windows. `docs/index.html` is the report from the latest validated run.
- `rightsize validate` now records each window's average CPU and peak memory per container, keeps a copy of the recommendations in the results, and writes `out/validate.json` by default.
- `scripts/validate.sh` can record a fresh window from the cluster first (`RECORD=10m`) instead of using the committed recording.
- `.github/workflows/rightsize.yml` brings up kind, records, recommends, patches and validates on pull requests that change the cluster scripts, the sizing code or the recordings, and uploads the report. `ci.yml` runs the tests, actionlint and the Docker image build.
- A `Dockerfile` for the CLI.

## 0.2.0 - Manifest patches validated by replayed load

- `rightsize patch` turns recommendations into one Kustomize strategic-merge patch per workload, plus a unified diff of every container's resources block. Existing CPU limits are kept unless `--cpu-limits drop` is given, and a kept limit is raised if the new request would exceed it.
- `rightsize validate` measures a baseline load window, applies the patches, waits for the rollouts and replays the same window. It fails on any OOM kill, on CPU throttling that rises above a threshold, or on a frontend error rate more than one percentage point above the baseline.
- `scripts/validate.sh` runs the whole thing on the kind cluster, creating and deleting it if needed, and writes `out/validation.json`. The run on Online Boutique is committed as `results/boutique-validation.json`.
- `scripts/dockerd.sh` starts a private Docker daemon under `$TMPDIR` for hosts where the system one isn't usable.
- Recommendations now carry the workload kind, guessed from pod names.

## 0.1.0 - Recommendations from recorded Prometheus usage

- `rightsize record` and `rightsize recommend`, the kind cluster scripts, and a 30-minute Online Boutique recording with tests against it.
