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

Each recommendation has `workload`, `container`, `replicas`, `current`, `recommended`, `observed`, `flags`, `confidence` and `reasons`. `observed` holds coverage in seconds, sample counts, CPU P95, memory P99 and max, P95 throttling, restarts and OOM kills.
