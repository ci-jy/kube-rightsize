import json
import os
import re

import pytest

from rightsize.cli import main
from rightsize.recommend import recommend
from rightsize.report import container_rows, render, totals

ROOT = os.path.dirname(os.path.dirname(__file__))
RECORDING = os.path.join(ROOT, "recordings", "boutique-30m.json")
RUN = os.path.join(ROOT, "results", "boutique-validation.json")


def rec(workload="svc", container="server", cur=("200m", "128Mi"), new=("20m", "64Mi"), replicas=1, flags=()):
    return {
        "workload": workload, "container": container, "replicas": replicas, "confidence": "medium",
        "flags": list(flags), "reasons": [f"{workload} reason"],
        "current": {"requests": {"cpu": cur[0], "memory": cur[1]}, "limits": {"cpu": None, "memory": None}},
        "recommended": {"requests": {"cpu": new[0], "memory": new[1]}, "limits": {"cpu": None, "memory": "80Mi"}},
        "observed": {"cpu_p95_millicores": 12.5, "memory_p99_mib": 50.0},
    }


def run(passed=True, recommendations=None, replay_usage=None):
    gates = [{"gate": "rollout", "passed": True, "detail": "every patched workload rolled out"},
             {"gate": "oom", "passed": passed, "detail": "ok" if passed else "OOM-killed during replay: svc/server x1"}]
    r = {
        "namespace": "shop", "window_seconds": 300.0, "passed": passed, "gates": gates,
        "baseline": {"requests": 600, "failures": 0, "restarts": {"svc/server": {"restarts": 2, "oom_kills": 2}},
                     "throttling": {"svc/server": 0.03}},
        "replay": {"requests": 590, "failures": 1, "restarts": {}, "throttling": {},
                   "usage": replay_usage or {}},
    }
    if recommendations is not None:
        r["recommendations"] = recommendations
    return r


@pytest.fixture(scope="module")
def boutique():
    with open(RECORDING) as fh:
        recs = recommend(json.load(fh))
    with open(RUN) as fh:
        return json.load(fh), recs


def test_totals_weight_by_replicas_and_skip_missing_requests():
    rows = container_rows(run(), {"recommendations": [
        rec("a", replicas=3),
        rec("b", cur=(None, "128Mi")),
    ]})
    t = totals(rows)
    assert t["cpu"] == {"requested": 600.0, "recommended": 60.0, "freed": 540.0, "freed_percent": 90.0}
    assert t["memory"]["requested"] == 3 * 128 + 128
    assert t["memory"]["freed_percent"] == 50.0


def test_replay_usage_is_joined_by_workload_and_container():
    rows = container_rows(run(replay_usage={"svc/server": {"cpu_millicores": 7.25, "memory_mib": 41.0}}),
                          {"recommendations": [rec()]})
    assert rows[0]["cpu"]["replay"] == 7.25 and rows[0]["memory"]["replay"] == 41.0
    assert rows[0]["cpu"]["used"] == 12.5 and rows[0]["memory"]["limit"] == 80.0


def test_report_uses_recommendations_kept_in_the_run():
    page = render(run(recommendations={"recommendations": [rec()]}))
    assert "<code>svc/server</code>" in page
    assert "200m &rarr; 20m" in page and "180m freed (90%)" in page


def test_run_without_recommendations_needs_a_file():
    with pytest.raises(ValueError):
        render(run())


def test_failed_gate_is_shown_as_failed():
    page = render(run(passed=False), {"recommendations": [rec()]})
    assert "Validation <span class=\"fail\">failed</span>" in page
    assert "1 / 2 passed" in page and "svc/server x1" in page


def test_load_windows_count_restarts_and_oom_kills():
    page = render(run(), {"recommendations": [rec()]})
    assert re.search(r"Container restarts</td><td class=\"num\">2</td>\s*<td class=\"num\">0</td>", page)


def test_names_and_reasons_are_escaped():
    page = render(run(), {"recommendations": [rec("<script>x</script>")]})
    assert "<script>" not in page and "&lt;script&gt;" in page


def test_boutique_report_shows_every_container_and_the_freed_capacity(boutique):
    validation, recs = boutique
    page = render(validation, recs)
    for r in recs["recommendations"]:
        assert f"<code>{r['workload']}/{r['container']}</code>" in page
    s = recs["summary"]
    assert f"{s['cpu_requested_millicores']:,}m &rarr; {s['cpu_recommended_millicores']:,}m" in page
    assert f"({s['memory_freed_percent']:g}%)" in page
    assert "4 / 4 passed" in page
    # Before/after table: the squeezed service's OOM kills are gone after patching.
    assert re.search(r"OOM kills</td><td class=\"num\">4</td>\s*<td class=\"num\">0</td>", page)


def test_report_is_the_same_for_the_same_run(boutique):
    validation, recs = boutique
    assert render(validation, recs) == render(validation, recs)


def test_report_command_writes_html(tmp_path, boutique):
    validation, recs = boutique
    path = tmp_path / "validate.json"
    path.write_text(json.dumps(dict(validation, recommendations=recs)))
    out = tmp_path / "site" / "index.html"
    assert main(["report", "--run", str(path), "--out", str(out)]) == 0
    assert out.read_text().startswith("<!doctype html>")


def test_report_command_fails_cleanly_without_recommendations(tmp_path):
    path = tmp_path / "validate.json"
    path.write_text(json.dumps(run()))
    assert main(["report", "--run", str(path), "--out", str(tmp_path / "x.html")]) == 2


def test_docs_page_is_the_report_of_the_committed_ci_style_run():
    # results/boutique-validate.json is the run docs/index.html was rendered from; it carries its own
    # recommendations and per-window usage.
    with open(os.path.join(ROOT, "results", "boutique-validate.json")) as fh:
        validation = json.load(fh)
    assert validation["passed"] and validation["replay"]["restarts"] == {}
    assert set(validation["replay"]["usage"]) == {
        f"{r['workload']}/{r['container']}" for r in validation["recommendations"]["recommendations"]
    }
    with open(os.path.join(ROOT, "docs", "index.html")) as fh:
        assert fh.read() == render(validation)
