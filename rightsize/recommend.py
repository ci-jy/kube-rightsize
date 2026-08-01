"""Turn recorded usage series into per-container request/limit recommendations.

The rules, per container (all replicas of a workload pooled):

* CPU request    = P95 of usage, floored, rounded up to ``cpu_step``.
* Memory request = P99 of working set + 10 %, floored, rounded up to ``memory_step``.
* Memory limit   = request x ``memory_limit_ratio`` but never below the
  observed peak + headroom.

Usage that hit a ceiling is *censored*: a container that was OOM-killed, or
whose working set sat at its memory limit, never showed its true peak, so its
percentile is a lower bound and must not be used to shrink it. The same goes
for CPU that was being throttled at its limit. Censored series are flagged and
the recommendation is raised the way the Vertical Pod Autoscaler reacts to an
OOM kill: to the larger of ``censored_bump`` x the old limit and the old limit
plus ``censored_min_bump`` (20 % / 100Mi by default).
"""

import math
from collections import defaultdict
from dataclasses import asdict, dataclass

from .stats import percentile
from .units import MI, format_cpu, format_memory, millicores, round_up


@dataclass
class Policy:
    cpu_percentile: float = 95.0
    memory_percentile: float = 99.0
    memory_headroom: float = 0.10
    cpu_floor: float = 0.010  # cores
    memory_floor: float = 32 * MI  # bytes
    cpu_step: float = 0.005
    memory_step: float = 4 * MI
    memory_limit_ratio: float = 1.25
    cpu_limit_ratio: float = 0.0  # 0 = recommend no CPU limit
    censored_bump: float = 1.2
    censored_min_bump: float = 100 * MI  # bytes, memory only
    memory_capped_ratio: float = 0.95  # working set >= 95 % of limit counts as capped
    cpu_capped_ratio: float = 0.90
    throttle_threshold: float = 0.20  # P95 share of throttled CFS periods
    replica_spread: float = 1.5  # max/min per-replica percentile before we size for the hungriest
    warmup_seconds: float = 60.0  # CPU samples ignored after a container (re)start
    min_history_seconds: float = 10 * 60  # below this we never shrink requests
    full_history_seconds: float = 24 * 3600  # needed for "high" confidence
    min_samples: int = 20


def _increase(points):
    """Total increase of a counter, ignoring resets."""
    total = 0.0
    for (_, a), (_, b) in zip(points, points[1:]):
        if b > a:
            total += b - a
    return total


def _restart_times(points):
    return [t for (_, a), (t, b) in zip(points, points[1:]) if b > a]


def analyse_pod(pod, policy, step):
    """Per-pod facts: usable samples, restarts and whether each restart was an OOM kill."""
    restarts = pod.get("restarts", [])
    restart_at = _restart_times(restarts)
    oom_flag = {t: v for t, v in pod.get("oom_last", [])}
    oom_kills = 0
    for t in restart_at:
        # kube-state-metrics flips last_terminated_reason within a scrape or two of the restart.
        if any(oom_flag.get(t + k * step, 0) >= 1 for k in range(-1, 3)):
            oom_kills += 1
    oom_kills = max(oom_kills, int(_increase(pod.get("oom_events", []))))

    cpu = pod.get("cpu", [])
    starts = list(restart_at)
    if cpu:
        starts.append(cpu[0][0])  # a pod that appears mid-recording also just started
    # A rate() window straddling a restart is meaningless; startup CPU bursts are
    # absorbed by the absent CPU limit, so they should not drive the request either.
    def settled(points):
        kept = [v for t, v in points if not any(0 <= t - s < policy.warmup_seconds for s in starts)]
        return kept or [v for _, v in points]

    cpu_vals = settled(cpu)

    mem_vals = [v for _, v in pod.get("memory", [])]
    throttled = settled(pod.get("throttled", []))
    times = [t for t, _ in pod.get("memory", [])] + [t for t, _ in cpu]
    return {
        "pod": pod["pod"],
        "cpu": cpu_vals,
        "memory": mem_vals,
        "throttled": throttled,
        "restarts": int(_increase(restarts)),
        "oom_kills": oom_kills,
        "first": min(times) if times else None,
        "last": max(times) if times else None,
        "requests": pod.get("requests", {}),
        "limits": pod.get("limits", {}),
        "timestamps": {t for t, _ in pod.get("memory", [])},
    }


def _typical_replicas(pods):
    """Median number of pods alive at once, so a rollout's brief overlap is not counted as scale."""
    counts = defaultdict(int)
    for p in pods:
        for t in p["timestamps"]:
            counts[t] += 1
    if not counts:
        return len(pods)
    return int(math.ceil(percentile(list(counts.values()), 50)))


def _current(pods, kind, resource):
    """Setting on the most recently seen pod; replaced pods may carry an older spec."""
    seen = [p for p in pods if resource in p[kind]]
    if not seen:
        return None
    newest = max(seen, key=lambda p: (p["last"] or 0, p["first"] or 0))
    return newest[kind][resource]


def _spread(pods, key, q, policy):
    per = [percentile(p[key], q) for p in pods if len(p[key]) >= policy.min_samples]
    if len(per) < 2 or min(per) <= 0:
        return None, per
    return max(per) / min(per), per


def recommend_container(entry, policy=None, step=15.0):
    policy = policy or Policy()
    pods = [analyse_pod(p, policy, step) for p in entry.get("pods", [])]
    cur = {
        "requests": {"cpu": _current(pods, "requests", "cpu"), "memory": _current(pods, "requests", "memory")},
        "limits": {"cpu": _current(pods, "limits", "cpu"), "memory": _current(pods, "limits", "memory")},
    }
    result = {
        "namespace": entry.get("namespace"),
        "workload": entry["workload"],
        "container": entry["container"],
        "replicas": _typical_replicas(pods) if pods else 0,
        "current": _quantities(cur),
        "flags": [],
        "reasons": [],
    }
    cpu = [v for p in pods for v in p["cpu"]]
    mem = [v for p in pods for v in p["memory"]]
    if not cpu or not mem:
        result.update(
            recommended=_quantities(cur), confidence="none", observed=None,
            _raw={"current": cur, "recommended": cur},
        )
        result["flags"].append("no-data")
        result["reasons"].append("No usage samples were recorded; keeping the current settings.")
        return result

    first = min(p["first"] for p in pods if p["first"] is not None)
    last = max(p["last"] for p in pods if p["last"] is not None)
    coverage = last - first + step
    restarts = sum(p["restarts"] for p in pods)
    oom_kills = sum(p["oom_kills"] for p in pods)
    flags, reasons = result["flags"], result["reasons"]

    # ---- memory -----------------------------------------------------------
    mem_p = percentile(mem, policy.memory_percentile)
    mem_max = max(mem)
    mem_req = mem_p * (1 + policy.memory_headroom)
    reasons.append(
        f"Memory P{policy.memory_percentile:g} is {mem_p / MI:.1f}Mi over {len(mem)} samples; "
        f"+{policy.memory_headroom:.0%} headroom gives {mem_req / MI:.1f}Mi."
    )
    mem_limit_now = cur["limits"]["memory"]
    mem_capped = bool(mem_limit_now) and mem_max >= policy.memory_capped_ratio * mem_limit_now
    if oom_kills or mem_capped:
        flags.append("memory-censored")
        base = mem_limit_now or mem_max
        floor = max(base * policy.censored_bump, base + policy.censored_min_bump)
        why = (
            f"OOM-killed {oom_kills} time(s)" if oom_kills
            else f"working set peaked at {mem_max / MI:.1f}Mi, at its {mem_limit_now / MI:.0f}Mi limit"
        )
        reasons.append(
            f"Memory is censored: {why}, so the real peak is unknown. "
            f"Raised to {floor / MI:.0f}Mi (old limit +{policy.censored_bump - 1:.0%} or "
            f"+{policy.censored_min_bump / MI:.0f}Mi, whichever is larger) instead of trusting the percentile."
        )
        mem_req = max(mem_req, floor)

    ratio, per = _spread(pods, "memory", policy.memory_percentile, policy)
    if ratio and ratio > policy.replica_spread:
        flags.append("replicas-uneven")
        hungriest = max(per) * (1 + policy.memory_headroom)
        reasons.append(
            f"Replica memory differs {ratio:.1f}x between pods; sized for the hungriest ({hungriest / MI:.1f}Mi)."
        )
        mem_req = max(mem_req, hungriest)

    if mem_req < policy.memory_floor:
        reasons.append(f"Memory raised to the {policy.memory_floor / MI:.0f}Mi floor.")
        mem_req = policy.memory_floor

    # ---- cpu --------------------------------------------------------------
    cpu_p = percentile(cpu, policy.cpu_percentile)
    cpu_req = cpu_p
    reasons.append(f"CPU P{policy.cpu_percentile:g} is {cpu_p * 1000:.1f}m over {len(cpu)} samples.")
    cpu_limit_now = cur["limits"]["cpu"]
    throttled = [v for p in pods for v in p["throttled"]]
    throttle_p = percentile(throttled, 95) if throttled else 0.0
    cpu_capped = bool(cpu_limit_now) and cpu_p >= policy.cpu_capped_ratio * cpu_limit_now
    if throttle_p > policy.throttle_threshold or cpu_capped:
        flags.append("cpu-censored")
        floor = cpu_limit_now * policy.censored_bump if cpu_limit_now else cpu_p
        reasons.append(
            f"CPU is censored: throttled in {throttle_p:.0%} of CFS periods at P95"
            + (f" against a {cpu_limit_now * 1000:.0f}m limit" if cpu_limit_now else "")
            + f", so demand is higher than measured. Raised to at least {floor * 1000:.0f}m."
        )
        cpu_req = max(cpu_req, floor)

    ratio, per = _spread(pods, "cpu", policy.cpu_percentile, policy)
    if ratio and ratio > policy.replica_spread:
        if "replicas-uneven" not in flags:
            flags.append("replicas-uneven")
        reasons.append(f"Replica CPU differs {ratio:.1f}x between pods; sized for the busiest ({max(per) * 1000:.1f}m).")
        cpu_req = max(cpu_req, max(per))

    if cpu_req < policy.cpu_floor:
        reasons.append(f"CPU raised to the {policy.cpu_floor * 1000:.0f}m floor.")
        cpu_req = policy.cpu_floor

    # ---- history and restarts ---------------------------------------------
    if coverage < policy.min_history_seconds:
        flags.append("short-history")
        reasons.append(
            f"Only {coverage / 60:.0f} min of data (< {policy.min_history_seconds / 60:.0f} min); "
            "requests are not lowered below their current values."
        )
        if cur["requests"]["cpu"]:
            cpu_req = max(cpu_req, cur["requests"]["cpu"])
        if cur["requests"]["memory"]:
            mem_req = max(mem_req, cur["requests"]["memory"])
    if restarts and not oom_kills:
        flags.append("restarted")
        reasons.append(
            f"{restarts} restart(s) not caused by OOM; samples within {policy.warmup_seconds:.0f}s "
            "of each start were left out of the CPU percentile."
        )
    elif restarts > oom_kills:
        flags.append("restarted")

    cpu_req = round_up(cpu_req, policy.cpu_step)
    mem_req = round_up(mem_req, policy.memory_step)
    mem_lim = max(mem_req * policy.memory_limit_ratio, mem_max * (1 + policy.memory_headroom))
    if "memory-censored" in flags and mem_limit_now:
        mem_lim = max(mem_lim, mem_req * policy.memory_limit_ratio)
    mem_lim = round_up(mem_lim, policy.memory_step)
    cpu_lim = round_up(cpu_req * policy.cpu_limit_ratio, policy.cpu_step) if policy.cpu_limit_ratio else None

    rec = {"requests": {"cpu": cpu_req, "memory": mem_req}, "limits": {"cpu": cpu_lim, "memory": mem_lim}}
    result["recommended"] = _quantities(rec)
    result["confidence"] = _confidence(flags, coverage, len(mem), policy)
    result["observed"] = {
        "coverage_seconds": round(coverage, 1),
        "samples": {"cpu": len(cpu), "memory": len(mem)},
        "cpu_p95_millicores": round(cpu_p * 1000, 2),
        "memory_p99_mib": round(mem_p / MI, 2),
        "memory_max_mib": round(mem_max / MI, 2),
        "throttled_p95": round(throttle_p, 3),
        "restarts": restarts,
        "oom_kills": oom_kills,
    }
    result["_raw"] = {"current": cur, "recommended": rec}
    return result


def _confidence(flags, coverage, n, policy):
    if "memory-censored" in flags or "cpu-censored" in flags or "short-history" in flags or n < policy.min_samples:
        return "low"
    if coverage < policy.full_history_seconds or "replicas-uneven" in flags or "restarted" in flags:
        return "medium"
    return "high"


def _quantities(spec):
    r, l = spec["requests"], spec["limits"]
    return {
        "requests": {"cpu": format_cpu(r["cpu"]), "memory": format_memory(r["memory"])},
        "limits": {"cpu": format_cpu(l["cpu"]), "memory": format_memory(l["memory"])},
    }


def summarise(results):
    """Cluster-wide requested capacity before and after, weighted by replica count."""
    totals = {"cpu": [0.0, 0.0], "memory": [0.0, 0.0]}
    counted = 0
    for r in results:
        cur, rec = r["_raw"]["current"]["requests"], r["_raw"]["recommended"]["requests"]
        if cur["cpu"] is None or cur["memory"] is None or "no-data" in r["flags"]:
            continue
        counted += 1
        n = max(r["replicas"], 1)
        totals["cpu"][0] += cur["cpu"] * n
        totals["cpu"][1] += rec["cpu"] * n
        totals["memory"][0] += cur["memory"] * n
        totals["memory"][1] += rec["memory"] * n

    def freed(pair):
        return round(100 * (pair[0] - pair[1]) / pair[0], 1) if pair[0] else 0.0

    return {
        "containers": len(results),
        "containers_compared": counted,
        "censored": sum(1 for r in results if {"memory-censored", "cpu-censored"} & set(r["flags"])),
        "cpu_requested_millicores": millicores(totals["cpu"][0]),
        "cpu_recommended_millicores": millicores(totals["cpu"][1]),
        "cpu_freed_percent": freed(totals["cpu"]),
        "memory_requested_mib": round(totals["memory"][0] / MI, 1),
        "memory_recommended_mib": round(totals["memory"][1] / MI, 1),
        "memory_freed_percent": freed(totals["memory"]),
    }


def recommend(recording, policy=None):
    policy = policy or Policy()
    step = float(recording.get("step_seconds", 15))
    results = [recommend_container(c, policy, step) for c in recording["containers"]]
    summary = summarise(results)
    for r in results:
        r.pop("_raw")
    return {
        "format": "rightsize-recommendations/1",
        "source": {
            "namespace": recording.get("namespace"),
            "start": recording.get("start"),
            "end": recording.get("end"),
            "step_seconds": step,
        },
        "policy": {k: v for k, v in asdict(policy).items()},
        "summary": summary,
        "recommendations": results,
    }
