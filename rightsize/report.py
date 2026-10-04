"""Static HTML report of a validation run: requested vs used vs recommended per container.

The page is one self-contained file (inline CSS, no scripts) so it can be
served from GitHub Pages or opened straight from a CI artifact. It is built
from the validation results, plus the recommendations they were made from;
``validate`` keeps a copy of those in the results file, and older results
can be paired with a recommendations file instead.

Nothing time-dependent goes into the page, so the same run always renders to
the same bytes.
"""

from html import escape

from .units import MI, parse_cpu, parse_memory

_CSS = """
body { font: 15px/1.45 system-ui, -apple-system, "Segoe UI", sans-serif; color: #1d232a; margin: 0; background: #f6f7f9; }
main { max-width: 1120px; margin: 0 auto; padding: 28px 24px 48px; }
h1 { font-size: 24px; margin: 0 0 4px; }
h2 { font-size: 18px; margin: 32px 0 10px; }
p.sub { color: #57606a; margin: 0 0 20px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 12px; }
.card { background: #fff; border: 1px solid #d8dee4; border-radius: 8px; padding: 14px 16px; }
.card .label { color: #57606a; font-size: 13px; }
.card .value { font-size: 22px; font-weight: 600; margin-top: 2px; }
.card .note { color: #57606a; font-size: 13px; }
table { border-collapse: collapse; width: 100%; background: #fff; border: 1px solid #d8dee4; border-radius: 8px; }
th, td { text-align: left; padding: 7px 10px; border-bottom: 1px solid #eaeef2; vertical-align: top; }
th { font-size: 13px; color: #57606a; font-weight: 600; background: #f6f8fa; }
td.num { text-align: right; font-variant-numeric: tabular-nums; white-space: nowrap; }
.pass { color: #1a7f37; font-weight: 600; }
.fail { color: #cf222e; font-weight: 600; }
.bars { width: 230px; }
.bar { display: flex; align-items: center; gap: 6px; font-size: 12px; color: #57606a; height: 15px; }
.bar span.fill { display: inline-block; height: 9px; border-radius: 2px; min-width: 1px; }
.req { background: #afb8c1; } .used { background: #e8a33d; } .rec { background: #2f81f7; }
.legend span { display: inline-flex; align-items: center; gap: 5px; margin-right: 14px; font-size: 13px; color: #57606a; }
.legend i { display: inline-block; width: 12px; height: 9px; border-radius: 2px; }
.flags { font-size: 12px; color: #9a6700; }
details { margin-top: 3px; font-size: 12px; color: #57606a; }
"""


def _cores(q):
    return parse_cpu(q) * 1000 if q else None


def _mib(q):
    return parse_memory(q) / MI if q else None


def _fmt(v, unit, digits=0):
    if v is None:
        return "-"
    return f"{v:,.{digits}f}{unit}"


def container_rows(run, recommendations):
    """One row per recommended container, joining recorded and replayed usage."""
    replay = (run.get("replay") or {}).get("usage") or {}
    baseline = (run.get("baseline") or {}).get("usage") or {}
    rows = []
    for r in recommendations.get("recommendations", []):
        key = f"{r['workload']}/{r['container']}"
        obs = r.get("observed") or {}
        rows.append({
            "key": key,
            "replicas": r.get("replicas", 1),
            "confidence": r.get("confidence"),
            "flags": r.get("flags", []),
            "reasons": r.get("reasons", []),
            "cpu": {
                "requested": _cores(r["current"]["requests"].get("cpu")),
                "used": obs.get("cpu_p95_millicores"),
                "replay": replay.get(key, {}).get("cpu_millicores"),
                "baseline": baseline.get(key, {}).get("cpu_millicores"),
                "recommended": _cores(r["recommended"]["requests"].get("cpu")),
            },
            "memory": {
                "requested": _mib(r["current"]["requests"].get("memory")),
                "used": obs.get("memory_p99_mib"),
                "replay": replay.get(key, {}).get("memory_mib"),
                "baseline": baseline.get(key, {}).get("memory_mib"),
                "recommended": _mib(r["recommended"]["requests"].get("memory")),
                "limit": _mib(r["recommended"]["limits"].get("memory")),
            },
        })
    return rows


def totals(rows):
    """Requested and recommended capacity summed over containers with both, weighted by replicas."""
    out = {}
    for res in ("cpu", "memory"):
        req = rec = 0.0
        for row in rows:
            v = row[res]
            if v["requested"] is None or v["recommended"] is None:
                continue
            n = max(row["replicas"] or 1, 1)
            req += v["requested"] * n
            rec += v["recommended"] * n
        out[res] = {
            "requested": round(req, 1),
            "recommended": round(rec, 1),
            "freed": round(req - rec, 1),
            "freed_percent": round(100 * (req - rec) / req, 1) if req else 0.0,
        }
    return out


def _bars(values, unit):
    known = [v for v in (values["requested"], values["used"], values["recommended"]) if v is not None]
    top = max(known) if known else 0.0
    out = []
    for cls, label, v in (("req", "requested", values["requested"]), ("used", "used", values["used"]),
                          ("rec", "recommended", values["recommended"])):
        width = 0 if v is None or not top else max(1, round(150 * v / top))
        out.append(f'<div class="bar" title="{label}"><span class="fill {cls}" style="width:{width}px"></span>'
                   f"{escape(_fmt(v, unit, 1 if v is not None and v < 10 else 0))}</div>")
    return "".join(out)


def _window_stats(w):
    restarts = w.get("restarts") or {}
    throttle = w.get("throttling") or {}
    worst = max(throttle.items(), key=lambda kv: kv[1]) if throttle else None
    return {
        "requests": w.get("requests"),
        "failures": w.get("failures"),
        "restarts": sum(v.get("restarts", 0) for v in restarts.values()),
        "oom": sum(v.get("oom_kills", 0) for v in restarts.values()),
        "throttle": f"{worst[1]:.1%} ({worst[0]})" if worst else "-",
    }


def render(run, recommendations=None):
    """The report as an HTML string."""
    recommendations = recommendations or run.get("recommendations")
    if not recommendations:
        raise ValueError("the run has no recommendations attached; pass the recommendations file it was made from")
    rows = container_rows(run, recommendations)
    tot = totals(rows)
    passed = run.get("passed")
    window = run.get("window_seconds") or 0
    ns = run.get("namespace") or recommendations.get("source", {}).get("namespace") or "?"
    verdict = '<span class="pass">passed</span>' if passed else '<span class="fail">failed</span>'

    h = ['<!doctype html><html lang="en"><head><meta charset="utf-8">',
         '<meta name="viewport" content="width=device-width, initial-scale=1">',
         f"<title>kube-rightsize report: {escape(ns)}</title><style>{_CSS}</style></head><body><main>",
         f"<h1>Right-sizing report for <code>{escape(ns)}</code></h1>",
         f'<p class="sub">{len(rows)} containers. Validation {verdict}: '
         f"the recommended requests were applied and load was replayed for {window / 60:g} minutes "
         "before and after.</p>"]

    cpu, mem = tot["cpu"], tot["memory"]
    h.append('<div class="cards">')
    for label, value, note in (
        ("CPU requested", f"{cpu['requested']:,.0f}m &rarr; {cpu['recommended']:,.0f}m",
         f"{cpu['freed']:,.0f}m freed ({cpu['freed_percent']:g}%)"),
        ("Memory requested", f"{mem['requested']:,.0f}Mi &rarr; {mem['recommended']:,.0f}Mi",
         f"{mem['freed']:,.0f}Mi freed ({mem['freed_percent']:g}%)"),
        ("Gates", f"{sum(g['passed'] for g in run.get('gates', []))} / {len(run.get('gates', []))} passed",
         ", ".join(g["gate"] for g in run.get("gates", []) if not g["passed"]) or "nothing failed"),
    ):
        h.append(f'<div class="card"><div class="label">{label}</div><div class="value">{value}</div>'
                 f'<div class="note">{escape(note)}</div></div>')
    h.append("</div>")

    h.append("<h2>Per container</h2>")
    h.append('<p class="legend"><span><i class="req"></i>requested now</span>'
             '<span><i class="used"></i>used (CPU P95, memory P99 of the recording)</span>'
             '<span><i class="rec"></i>recommended request</span></p>')
    h.append("<table><tr><th>Container</th><th>CPU</th><th>CPU in replay</th>"
             "<th>Memory</th><th>Memory peak in replay</th><th>Confidence</th></tr>")
    for row in rows:
        c, m = row["cpu"], row["memory"]
        flags = f'<div class="flags">{escape(", ".join(row["flags"]))}</div>' if row["flags"] else ""
        reasons = "".join(f"<li>{escape(x)}</li>" for x in row["reasons"])
        reasons = f"<details><summary>why</summary><ul>{reasons}</ul></details>" if reasons else ""
        h.append(
            f"<tr><td><code>{escape(row['key'])}</code>{flags}{reasons}</td>"
            f'<td class="bars">{_bars(c, "m")}</td>'
            f'<td class="num">{_fmt(c["replay"], "m", 1)}</td>'
            f'<td class="bars">{_bars(m, "Mi")}</td>'
            f'<td class="num">{_fmt(m["replay"], "Mi", 1)}<br><small>limit {_fmt(m["limit"], "Mi")}</small></td>'
            f"<td>{escape(row['confidence'] or '-')}</td></tr>"
        )
    h.append("</table>")

    h.append("<h2>Gates</h2><table><tr><th>Gate</th><th>Result</th><th>Detail</th></tr>")
    for g in run.get("gates", []):
        res = '<span class="pass">pass</span>' if g["passed"] else '<span class="fail">fail</span>'
        h.append(f"<tr><td>{escape(g['gate'])}</td><td>{res}</td><td>{escape(g['detail'])}</td></tr>")
    h.append("</table>")

    b, r = _window_stats(run.get("baseline") or {}), _window_stats(run.get("replay") or {})
    h.append("<h2>Load windows</h2><table><tr><th></th><th>Before patching</th><th>After patching</th></tr>")
    for label, key in (("Requests sent", "requests"), ("Failed requests", "failures"),
                       ("Container restarts", "restarts"), ("OOM kills", "oom"), ("Worst throttled share of CFS periods", "throttle")):
        h.append(f'<tr><td>{label}</td><td class="num">{escape(str(b[key]))}</td>'
                 f'<td class="num">{escape(str(r[key]))}</td></tr>')
    h.append("</table>")
    h.append('<p class="sub" style="margin-top:20px">Generated by <code>rightsize report</code>. '
             "Recommendations: CPU P95, memory P99 plus headroom, raised for OOM-killed or limit-capped "
             "containers.</p>")
    h.append("</main></body></html>\n")
    return "\n".join(h)
