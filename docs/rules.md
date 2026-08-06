# Recommendation rules

All replicas of a container are pooled before any of this runs. Defaults live in `Policy` in `rightsize/recommend.py`; the three percentile/headroom settings can be changed from the command line (see [cli.md](cli.md)).

## Base rules

- CPU request: P95 of usage, floor 10m, rounded up to 5m.
- Memory request: P99 of working set + 10%, floor 32Mi, rounded up to 4Mi.
- Memory limit: 1.25x the request, never below the observed peak + 10%.
- CPU limit: none. CPU limits mostly cause throttling.

## Adjustments

- Censored memory: the container was OOM-killed, or its working set reached 95% of its limit. The percentile is not trusted; the request is raised to `max(limit * 1.2, limit + 100Mi)`, the same bump the Vertical Pod Autoscaler applies after an OOM kill. Flag: `memory-censored`.
- Censored CPU: P95 throttling above 20% of CFS periods, or P95 usage at 90% or more of the CPU limit. The request is raised to at least 1.2x the old limit. Flag: `cpu-censored`.
- Restarts: CPU and throttling samples in the 60 s after each container start (including a pod first appearing mid-recording) are skipped, so boot bursts don't look like steady demand or like being capped. Non-OOM restarts add the `restarted` flag.
- Uneven replicas: if the per-pod percentile differs by more than 1.5x between pods (pods with at least 20 samples), the container is sized for the hungriest pod. Flag: `replicas-uneven`.
- Short history: with less than 10 minutes of data, requests are never lowered below their current values. Flag: `short-history`.
- No data: current settings are kept. Flag: `no-data`.

## Confidence

- `low`: anything censored, short history, or fewer than 20 memory samples.
- `medium`: clean, but under a day of history, or uneven replicas, or restarts.
- `high`: a full day of clean data.
- `none`: no samples at all.

## Summary totals

The summary compares total requested CPU and memory before and after, weighted by the typical number of replicas (the median count of pods alive at once, so a rollout's overlap isn't counted as scale). Containers without a current request are left out of the totals.
