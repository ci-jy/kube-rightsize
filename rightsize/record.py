"""Snapshot container usage series from Prometheus into a JSON recording.

A recording keeps the raw per-pod series so recommendations can be
recomputed offline (and in tests) without a cluster.
"""

import re
import time

from . import __version__

# Deployment pods: <workload>-<replicaset hash>-<pod hash>; StatefulSet pods: <workload>-<ordinal>.
_DEPLOYMENT_POD = re.compile(r"^(?P<w>.+)-[a-z0-9]{6,10}-[a-z0-9]{5}$")
_STATEFUL_POD = re.compile(r"^(?P<w>.+)-\d+$")
_DAEMONSET_POD = re.compile(r"^(?P<w>.+)-[a-z0-9]{5}$")


def workload_of(pod):
    for pattern in (_DEPLOYMENT_POD, _STATEFUL_POD, _DAEMONSET_POD):
        m = pattern.match(pod)
        if m:
            return m.group("w")
    return pod


def _queries(namespace, rate_window):
    sel = f'namespace="{namespace}",container!="",container!="POD"'
    by = "by (namespace, pod, container)"
    return {
        "cpu": f"sum {by} (rate(container_cpu_usage_seconds_total{{{sel}}}[{rate_window}]))",
        "memory": f"max {by} (container_memory_working_set_bytes{{{sel}}})",
        "throttled": (
            f"sum {by} (rate(container_cpu_cfs_throttled_periods_total{{{sel}}}[{rate_window}]))"
            f" / sum {by} (rate(container_cpu_cfs_periods_total{{{sel}}}[{rate_window}]))"
        ),
        "restarts": f"max {by} (kube_pod_container_status_restarts_total{{{sel}}})",
        "oom_last": (
            f'max {by} (kube_pod_container_status_last_terminated_reason{{{sel},reason="OOMKilled"}})'
        ),
        "oom_events": f"max {by} (container_oom_events_total{{{sel}}})",
        "requests": f"max by (namespace, pod, container, resource) (kube_pod_container_resource_requests{{{sel}}})",
        "info": f"max {by} (kube_pod_container_info{{{sel}}})",
        "limits": f"max by (namespace, pod, container, resource) (kube_pod_container_resource_limits{{{sel}}})",
    }


def _points(values):
    return [[float(ts), float(v)] for ts, v in values if v not in ("NaN", "+Inf", "-Inf")]


def record(prom, namespace, duration_s, step_s=15, end=None, rate_window="1m"):
    end = time.time() if end is None else end
    start = end - duration_s
    containers = {}

    def slot(metric):
        pod, container = metric["pod"], metric["container"]
        key = (workload_of(pod), container)
        entry = containers.setdefault(
            key,
            {"namespace": namespace, "workload": key[0], "container": container, "pods": {}},
        )
        return entry["pods"].setdefault(pod, {"pod": pod})

    for name, query in _queries(namespace, rate_window).items():
        for series in prom.query_range(query, start, end, step_s):
            metric = series["metric"]
            if "pod" not in metric or "container" not in metric:
                continue
            pts = _points(series["values"])
            if not pts:
                continue
            pod = slot(metric)
            if name in ("requests", "limits"):
                resource = metric.get("resource")
                if resource in ("cpu", "memory"):
                    pod.setdefault(name, {})[resource] = pts[-1][1]
            else:
                pod[name] = pts

    # cAdvisor also reports init containers; kube_pod_container_info only lists
    # regular ones, which are the only ones whose requests matter here.
    regular = {k for k, e in containers.items() if any("info" in p for p in e["pods"].values())}
    if regular:
        containers = {k: e for k, e in containers.items() if k in regular}
    out = []
    for key in sorted(containers):
        entry = containers[key]
        for p in entry["pods"].values():
            p.pop("info", None)
        entry["pods"] = [entry["pods"][p] for p in sorted(entry["pods"])]
        out.append(entry)
    return {
        "format": "rightsize-recording/1",
        "rightsize_version": __version__,
        "prometheus": prom.base_url,
        "namespace": namespace,
        "start": start,
        "end": end,
        "step_seconds": step_s,
        "rate_window": rate_window,
        "containers": out,
    }
