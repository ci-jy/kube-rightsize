import json

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rightsize.validate import (
    Gates, container_states, evaluate, measure, parse_locust, restarts_between, throttle_query, throttling,
    usage, usage_queries,
)

LOCUST_LOG = """\
Type     Name                                                                          # reqs      # fails |    Avg     Min     Max    Med |   req/s  failures/s
--------|----------------------------------------------------------------------------|-------|-------------|-------|-------|-------|-------|--------|-----------
GET      /                                                                                 41     0(0.00%) |     71      13     367     42 |    0.80        0.00
--------|----------------------------------------------------------------------------|-------|-------------|-------|-------|-------|-------|--------|-----------
         Aggregated                                                                       304     2(0.66%) |     60      10     367     40 |    5.10        0.03

Type     Name                                                                          # reqs      # fails |    Avg     Min     Max    Med |   req/s  failures/s
--------|----------------------------------------------------------------------------|-------|-------------|-------|-------|-------|-------|--------|-----------
         Aggregated                                                                      1210    13(1.07%) |     58      10     900     40 |    5.20        0.05
"""


def window(requests=1000, failures=0, restarts=None, throttle=None):
    return {"requests": requests, "failures": failures, "restarts": restarts or {}, "throttling": throttle or {}}


def gate(result, name):
    return next(g for g in result["gates"] if g["gate"] == name)


def test_parse_locust_takes_the_last_aggregated_row():
    assert parse_locust(LOCUST_LOG) == (1210, 13)
    assert parse_locust("starting locust\n") is None


def test_clean_replay_passes_every_gate():
    result = evaluate(window(failures=2), window(failures=3, throttle={"frontend/server": 0.05}))
    assert result["passed"]
    assert [g["gate"] for g in result["gates"]] == ["oom", "throttling", "errors"]


def test_any_oom_kill_in_the_replay_fails():
    replay = window(restarts={"cartservice/server": {"restarts": 2, "oom_kills": 2}})
    result = evaluate(window(), replay)
    assert not result["passed"]
    assert gate(result, "oom")["offenders"] == ["cartservice/server"]
    assert "x2" in gate(result, "oom")["detail"]


def test_restarts_that_are_not_oom_do_not_trip_the_oom_gate():
    replay = window(restarts={"cartservice/server": {"restarts": 1, "oom_kills": 0}})
    assert gate(evaluate(window(), replay), "oom")["passed"]


def test_throttling_above_threshold_fails_only_if_it_rose():
    base = window(throttle={"adservice/server": 0.40, "frontend/server": 0.10})
    replay = window(throttle={"adservice/server": 0.45, "frontend/server": 0.30})
    g = gate(evaluate(base, replay), "throttling")
    assert not g["passed"]
    assert g["offenders"] == ["frontend/server"]  # adservice was already this throttled before


def test_throttle_threshold_is_configurable():
    replay = window(throttle={"frontend/server": 0.30})
    assert gate(evaluate(window(), replay, Gates(max_throttle=0.5)), "throttling")["passed"]


def test_error_rate_rise_beyond_allowance_fails():
    g = gate(evaluate(window(failures=5), window(failures=30)), "errors")
    assert not g["passed"]
    assert g["baseline"] == pytest.approx(0.005) and g["replay"] == pytest.approx(0.03)


def test_lower_error_rate_than_baseline_passes():
    # The squeezed baseline crash-loops a backend, so a healthy replay has fewer errors.
    assert gate(evaluate(window(failures=80), window(failures=1)), "errors")["passed"]


def test_too_few_requests_cannot_pass():
    g = gate(evaluate(window(), window(requests=10)), "errors")
    assert not g["passed"] and "cannot judge" in g["detail"]


@given(st.integers(100, 100_000), st.data())
def test_error_gate_matches_the_allowed_rise(n, data):
    base_fail = data.draw(st.integers(0, n))
    rep_fail = data.draw(st.integers(0, n))
    g = gate(evaluate(window(n, base_fail), window(n, rep_fail)), "errors")
    assert g["passed"] == ((rep_fail - base_fail) / n <= Gates.max_error_increase)


def pod(name, container="server", restarts=0, reason=None, finished=None):
    last = {"terminated": {"reason": reason, "finishedAt": finished}} if reason else {}
    return {"metadata": {"name": name}, "status": {"containerStatuses": [
        {"name": container, "restartCount": restarts, "lastState": last},
    ]}}


def test_restarts_between_counts_oom_kills_per_workload():
    before = container_states([pod("cart-7d446cd6cd-jvf2d"), pod("rec-5767cf4d97-hfwk7", restarts=3,
                                                                   reason="OOMKilled", finished="2030-01-01T00:00:00Z")])
    after = container_states([pod("cart-7d446cd6cd-jvf2d"), pod("rec-5767cf4d97-hfwk7", restarts=5,
                                                                  reason="OOMKilled", finished="2030-01-01T00:04:00Z")])
    assert restarts_between(before, after) == {"rec/server": {"restarts": 2, "oom_kills": 2}}


def test_old_oom_before_the_window_is_not_counted():
    old = pod("rec-5767cf4d97-hfwk7", restarts=1, reason="OOMKilled", finished="2030-01-01T00:00:00Z")
    s = container_states([old])
    start = s["rec-5767cf4d97-hfwk7/server"]["last_finished"] + 60
    assert restarts_between(s, s, start) == {}


def test_new_pod_counts_all_its_restarts():
    after = container_states([pod("cart-7d446cd6cd-abcde", restarts=1, reason="Error",
                                  finished="2030-01-01T00:00:00Z")])
    assert restarts_between({}, after) == {"cart/server": {"restarts": 1, "oom_kills": 0}}


def test_throttling_keeps_the_worst_replica_and_skips_unlimited_containers():
    result = [
        {"metric": {"pod": "frontend-6d47d98676-kpxg9", "container": "server"}, "value": [0, "0.1"]},
        {"metric": {"pod": "frontend-6d47d98676-zzzzz", "container": "server"}, "value": [0, "0.3"]},
        {"metric": {"pod": "redis-cart-c8ff86559-sxscx", "container": "redis"}, "value": [0, "NaN"]},
    ]
    assert throttling(result) == {"frontend/server": 0.3}
    assert "[300s]" in throttle_query("boutique", 300)


def test_usage_keeps_the_busiest_replica_in_millicores_and_mib():
    cpu = [
        {"metric": {"pod": "frontend-6d47d98676-kpxg9", "container": "server"}, "value": [0, "0.004"]},
        {"metric": {"pod": "frontend-6d47d98676-zzzzz", "container": "server"}, "value": [0, "0.0125"]},
        {"metric": {"pod": "redis-cart-c8ff86559-sxscx", "container": "redis"}, "value": [0, "NaN"]},
    ]
    memory = [{"metric": {"pod": "frontend-6d47d98676-kpxg9", "container": "server"}, "value": [0, str(20 * 2**20)]}]
    assert usage(cpu, memory) == {"frontend/server": {"cpu_millicores": 12.5, "memory_mib": 20.0}}
    q = usage_queries("boutique", 300)
    assert "max_over_time(container_memory_working_set_bytes" in q["memory"] and "[300s]" in q["cpu"]


class FakeKubectl:
    namespace = "boutique"

    def __init__(self):
        self.calls = []
        self.snapshots = [
            [pod("rec-5767cf4d97-hfwk7", restarts=0)],
            [pod("rec-5767cf4d97-hfwk7", restarts=1, reason="OOMKilled", finished="2030-01-01T00:00:00Z")],
        ]

    def run(self, *args, timeout=600):
        self.calls.append(args)
        return LOCUST_LOG if args[0] == "logs" else ""

    def pods(self):
        return self.snapshots.pop(0)


class FakeProm:
    def query(self, query, at=None):
        return [{"metric": {"pod": "rec-5767cf4d97-hfwk7", "container": "server"}, "value": [at, "0.02"]}]


def test_measure_restarts_load_and_collects_one_window():
    kubectl = FakeKubectl()
    slept = []
    w = measure(kubectl, FakeProm(), 300, log=lambda m: None, sleep=slept.append)
    assert slept == [300]
    assert kubectl.calls[0] == ("rollout", "restart", "deployment/loadgenerator")
    assert (w["requests"], w["failures"]) == (1210, 13)
    assert w["restarts"] == {"rec/server": {"restarts": 1, "oom_kills": 1}}
    assert w["throttling"] == {"rec/server": 0.02}
    assert w["usage"]["rec/server"]["cpu_millicores"] == 20.0
    json.dumps(w)  # goes straight into the results file


def test_committed_boutique_run_re_evaluates_to_the_same_verdict():
    import os
    path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "results", "boutique-validation.json")
    with open(path) as fh:
        run = json.load(fh)
    again = evaluate(run["baseline"], run["replay"], Gates(**run["settings"]))
    assert again["passed"] and run["passed"]
    assert [g["detail"] for g in again["gates"]] == [g["detail"] for g in run["gates"][1:]]
    # The squeezed service was OOM-killed before patching and not after.
    assert run["baseline"]["restarts"]["recommendationservice/server"]["oom_kills"] > 0
    assert run["replay"]["restarts"] == {}
