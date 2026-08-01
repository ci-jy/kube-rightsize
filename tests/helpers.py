"""Build small synthetic recordings for unit tests."""

MI = 1024 * 1024
STEP = 15.0


def series(values, start=0.0, step=STEP):
    return [[start + i * step, float(v)] for i, v in enumerate(values)]


def pod(name, cpu, memory, *, requests=None, limits=None, restarts=None, oom_last=None,
        throttled=None, start=0.0):
    p = {
        "pod": name,
        "cpu": series(cpu, start),
        "memory": series(memory, start),
        "requests": requests or {"cpu": 0.1, "memory": 128 * MI},
        "limits": limits or {"memory": 256 * MI},
    }
    if restarts is not None:
        p["restarts"] = series(restarts, start)
    if oom_last is not None:
        p["oom_last"] = series(oom_last, start)
    if throttled is not None:
        p["throttled"] = series(throttled, start)
    return p


def container(*pods, workload="svc", name="server"):
    return {"namespace": "test", "workload": workload, "container": name, "pods": list(pods)}


def recording(*containers):
    return {"format": "rightsize-recording/1", "namespace": "test", "start": 0, "end": 1800,
            "step_seconds": STEP, "containers": list(containers)}
