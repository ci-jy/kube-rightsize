"""Tests on series recorded from the kind cluster running Online Boutique under load.

recordings/boutique-30m.json was made with scripts/up.sh --squeeze and
scripts/record.sh: 30 minutes of the boutique namespace while the
loadgenerator was running, with recommendationservice limited to 40Mi.
"""

import copy
import json
from pathlib import Path

import pytest

from rightsize.recommend import Policy, recommend, recommend_container
from rightsize.units import MI, parse_cpu, parse_memory

RECORDING = Path(__file__).resolve().parent.parent / "recordings" / "boutique-30m.json"
SERVICES = {
    "adservice", "cartservice", "checkoutservice", "currencyservice", "emailservice",
    "frontend", "loadgenerator", "paymentservice", "productcatalogservice",
    "recommendationservice", "redis-cart", "shippingservice",
}


@pytest.fixture(scope="module")
def recording():
    return json.loads(RECORDING.read_text())


@pytest.fixture(scope="module")
def result(recording):
    return recommend(recording)


def by_workload(result):
    return {r["workload"]: r for r in result["recommendations"]}


def entry(recording, workload):
    return copy.deepcopy(next(c for c in recording["containers"] if c["workload"] == workload))


def test_recording_covers_thirty_minutes_of_every_service(recording):
    assert recording["end"] - recording["start"] == pytest.approx(1800, abs=1)
    assert {c["workload"] for c in recording["containers"]} == SERVICES
    for c in recording["containers"]:
        samples = sum(len(p.get("memory", [])) for p in c["pods"])
        assert samples >= 100, c["workload"]


def test_every_container_gets_a_recommendation_with_reasons(result):
    recs = by_workload(result)
    assert set(recs) == SERVICES
    for r in recs.values():
        assert r["reasons"], r["workload"]
        assert r["confidence"] in {"low", "medium"}  # 30 minutes is never "high"
        assert r["recommended"]["requests"]["cpu"] and r["recommended"]["requests"]["memory"]


def test_oom_killed_recommendationservice_is_flagged_not_undersized(result):
    r = by_workload(result)["recommendationservice"]
    assert r["observed"]["oom_kills"] >= 1
    assert "memory-censored" in r["flags"]
    assert r["confidence"] == "low"
    old_limit = parse_memory(r["current"]["limits"]["memory"])
    assert old_limit == 40 * MI
    assert parse_memory(r["recommended"]["requests"]["memory"]) >= old_limit + 100 * MI


def test_uncensored_recommendations_cover_observed_percentiles(result):
    for r in result["recommendations"]:
        obs = r["observed"]
        mem = parse_memory(r["recommended"]["requests"]["memory"])
        cpu = parse_cpu(r["recommended"]["requests"]["cpu"])
        assert mem >= obs["memory_p99_mib"] * MI * 1.1 - 1, r["workload"]
        assert cpu * 1000 >= obs["cpu_p95_millicores"] - 0.01, r["workload"]
        assert parse_memory(r["recommended"]["limits"]["memory"]) >= obs["memory_max_mib"] * MI


def test_boutique_is_over_requested_overall(result):
    s = result["summary"]
    assert s["containers"] == len(SERVICES)
    assert s["censored"] >= 1
    assert s["memory_freed_percent"] > 0
    assert s["cpu_freed_percent"] > 0


def test_pooling_identical_replicas_does_not_change_the_answer(recording):
    single = entry(recording, "frontend")
    twin = copy.deepcopy(single["pods"][0])
    twin["pod"] = twin["pod"][:-5] + "zzzzz"
    double = copy.deepcopy(single)
    double["pods"].append(twin)
    a, b = recommend_container(single), recommend_container(double)
    assert b["replicas"] == 2 * a["replicas"]
    assert a["recommended"] == b["recommended"]


def test_a_hungry_replica_drives_the_request(recording):
    e = entry(recording, "cartservice")
    hungry = copy.deepcopy(e["pods"][0])
    hungry["pod"] = hungry["pod"][:-5] + "zzzzz"
    hungry["memory"] = [[t, v * 3] for t, v in hungry["memory"]]
    e["pods"].append(hungry)
    r = recommend_container(e)
    assert "replicas-uneven" in r["flags"]
    peak = max(v for _, v in hungry["memory"])
    assert parse_memory(r["recommended"]["requests"]["memory"]) >= peak


def test_truncated_history_does_not_shrink_requests(recording):
    e = entry(recording, "emailservice")
    for p in e["pods"]:
        cutoff = p["memory"][0][0] + 5 * 60
        for key in ("cpu", "memory", "throttled", "restarts"):
            p[key] = [pt for pt in p.get(key, []) if pt[0] < cutoff]
    r = recommend_container(e)
    assert "short-history" in r["flags"]
    for res in ("cpu", "memory"):
        parse = parse_cpu if res == "cpu" else parse_memory
        assert parse(r["recommended"]["requests"][res]) >= parse(r["current"]["requests"][res])


def test_stricter_percentile_never_lowers_requests(recording):
    loose = recommend(recording, Policy(cpu_percentile=90, memory_percentile=95))
    strict = recommend(recording, Policy(cpu_percentile=99, memory_percentile=99.9))
    for a, b in zip(loose["recommendations"], strict["recommendations"]):
        assert parse_cpu(b["recommended"]["requests"]["cpu"]) >= parse_cpu(a["recommended"]["requests"]["cpu"])
        assert parse_memory(b["recommended"]["requests"]["memory"]) >= parse_memory(a["recommended"]["requests"]["memory"])
