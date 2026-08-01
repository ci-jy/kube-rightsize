import json

import pytest

from rightsize.cli import main, parse_duration
from rightsize.record import record, workload_of


@pytest.mark.parametrize("pod,workload", [
    ("cartservice-7c9d8b6f5d-x2k9p", "cartservice"),
    ("redis-cart-5b6f47b748-abcde", "redis-cart"),
    ("prometheus-server-0", "prometheus-server"),
    ("kindnet-w2wzj", "kindnet"),
    ("standalone", "standalone"),
])
def test_workload_of_strips_controller_suffixes(pod, workload):
    assert workload_of(pod) == workload


class FakeProm:
    """Answers each query by the metric name it mentions."""

    base_url = "http://fake"

    def __init__(self, answers):
        self.answers = answers
        self.queries = []

    def query_range(self, query, start, end, step):
        self.queries.append(query)
        for needle, result in self.answers.items():
            if needle in query:
                return result
        return []


def m(pod, container, **extra):
    return {"namespace": "shop", "pod": pod, "container": container, **extra}


def test_record_groups_replicas_under_one_container():
    prom = FakeProm({
        "container_cpu_usage_seconds_total{": [
            {"metric": m("web-abc12345-aaaaa", "server"), "values": [[0, "0.1"], [15, "0.2"]]},
            {"metric": m("web-abc12345-bbbbb", "server"), "values": [[0, "0.3"], [15, "NaN"]]},
        ],
        "container_memory_working_set_bytes": [
            {"metric": m("web-abc12345-aaaaa", "server"), "values": [[0, "1000"], [15, "2000"]]},
        ],
        "kube_pod_container_resource_requests": [
            {"metric": m("web-abc12345-aaaaa", "server", resource="cpu"), "values": [[0, "0.1"]]},
            {"metric": m("web-abc12345-aaaaa", "server", resource="memory"), "values": [[0, "64"]]},
            {"metric": m("web-abc12345-aaaaa", "server", resource="ephemeral_storage"), "values": [[0, "1"]]},
        ],
    })
    rec = record(prom, "shop", 1800, end=10_000)
    assert rec["start"] == 8200 and rec["end"] == 10_000
    assert [c["workload"] for c in rec["containers"]] == ["web"]
    pods = rec["containers"][0]["pods"]
    assert [p["pod"] for p in pods] == ["web-abc12345-aaaaa", "web-abc12345-bbbbb"]
    assert pods[0]["requests"] == {"cpu": 0.1, "memory": 64.0}
    assert pods[1]["cpu"] == [[0.0, 0.3]]  # NaN dropped
    assert all('namespace="shop"' in q for q in prom.queries)


def test_parse_duration():
    assert parse_duration("30m") == 1800
    assert parse_duration("2h") == 7200
    assert parse_duration("90") == 90


def test_recommend_cli_writes_json(tmp_path, capsys):
    from helpers import MI, container, pod, recording
    src = tmp_path / "rec.json"
    src.write_text(json.dumps(recording(container(pod("p", [0.02] * 120, [64 * MI] * 120)))))
    out = tmp_path / "out" / "recs.json"
    assert main(["recommend", "--from-recording", str(src), "--out", str(out)]) == 0
    data = json.loads(out.read_text())
    assert data["recommendations"][0]["recommended"]["requests"] == {"cpu": "20m", "memory": "72Mi"}
    assert "svc/server" in capsys.readouterr().out
