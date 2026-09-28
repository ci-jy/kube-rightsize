import json
import os

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rightsize.cli import main
from rightsize.patch import build_patches, diff_text, manifest, plan_container, to_yaml, write_patches
from rightsize.units import parse_cpu

ROOT = os.path.dirname(os.path.dirname(__file__))
RECORDING = os.path.join(ROOT, "recordings", "boutique-30m.json")


def rec(workload="svc", container="server", cur=None, new=None, flags=(), confidence="medium", kind="Deployment"):
    cur = cur or {"requests": {"cpu": "200m", "memory": "128Mi"}, "limits": {"cpu": "300m", "memory": "256Mi"}}
    new = new or {"requests": {"cpu": "20m", "memory": "64Mi"}, "limits": {"cpu": None, "memory": "80Mi"}}
    return {"namespace": "shop", "workload": workload, "kind": kind, "container": container,
            "current": cur, "recommended": new, "flags": list(flags), "confidence": confidence}


def recs(*items):
    return {"recommendations": list(items)}


def test_patch_sets_requests_and_memory_limit_but_keeps_cpu_limit():
    [plan] = build_patches(recs(rec()))
    m = manifest(plan)
    assert m["kind"] == "Deployment" and m["apiVersion"] == "apps/v1"
    assert m["metadata"] == {"name": "svc", "namespace": "shop"}
    [c] = m["spec"]["template"]["spec"]["containers"]
    assert c == {"name": "server", "resources": {
        "requests": {"cpu": "20m", "memory": "64Mi"},
        "limits": {"cpu": "300m", "memory": "80Mi"},
    }}


def test_drop_mode_deletes_cpu_limit_with_null():
    [plan] = build_patches(recs(rec()), cpu_limits="drop")
    [c] = manifest(plan, drop_cpu_limits=True)["spec"]["template"]["spec"]["containers"]
    assert c["resources"]["limits"] == {"memory": "80Mi", "cpu": None}
    assert "cpu: null" in to_yaml(manifest(plan, drop_cpu_limits=True))


def test_kept_cpu_limit_below_new_request_is_raised():
    r = rec(cur={"requests": {"cpu": "50m", "memory": "32Mi"}, "limits": {"cpu": "100m", "memory": "40Mi"}},
            new={"requests": {"cpu": "250m", "memory": "140Mi"}, "limits": {"cpu": None, "memory": "176Mi"}},
            flags=["cpu-censored"], confidence="low")
    [plan] = build_patches(recs(r))
    after = plan["containers"][0]["after"]
    assert after["limits"]["cpu"] == "250m"
    assert "raised" in plan["containers"][0]["notes"][0]


def test_unchanged_and_no_data_containers_are_skipped():
    same = rec(workload="same", new={"requests": {"cpu": "200m", "memory": "128Mi"},
                                     "limits": {"cpu": None, "memory": "256Mi"}})
    empty = rec(workload="empty", flags=["no-data"], confidence="none")
    assert plan_container(same) is None
    assert build_patches(recs(same, empty)) == []


def test_containers_of_one_workload_share_a_patch_and_bare_pods_are_left_out():
    plans = build_patches(recs(rec(container="app"), rec(container="sidecar"), rec(workload="lonely", kind="Pod")))
    assert [p["name"] for p in plans] == ["svc"]
    assert [c["name"] for c in plans[0]["containers"]] == ["app", "sidecar"]


def test_min_confidence_filters_low_confidence_recommendations():
    plans = build_patches(recs(rec(workload="a", confidence="low"), rec(workload="b")), min_confidence="medium")
    assert [p["name"] for p in plans] == ["b"]


def test_diff_shows_only_changed_lines():
    [plan] = build_patches(recs(rec()))
    text = diff_text([plan])
    assert "--- a/deployment/svc container server" in text
    assert "-    cpu: 200m" in text and "+    cpu: 20m" in text
    assert "     cpu: 300m" in text  # the kept CPU limit is context, not a change
    assert "-    memory: 256Mi" in text and "+    memory: 80Mi" in text


def test_yaml_quotes_strings_that_would_change_meaning():
    assert to_yaml({"a": "no", "b": "-x", "c": "12Mi", "d": None}) == 'a: "no"\nb: "-x"\nc: 12Mi\nd: null'
    assert to_yaml({"l": [{"x": 1, "y": {"z": "w"}}]}) == "l:\n  - x: 1\n    y:\n      z: w"


@given(st.integers(1, 4000), st.integers(1, 4000), st.integers(1, 4000))
def test_patched_cpu_request_never_exceeds_its_limit(old_limit, new_req, old_req):
    r = rec(cur={"requests": {"cpu": f"{old_req}m", "memory": "64Mi"},
                 "limits": {"cpu": f"{old_limit}m", "memory": "128Mi"}},
            new={"requests": {"cpu": f"{new_req}m", "memory": "64Mi"}, "limits": {"cpu": None, "memory": "96Mi"}})
    c = plan_container(r)
    lim = c["after"]["limits"]["cpu"]
    assert parse_cpu(c["after"]["requests"]["cpu"]) <= parse_cpu(lim)
    assert parse_cpu(lim) >= old_limit / 1000  # limits are only ever kept or raised


def test_boutique_recommendations_become_twelve_deployment_patches(tmp_path):
    recs_path = tmp_path / "recs.json"
    assert main(["recommend", "--from-recording", RECORDING, "--out", str(recs_path), "-q"]) == 0
    out = tmp_path / "patches"
    assert main(["patch", "--recommendations", str(recs_path), "--out", str(out), "-q"]) == 0
    index = json.loads((out / "patches.json").read_text())
    names = [w["name"] for w in index["workloads"]]
    assert len(names) == 12 and "recommendationservice" in names
    assert all(w["kind"] == "Deployment" for w in index["workloads"])
    squeezed = (out / "deployment-recommendationservice.yaml").read_text()
    assert "memory: 140Mi" in squeezed  # censored: raised, not shrunk
    kust = (out / "kustomization.yaml").read_text()
    assert kust.count("- path: deployment-") == 12
    assert "+    memory: 140Mi" in (out / "diff.txt").read_text()


def test_write_patches_lists_base_and_clears_stale_files(tmp_path):
    (tmp_path / "base").mkdir()
    out = tmp_path / "patches"
    out.mkdir()
    (out / "deployment-gone.yaml").write_text("stale")
    [plan] = build_patches(recs(rec()))
    files, _ = write_patches([plan], str(out), base=str(tmp_path / "base"))
    assert files == ["deployment-svc.yaml"]
    assert not (out / "deployment-gone.yaml").exists()
    assert "resources:\n  - ../base" in (out / "kustomization.yaml").read_text()


@pytest.mark.parametrize("value", ["true", "yes", "a: b", ""])
def test_yaml_scalars_that_need_quotes(value):
    assert to_yaml(value).startswith('"')
