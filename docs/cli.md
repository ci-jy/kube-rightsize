# Command line and file formats

Run as `python3 -m rightsize <command>`.

## Shared options

Used by `record`, and by `recommend` when it queries Prometheus directly.

- `--prometheus URL`: Prometheus base URL (default `http://127.0.0.1:29090`).
- `--namespace NS`: namespace to read. Required for `record`; for `recommend`, either this or `--from-recording` is required.
- `--duration D`: how far back to read, e.g. `30m`, `2h`, `900s` (default `30m`).
- `--step S`: query resolution in seconds (default 15).

## record

- `--end T`: unix time the window ends (default now).
- `--out PATH`: recording file to write, `-` for stdout. Required.

## recommend

- `--from-recording PATH`: read a recording instead of querying Prometheus.
- `--out PATH`: recommendations file, `-` for stdout (the default). Writing to stdout turns the table off.
- `--cpu-percentile` (default 95), `--memory-percentile` (default 99), `--memory-headroom` (default 0.10).
- `-q`, `--quiet`: skip the summary table.

## patch

Reads a recommendations file and writes one strategic-merge patch per workload, touching only the `resources` of containers whose numbers change.

- `--recommendations PATH`: output of `recommend` (default `out/recommendations.json`).
- `--out DIR`: where to write the patches (default `out/patches`).
- `--base DIR`: a kustomization directory to list under `resources:`, so the output is a complete overlay. Without it the kustomization only lists the patches.
- `--cpu-limits keep|drop`: the recommender suggests no CPU limit, but by default existing CPU limits are kept. A kept limit below the new request is raised to the request. `drop` removes them (`cpu: null` in the patch).
- `--min-confidence none|low|medium|high`: leave out recommendations below this confidence (default `low`, so everything with data).
- `-q`: don't print the diff.

The directory gets `<kind>-<name>.yaml` patches, `kustomization.yaml`, `diff.txt` (a unified diff of each container's resources block) and `patches.json`, an index that `validate` reads.

## validate

Applies the patches to a live cluster and compares two load windows of the same length: a baseline on the cluster as it is, then a replay after the patched workloads have rolled out. Each window starts by restarting the load generator, so Locust's request and failure counts cover just that window.

- `--patches DIR` (default `out/patches`), `--namespace NS`, `--context CTX`, `--prometheus URL`.
- `--window D`: length of each window (default `5m`).
- `--loadgenerator NAME`: the Locust deployment (default `loadgenerator`).
- `--max-throttle F`: fail if a container's share of throttled CFS periods goes above this and wasn't above it in the baseline (default 0.25).
- `--max-error-increase F`: fail if the failure rate rises by more than this, absolute (default 0.01, one percentage point).
- `--min-requests N`: fewer requests than this in the replay is a failure, because the error rate means nothing (default 50).
- `--recommendations PATH`: the recommendations the patches were made from (default `out/recommendations.json`). If the file exists it is copied into the results, so `report` needs nothing else.
- `--out PATH`: results file (default `out/validate.json`).

It exits 0 when every gate passes, 1 when one fails and 2 when kubectl or Prometheus can't be reached. The gates are `rollout` (every patched workload became available), `oom`, `throttling` and `errors`.

## report

Writes a single static HTML page (inline CSS, no JavaScript, nothing time-dependent) from a validation run: requested, used and recommended CPU and memory per container, the total capacity freed, the gates, and the two load windows side by side.

- `--run PATH`: results from `validate` (default `out/validate.json`).
- `--recommendations PATH`: only needed for results that don't carry their own copy, such as the 0.2.0 runs in `results/`.
- `--out PATH`: the page (default `docs/index.html`).

"Used" means CPU P95 and memory P99 from the recording the recommendations came from. The "in replay" columns are the busiest replica's average CPU and peak working set during the patched replay window.

## Recording format (`rightsize-recording/1`)

Top level: `namespace`, `start`, `end`, `step_seconds`, `rate_window`, `prometheus` and `containers`. Each container entry has `workload`, `container` and a list of `pods`. Each pod holds raw `[timestamp, value]` series for `cpu`, `memory`, `throttled`, `restarts`, `oom_last` and `oom_events`, plus the last seen `requests` and `limits`.

The queries behind those series:

- `cpu`: `rate(container_cpu_usage_seconds_total[1m])`
- `memory`: `container_memory_working_set_bytes`
- `throttled`: throttled CFS periods divided by all CFS periods
- `restarts`: `kube_pod_container_status_restarts_total`
- `oom_last`: `kube_pod_container_status_last_terminated_reason{reason="OOMKilled"}`
- `oom_events`: `container_oom_events_total`
- `requests`, `limits`: `kube_pod_container_resource_requests` and `_limits`

`kube_pod_container_info` is also queried, only to tell regular containers from init containers.

## Recommendations format (`rightsize-recommendations/1`)

Top level: `source`, `policy` (the settings used), `summary` and `recommendations`.

`summary` has container counts, how many were censored, and requested vs recommended CPU (millicores) and memory (MiB) with the freed percentage for each.

Each recommendation has `workload`, `kind` (guessed from the pod names), `container`, `replicas`, `current`, `recommended`, `observed`, `flags`, `confidence` and `reasons`. `observed` holds coverage in seconds, sample counts, CPU P95, memory P99 and max, P95 throttling, restarts and OOM kills.

## Validation format (`rightsize-validation/1`)

Top level: `namespace`, `window_seconds`, `settings` (the gate thresholds), `patched` (the new resources per workload), `passed`, `gates`, and the two windows `baseline` and `replay`. Each window has its `start` and `end`, Locust's `requests` and `failures`, `error_rate`, `restarts` (restarts and OOM kills per workload/container during the window), `throttling` (throttled share of CFS periods per workload/container, only for containers with a CPU limit) and `usage` (the busiest replica's average CPU in millicores and peak working set in MiB over the window). Runs from 0.3.0 on also hold a copy of the `recommendations`.
