import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from helpers import MI, container, pod, recording
from rightsize.recommend import Policy, recommend, recommend_container
from rightsize.units import parse_cpu, parse_memory

N = 120  # 30 minutes at 15 s


def req(r, resource):
    q = r["recommended"]["requests"][resource]
    return parse_cpu(q) if resource == "cpu" else parse_memory(q)


def flat(value, n=N):
    return [value] * n


def test_steady_container_gets_p95_cpu_and_p99_plus_ten_percent_memory():
    cpu = [0.02] * 100 + [0.05] * 20
    mem = [100 * MI] * 119 + [120 * MI]
    r = recommend_container(container(pod("svc-abc12345-x1y2z", cpu, mem)))
    assert req(r, "cpu") == pytest.approx(0.05)
    # P99 of 119x100Mi + 1x120Mi is 100.2Mi (interpolated); +10% -> 110.22Mi -> next 4Mi step.
    assert req(r, "memory") == 112 * MI
    assert r["flags"] == []
    assert r["confidence"] == "medium"  # 30 minutes is well short of a day


def test_floors_apply_to_idle_containers():
    r = recommend_container(container(pod("p", flat(0.0005), flat(3 * MI))))
    assert req(r, "cpu") == pytest.approx(0.010)
    assert req(r, "memory") == 32 * MI
    assert any("floor" in reason for reason in r["reasons"])


def test_oom_killed_container_is_censored_not_shrunk():
    # Working set climbs to the 64Mi limit and the container is OOM-killed twice.
    mem = ([40 * MI] * 30 + [63 * MI] * 10) * 3
    restarts = [0] * 40 + [1] * 40 + [2] * 40
    oom = [0] * 40 + [1] * 80
    p = pod("svc-abc12345-x1y2z", flat(0.02), mem, restarts=restarts, oom_last=oom,
            requests={"cpu": 0.1, "memory": 48 * MI}, limits={"memory": 64 * MI})
    r = recommend_container(container(p))
    assert "memory-censored" in r["flags"]
    assert r["observed"]["oom_kills"] == 2
    assert r["confidence"] == "low"
    assert req(r, "memory") >= 64 * MI * 1.25
    assert parse_memory(r["recommended"]["limits"]["memory"]) > 64 * MI
    assert "restarted" not in r["flags"]  # restarts explained by OOM


def test_working_set_pinned_at_limit_counts_as_censored_without_a_kill():
    p = pod("p", flat(0.02), flat(127 * MI), limits={"memory": 128 * MI})
    r = recommend_container(container(p))
    assert "memory-censored" in r["flags"]
    assert req(r, "memory") >= 160 * MI


def test_throttled_cpu_is_censored():
    p = pod("p", flat(0.19), flat(50 * MI), limits={"cpu": 0.2, "memory": 256 * MI},
            throttled=flat(0.6))
    r = recommend_container(container(p))
    assert "cpu-censored" in r["flags"]
    assert req(r, "cpu") >= 0.25


def test_non_oom_restart_drops_warmup_cpu_and_lowers_confidence():
    cpu = flat(0.02)
    cpu[60:64] = [2.0] * 4  # startup burst right after the restart
    restarts = [0] * 60 + [1] * 60
    r = recommend_container(container(pod("p", cpu, flat(80 * MI), restarts=restarts)))
    assert "restarted" in r["flags"]
    assert r["observed"]["restarts"] == 1
    assert req(r, "cpu") == pytest.approx(0.02)


def test_replicas_are_pooled_and_uneven_replicas_sized_for_the_hungriest():
    even = recommend_container(container(
        pod("svc-abc12345-aaaaa", flat(0.02), flat(100 * MI)),
        pod("svc-abc12345-bbbbb", flat(0.02), flat(110 * MI)),
    ))
    assert even["replicas"] == 2
    assert "replicas-uneven" not in even["flags"]

    uneven = recommend_container(container(
        pod("svc-abc12345-aaaaa", flat(0.02), flat(50 * MI)),
        pod("svc-abc12345-bbbbb", flat(0.02), flat(200 * MI)),
    ))
    assert "replicas-uneven" in uneven["flags"]
    assert req(uneven, "memory") >= 220 * MI


def test_replaced_pod_counts_as_one_replica():
    # Old pod for the first half, its replacement for the second half.
    old = pod("svc-abc12345-aaaaa", flat(0.02, 60), flat(90 * MI, 60))
    new = pod("svc-abc12345-bbbbb", flat(0.02, 60), flat(90 * MI, 60), start=60 * 15.0)
    r = recommend_container(container(old, new))
    assert r["replicas"] == 1


def test_short_history_never_lowers_requests():
    p = pod("p", flat(0.001, 20), flat(10 * MI, 20), requests={"cpu": 0.1, "memory": 128 * MI})
    r = recommend_container(container(p))
    assert "short-history" in r["flags"]
    assert r["confidence"] == "low"
    assert req(r, "cpu") >= 0.1
    assert req(r, "memory") >= 128 * MI


def test_container_without_samples_keeps_current_settings():
    entry = container({"pod": "p", "requests": {"cpu": 0.1, "memory": 64 * MI}, "limits": {}})
    r = recommend_container(entry)
    assert r["flags"] == ["no-data"]
    assert r["recommended"] == r["current"]


def test_summary_weights_by_replicas_and_reports_freed_capacity():
    rec = recording(
        container(pod("a-abc12345-aaaaa", flat(0.02), flat(50 * MI)),
                  pod("a-abc12345-bbbbb", flat(0.02), flat(50 * MI)), workload="a"),
        container(pod("b-abc12345-aaaaa", flat(0.02), flat(50 * MI)), workload="b"),
    )
    out = recommend(rec)
    s = out["summary"]
    assert s["containers"] == 2  # two workloads
    assert s["cpu_requested_millicores"] == 300  # 3 pods x 100m
    assert s["memory_requested_mib"] == 384.0
    assert s["cpu_recommended_millicores"] == 60
    assert s["cpu_freed_percent"] == 80.0
    assert "_raw" not in out["recommendations"][0]


# ---- property tests ---------------------------------------------------------

usage = st.lists(st.floats(1 * MI, 2048 * MI), min_size=40, max_size=150)
cpus = st.lists(st.floats(0, 4), min_size=40, max_size=150)


@settings(max_examples=150, deadline=None)
@given(usage, cpus)
def test_recommendation_respects_floors_and_covers_the_percentile(mem, cpu):
    n = min(len(mem), len(cpu))
    p = pod("p", cpu[:n], mem[:n], limits={})
    r = recommend_container(container(p))
    pol = Policy()
    assert req(r, "cpu") >= pol.cpu_floor
    assert req(r, "memory") >= pol.memory_floor
    assert req(r, "memory") >= r["observed"]["memory_p99_mib"] * MI * 1.1 - 1
    assert req(r, "cpu") * 1000 >= r["observed"]["cpu_p95_millicores"] - 0.01
    assert parse_memory(r["recommended"]["limits"]["memory"]) >= req(r, "memory")


@settings(max_examples=150, deadline=None)
@given(usage, st.floats(16 * MI, 1024 * MI), st.integers(1, 5))
def test_oom_killed_never_recommended_below_its_old_limit(mem, limit, kills):
    n = len(mem)
    mem = [min(m, limit) for m in mem]  # usage cannot exceed the limit
    # Spread `kills` restarts over the window, each flagged as OOMKilled.
    restarts = [min(kills, i * (kills + 1) // n) for i in range(n)]
    oom = [1 if x else 0 for x in restarts]
    p = pod("p", [0.01] * n, mem, restarts=restarts, oom_last=oom, limits={"memory": limit})
    r = recommend_container(container(p))
    assert "memory-censored" in r["flags"]
    assert r["confidence"] == "low"
    assert req(r, "memory") >= limit * Policy().censored_bump - 1


@settings(max_examples=100, deadline=None)
@given(usage, st.floats(1.0, 3.0))
def test_more_usage_never_means_smaller_request(mem, factor):
    a = recommend_container(container(pod("p", [0.05] * len(mem), mem, limits={})))
    b = recommend_container(container(pod("p", [0.05] * len(mem), [m * factor for m in mem], limits={})))
    assert req(b, "memory") >= req(a, "memory")
