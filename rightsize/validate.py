"""Apply patches to a live cluster, replay load and check that nothing got worse.

A validation run measures two windows of the same length: a baseline on the
cluster as it is, then a replay after the patches are applied and rolled out.
Each window restarts the load generator, so its request and failure counters
cover exactly that window. Three gates decide the result:

* oom        - no container may be OOM-killed during the replay.
* throttling - no container's share of throttled CFS periods may go above
               ``max_throttle`` unless it was already above it in the baseline.
* errors     - the load generator's failure rate (all requests go through the
               frontend) may not rise by more than ``max_error_increase``.

The gate logic is pure (``evaluate``); everything that talks to kubectl or
Prometheus sits in ``Kubectl`` and ``measure`` so the gates can be tested on
plain dicts.
"""

import json
import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime

from .record import workload_of


@dataclass
class Gates:
    max_throttle: float = 0.25
    max_error_increase: float = 0.01  # absolute, 0.01 = one percentage point
    min_requests: int = 50


class KubectlError(RuntimeError):
    pass


class Kubectl:
    def __init__(self, namespace, context=None):
        self.namespace = namespace
        self.context = context

    def run(self, *args, timeout=600):
        cmd = ["kubectl"] + (["--context", self.context] if self.context else []) + ["-n", self.namespace, *args]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        if proc.returncode != 0:
            raise KubectlError(f"{' '.join(cmd)}: {proc.stderr.strip()}")
        return proc.stdout

    def pods(self):
        return json.loads(self.run("get", "pods", "-o", "json"))["items"]


# ---- reading cluster state ---------------------------------------------------

def _epoch(ts):
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp() if ts else None


def container_states(pods):
    """Restart count and last termination per (pod, container) from ``kubectl get pods -o json``."""
    states = {}
    for pod in pods:
        name = pod["metadata"]["name"]
        for cs in pod.get("status", {}).get("containerStatuses", []):
            last = (cs.get("lastState") or {}).get("terminated") or {}
            states[f"{name}/{cs['name']}"] = {
                "workload": workload_of(name),
                "container": cs["name"],
                "restarts": cs.get("restartCount", 0),
                "last_reason": last.get("reason"),
                "last_finished": _epoch(last.get("finishedAt")),
            }
    return states


def restarts_between(before, after, start=None):
    """Restarts and OOM kills per workload/container between two snapshots.

    A pod that only exists in ``after`` counts all its restarts. Only the most
    recent termination reason is visible, so restarts are counted as OOM kills
    when that reason is OOMKilled (or when an OOM finished after ``start``).
    """
    out = {}
    for key, s in after.items():
        delta = s["restarts"] - before.get(key, {}).get("restarts", 0)
        oom = 0
        if s["last_reason"] == "OOMKilled":
            if delta > 0:
                oom = delta
            elif start is not None and (s["last_finished"] or 0) >= start:
                oom = 1  # restart count not updated yet
        if delta <= 0 and not oom:
            continue
        w = out.setdefault(f"{s['workload']}/{s['container']}", {"restarts": 0, "oom_kills": 0})
        w["restarts"] += max(delta, 0)
        w["oom_kills"] += oom
    return out


_AGGREGATED = re.compile(r"^\s*Aggregated\s+(\d+)\s+(\d+)\(", re.M)


def parse_locust(log_text):
    """(requests, failures) from the last ``Aggregated`` row Locust printed, or None."""
    rows = _AGGREGATED.findall(log_text)
    if not rows:
        return None
    reqs, fails = rows[-1]
    return int(reqs), int(fails)


def throttle_query(namespace, window_s):
    sel = f'namespace="{namespace}",container!="",container!="POD"'
    w = f"{int(window_s)}s"
    by = "by (pod, container)"
    return (
        f"sum {by} (increase(container_cpu_cfs_throttled_periods_total{{{sel}}}[{w}]))"
        f" / sum {by} (increase(container_cpu_cfs_periods_total{{{sel}}}[{w}]))"
    )


def usage_queries(namespace, window_s):
    """Average CPU (cores) and peak working-set memory (bytes) per pod and container over a window."""
    sel = f'namespace="{namespace}",container!="",container!="POD"'
    w = f"{int(window_s)}s"
    by = "by (pod, container)"
    return {
        "cpu": f"sum {by} (rate(container_cpu_usage_seconds_total{{{sel}}}[{w}]))",
        "memory": f"max {by} (max_over_time(container_memory_working_set_bytes{{{sel}}}[{w}]))",
    }


def usage(cpu_result, memory_result):
    """Busiest replica's average CPU (millicores) and peak memory (MiB) per workload/container."""
    out = {}
    for field, result, scale in (("cpu_millicores", cpu_result, 1000.0), ("memory_mib", memory_result, 1 / 2**20)):
        for series in result:
            m = series["metric"]
            if "pod" not in m or "container" not in m:
                continue
            v = float(series["value"][1])
            if v != v:
                continue
            entry = out.setdefault(f"{workload_of(m['pod'])}/{m['container']}", {})
            entry[field] = round(max(entry.get(field, 0.0), v * scale), 2)
    return out


def throttling(result):
    """Highest throttled share per workload/container from an instant query result."""
    out = {}
    for series in result:
        m = series["metric"]
        if "pod" not in m or "container" not in m:
            continue
        v = float(series["value"][1])
        if v != v:  # NaN: no CFS periods at all, i.e. no CPU limit
            continue
        key = f"{workload_of(m['pod'])}/{m['container']}"
        out[key] = round(max(out.get(key, 0.0), v), 4)
    return out


# ---- gates -------------------------------------------------------------------

def error_rate(window):
    reqs = window.get("requests") or 0
    return window["failures"] / reqs if reqs else None


def evaluate(baseline, replay, gates=None):
    """Compare a replay window against the baseline; returns the gate results."""
    gates = gates or Gates()
    results = []

    ooms = {k: v["oom_kills"] for k, v in replay["restarts"].items() if v["oom_kills"]}
    results.append({
        "gate": "oom",
        "passed": not ooms,
        "detail": ("OOM-killed during replay: " + ", ".join(f"{k} x{n}" for k, n in sorted(ooms.items())))
        if ooms else "no container was OOM-killed during the replay",
        "offenders": sorted(ooms),
    })

    base_t = baseline.get("throttling", {})
    hot = {
        k: v for k, v in replay.get("throttling", {}).items()
        if v > gates.max_throttle and base_t.get(k, 0.0) <= gates.max_throttle
    }
    results.append({
        "gate": "throttling",
        "passed": not hot,
        "detail": ("throttled above {:.0%}: ".format(gates.max_throttle)
                   + ", ".join(f"{k} {v:.0%} (baseline {base_t.get(k, 0.0):.0%})" for k, v in sorted(hot.items())))
        if hot else f"no container rose above {gates.max_throttle:.0%} throttled CFS periods",
        "offenders": sorted(hot),
    })

    before, after = error_rate(baseline), error_rate(replay)
    if after is None or replay["requests"] < gates.min_requests:
        passed, detail = False, f"only {replay.get('requests') or 0} requests in the replay; cannot judge errors"
    elif before is None:
        passed = after <= gates.max_error_increase
        detail = f"error rate {after:.2%} with no baseline to compare against"
    else:
        passed = after - before <= gates.max_error_increase
        detail = (f"error rate {before:.2%} -> {after:.2%} "
                  f"(allowed rise {gates.max_error_increase:.2%})")
    results.append({"gate": "errors", "passed": passed, "detail": detail,
                    "baseline": before, "replay": after})
    return {"passed": all(r["passed"] for r in results), "gates": results}


# ---- talking to the cluster --------------------------------------------------

def restart_loadgenerator(kubectl, name, timeout_s=300):
    kubectl.run("rollout", "restart", f"deployment/{name}")
    kubectl.run("rollout", "status", f"deployment/{name}", f"--timeout={int(timeout_s)}s", timeout=timeout_s + 30)


def measure(kubectl, prom, window_s, loadgen="loadgenerator", container="main", log=print, sleep=time.sleep):
    """Restart the load generator, let it run for ``window_s`` and collect what happened."""
    restart_loadgenerator(kubectl, loadgen)
    start = time.time()
    before = container_states(kubectl.pods())
    log(f"  load running for {window_s:.0f}s")
    sleep(window_s)
    end = time.time()
    after = container_states(kubectl.pods())
    logs = kubectl.run("logs", f"deployment/{loadgen}", "-c", container, "--tail=400")
    counts = parse_locust(logs) or (0, 0)
    result = {
        "start": round(start, 1),
        "end": round(end, 1),
        "requests": counts[0],
        "failures": counts[1],
        "restarts": restarts_between(before, after, start),
        "throttling": throttling(prom.query(throttle_query(kubectl.namespace, end - start), end)),
    }
    q = usage_queries(kubectl.namespace, end - start)
    result["usage"] = usage(prom.query(q["cpu"], end), prom.query(q["memory"], end))
    result["error_rate"] = error_rate(result)
    return result


def apply_patches(kubectl, index, patch_dir, timeout_s=300, log=print):
    """kubectl-patch every workload in the index and wait for the rollouts; returns rollout problems."""
    problems = []
    for w in index["workloads"]:
        kubectl.run("patch", w["kind"].lower(), w["name"], "--type", "strategic",
                    "--patch-file", os.path.join(patch_dir, w["file"]))
    log(f"  patched {len(index['workloads'])} workload(s); waiting for rollouts")
    for w in index["workloads"]:
        try:
            kubectl.run("rollout", "status", f"{w['kind'].lower()}/{w['name']}", f"--timeout={int(timeout_s)}s",
                        timeout=timeout_s + 30)
        except KubectlError as exc:
            problems.append(f"{w['name']}: {exc}")
    return problems


def validate(kubectl, prom, index, patch_dir, window_s, gates=None, loadgen="loadgenerator", log=print):
    gates = gates or Gates()
    log("baseline window (cluster as it is)")
    baseline = measure(kubectl, prom, window_s, loadgen, log=log)
    log("applying patches")
    rollout = apply_patches(kubectl, index, patch_dir, log=log)
    log("replay window (patched)")
    replay = measure(kubectl, prom, window_s, loadgen, log=log)
    verdict = evaluate(baseline, replay, gates)
    verdict["gates"].insert(0, {
        "gate": "rollout",
        "passed": not rollout,
        "detail": "; ".join(rollout) if rollout else "every patched workload rolled out",
    })
    verdict["passed"] = all(g["passed"] for g in verdict["gates"])
    return {
        "format": "rightsize-validation/1",
        "namespace": kubectl.namespace,
        "window_seconds": window_s,
        "settings": asdict(gates),
        "patched": [
            {"workload": w["name"], "containers": {c["name"]: c["after"] for c in w["containers"]}}
            for w in index["workloads"]
        ],
        "passed": verdict["passed"],
        "gates": verdict["gates"],
        "baseline": baseline,
        "replay": replay,
    }
