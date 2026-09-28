"""Turn recommendations into Kustomize strategic-merge patches and a readable diff.

One patch file per workload, touching only ``resources`` of the containers
whose numbers actually change. CPU limits are left as they are by default:
the recommender suggests none, but dropping an existing limit is a separate
decision from resizing a request, so it is opt-in (``cpu_limits="drop"``).
A kept CPU limit that would end up below the new request is raised to match,
since the API server rejects a request above its limit.
"""

import difflib
import os

from .units import parse_cpu

API_VERSION = {"Deployment": "apps/v1", "StatefulSet": "apps/v1", "DaemonSet": "apps/v1"}


def _cpu(q):
    return parse_cpu(q) if q else None


def plan_container(rec, cpu_limits="keep"):
    """Before/after ``resources`` for one recommendation, or None if nothing would change."""
    if "no-data" in rec["flags"]:
        return None
    cur, new = rec["current"], rec["recommended"]
    before = {
        "requests": {k: v for k, v in cur["requests"].items() if v},
        "limits": {k: v for k, v in cur["limits"].items() if v},
    }
    after = {
        "requests": {k: v for k, v in new["requests"].items() if v},
        "limits": {k: v for k, v in before["limits"].items() if k != "memory"},
    }
    if new["limits"].get("memory"):
        after["limits"]["memory"] = new["limits"]["memory"]
    notes = []
    old_cpu_limit = cur["limits"].get("cpu")
    if cpu_limits == "drop" and new["limits"].get("cpu") is None:
        after["limits"].pop("cpu", None)
    elif new["limits"].get("cpu"):
        after["limits"]["cpu"] = new["limits"]["cpu"]
    elif old_cpu_limit and _cpu(old_cpu_limit) < _cpu(after["requests"].get("cpu")):
        after["limits"]["cpu"] = after["requests"]["cpu"]
        notes.append(f"CPU limit raised from {old_cpu_limit} to the new request {after['requests']['cpu']}.")
    if not after["limits"]:
        del after["limits"]
    if not before["limits"]:
        del before["limits"]
    if before == after:
        return None
    return {"name": rec["container"], "before": before, "after": after,
            "confidence": rec["confidence"], "flags": rec["flags"], "notes": notes}


def build_patches(recommendations, cpu_limits="keep", min_confidence="low"):
    """Group recommendations by workload into patch plans, skipping unchanged containers."""
    rank = {"none": 0, "low": 1, "medium": 2, "high": 3}
    plans = {}
    for rec in recommendations["recommendations"]:
        if rank[rec["confidence"]] < rank[min_confidence]:
            continue
        c = plan_container(rec, cpu_limits)
        if c is None:
            continue
        kind = rec.get("kind", "Deployment")
        if kind not in API_VERSION:
            continue  # bare pods cannot be patched in place
        key = (rec.get("namespace"), kind, rec["workload"])
        plans.setdefault(key, {"namespace": key[0], "kind": kind, "name": key[2], "containers": []})
        plans[key]["containers"].append(c)
    return [plans[k] for k in sorted(plans, key=lambda k: (k[0] or "", k[1], k[2]))]


def manifest(plan, drop_cpu_limits=False):
    """The strategic-merge patch for one workload as a plain dict."""
    containers = []
    for c in plan["containers"]:
        res = {"requests": dict(c["after"]["requests"])}
        limits = dict(c["after"].get("limits", {}))
        if drop_cpu_limits and "cpu" in c["before"].get("limits", {}) and "cpu" not in limits:
            limits["cpu"] = None  # null deletes the key in a strategic merge
        if limits:
            res["limits"] = limits
        containers.append({"name": c["name"], "resources": res})
    meta = {"name": plan["name"]}
    if plan["namespace"]:
        meta["namespace"] = plan["namespace"]
    return {
        "apiVersion": API_VERSION[plan["kind"]],
        "kind": plan["kind"],
        "metadata": meta,
        "spec": {"template": {"spec": {"containers": containers}}},
    }


def to_yaml(value, indent=0):
    """Just enough YAML for patches: dicts, lists, strings, numbers and null."""
    pad = "  " * indent
    lines = []
    if isinstance(value, dict):
        for k, v in value.items():
            if isinstance(v, (dict, list)) and v:
                lines.append(f"{pad}{k}:")
                lines.append(to_yaml(v, indent + 1))
            else:
                lines.append(f"{pad}{k}: {_scalar(v)}")
    elif isinstance(value, list):
        for item in value:
            body = to_yaml(item, indent + 1).lstrip()
            lines.append(f"{pad}- {body}")
    else:
        lines.append(pad + _scalar(value))
    return "\n".join(lines)


def _scalar(v):
    if v is None:
        return "null"
    if v == {}:
        return "{}"
    if v == []:
        return "[]"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return str(v)
    s = str(v)
    if s == "" or s[0] in "-?:,[]{}#&*!|>'\"%@`" or ": " in s or s.lower() in ("null", "true", "false", "yes", "no"):
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'
    return s


def _resources_text(res):
    lines = ["resources:"]
    for section in ("requests", "limits"):
        if res.get(section):
            lines.append(f"  {section}:")
            lines += [f"    {k}: {res[section][k]}" for k in ("cpu", "memory") if k in res[section]]
    return lines


def diff_text(plans):
    """Unified diff of each container's resources block, before and after."""
    out = []
    for plan in plans:
        for c in plan["containers"]:
            where = f"{plan['kind'].lower()}/{plan['name']} container {c['name']}"
            out += difflib.unified_diff(
                _resources_text(c["before"]), _resources_text(c["after"]),
                fromfile=f"a/{where}", tofile=f"b/{where}", lineterm="", n=10,
            )
            extra = [f"confidence {c['confidence']}"] + (c["flags"] or []) + c["notes"]
            out.append("# " + "; ".join(extra))
            out.append("")
    return "\n".join(out)


def file_name(plan):
    return f"{plan['kind'].lower()}-{plan['name']}.yaml"


def write_patches(plans, out_dir, base=None, cpu_limits="keep"):
    """Write one patch per workload, a kustomization.yaml, diff.txt and an index; return the paths."""
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith(".yaml") and old.split("-", 1)[0] in ("deployment", "statefulset", "daemonset"):
            os.remove(os.path.join(out_dir, old))
    files = []
    for plan in plans:
        name = file_name(plan)
        with open(os.path.join(out_dir, name), "w") as fh:
            fh.write(to_yaml(manifest(plan, cpu_limits == "drop")) + "\n")
        files.append(name)
    kust = {"apiVersion": "kustomize.config.k8s.io/v1beta1", "kind": "Kustomization"}
    if base:
        kust["resources"] = [os.path.relpath(os.path.abspath(base), os.path.abspath(out_dir))]
    kust["patches"] = [{"path": f} for f in files]
    with open(os.path.join(out_dir, "kustomization.yaml"), "w") as fh:
        if not base:
            fh.write("# Patches only: add your base under resources:, or apply each file with\n"
                     "# kubectl patch <kind> <name> --type strategic --patch-file <file>.\n")
        fh.write(to_yaml(kust) + "\n")
    with open(os.path.join(out_dir, "diff.txt"), "w") as fh:
        fh.write(diff_text(plans))
    index = {
        "format": "rightsize-patches/1",
        "cpu_limits": cpu_limits,
        "workloads": [
            {"namespace": p["namespace"], "kind": p["kind"], "name": p["name"], "file": file_name(p),
             "containers": p["containers"]}
            for p in plans
        ],
    }
    return files, index


def summary_line(plans):
    n = sum(len(p["containers"]) for p in plans)
    return f"{n} container(s) in {len(plans)} workload(s) patched"

