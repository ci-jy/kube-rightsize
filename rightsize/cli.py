"""Command line: ``python -m rightsize record|recommend|patch|validate``."""

import argparse
import json
import os
import re
import sys

from .patch import build_patches, summary_line, write_patches
from .prom import Prometheus, PrometheusError
from .recommend import Policy, recommend
from .record import record
from .validate import Gates, Kubectl, KubectlError, validate

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text):
    m = _DURATION.match(text.strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (try 30m, 2h, 900s)")
    return float(m.group(1)) * _UNIT[m.group(2)]


def _write_json(path, data):
    if path == "-":
        json.dump(data, sys.stdout, indent=2)
        sys.stdout.write("\n")
        return
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w") as fh:
        json.dump(data, fh, indent=1)
        fh.write("\n")


def cmd_record(args):
    prom = Prometheus(args.prometheus)
    try:
        rec = record(prom, args.namespace, args.duration, step_s=args.step, end=args.end)
    except PrometheusError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if not rec["containers"]:
        print(f"error: no container series found in namespace {args.namespace!r}", file=sys.stderr)
        return 1
    _write_json(args.out, rec)
    pods = sum(len(c["pods"]) for c in rec["containers"])
    print(f"recorded {len(rec['containers'])} containers ({pods} pods) to {args.out}", file=sys.stderr)
    return 0


def _table(result):
    rows = [("WORKLOAD/CONTAINER", "CPU now", "CPU rec", "MEM now", "MEM rec", "CONF", "FLAGS")]
    for r in result["recommendations"]:
        cur, rec = r["current"]["requests"], r["recommended"]["requests"]
        rows.append((
            f"{r['workload']}/{r['container']}",
            cur["cpu"] or "-", rec["cpu"] or "-",
            cur["memory"] or "-", rec["memory"] or "-",
            r["confidence"], ",".join(r["flags"]) or "-",
        ))
    widths = [max(len(row[i]) for row in rows) for i in range(len(rows[0]))]
    return "\n".join("  ".join(c.ljust(w) for c, w in zip(row, widths)).rstrip() for row in rows)


def cmd_recommend(args):
    if args.from_recording:
        with open(args.from_recording) as fh:
            rec = json.load(fh)
    else:
        try:
            rec = record(Prometheus(args.prometheus), args.namespace, args.duration, step_s=args.step)
        except PrometheusError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
    policy = Policy(
        cpu_percentile=args.cpu_percentile,
        memory_percentile=args.memory_percentile,
        memory_headroom=args.memory_headroom,
    )
    result = recommend(rec, policy)
    _write_json(args.out, result)
    if not args.quiet:
        s = result["summary"]
        print(_table(result))
        print(
            f"\n{s['containers']} containers, {s['censored']} censored. "
            f"Requested CPU {s['cpu_requested_millicores']}m -> {s['cpu_recommended_millicores']}m "
            f"({s['cpu_freed_percent']}% freed); memory {s['memory_requested_mib']}Mi -> "
            f"{s['memory_recommended_mib']}Mi ({s['memory_freed_percent']}% freed)."
        )
        if args.out != "-":
            print(f"wrote {args.out}")
    return 0


def cmd_patch(args):
    with open(args.recommendations) as fh:
        recs = json.load(fh)
    plans = build_patches(recs, cpu_limits=args.cpu_limits, min_confidence=args.min_confidence)
    files, index = write_patches(plans, args.out, base=args.base, cpu_limits=args.cpu_limits)
    _write_json(os.path.join(args.out, "patches.json"), index)
    if not args.quiet:
        with open(os.path.join(args.out, "diff.txt")) as fh:
            print(fh.read())
    print(f"{summary_line(plans)}; wrote {len(files)} patch file(s) to {args.out}", file=sys.stderr)
    return 0


def cmd_validate(args):
    with open(os.path.join(args.patches, "patches.json")) as fh:
        index = json.load(fh)
    if args.namespace:
        index["workloads"] = [w for w in index["workloads"] if w["namespace"] in (None, args.namespace)]
    namespace = args.namespace or next((w["namespace"] for w in index["workloads"] if w["namespace"]), "default")
    gates = Gates(max_throttle=args.max_throttle, max_error_increase=args.max_error_increase,
                  min_requests=args.min_requests)

    def log(msg):
        print(msg, file=sys.stderr, flush=True)

    try:
        result = validate(Kubectl(namespace, args.context), Prometheus(args.prometheus), index, args.patches,
                          args.window, gates, loadgen=args.loadgenerator, log=log)
    except (KubectlError, PrometheusError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    if args.recommendations and os.path.exists(args.recommendations):
        # Kept with the run so the report needs nothing else to show requested vs used vs recommended.
        with open(args.recommendations) as fh:
            result["recommendations"] = json.load(fh)
    _write_json(args.out, result)
    for g in result["gates"]:
        print(f"{'PASS' if g['passed'] else 'FAIL'}  {g['gate']:<10}  {g['detail']}")
    print(f"\n{'passed' if result['passed'] else 'FAILED'}; wrote {args.out}")
    return 0 if result["passed"] else 1


def build_parser():
    p = argparse.ArgumentParser(prog="rightsize", description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)

    def source_opts(sp, required):
        sp.add_argument("--prometheus", default="http://127.0.0.1:29090", help="Prometheus base URL")
        sp.add_argument("--namespace", required=required, help="namespace to read")
        sp.add_argument("--duration", type=parse_duration, default=parse_duration("30m"),
                        help="how far back to read, e.g. 30m or 2h (default 30m)")
        sp.add_argument("--step", type=float, default=15.0, help="query resolution in seconds (default 15)")

    r = sub.add_parser("record", help="snapshot Prometheus series to a JSON recording")
    source_opts(r, required=True)
    r.add_argument("--end", type=float, help="unix time the window ends (default now)")
    r.add_argument("--out", required=True, help="recording file to write ('-' for stdout)")
    r.set_defaults(func=cmd_record)

    c = sub.add_parser("recommend", help="per-container requests/limits with reasons")
    c.add_argument("--from-recording", help="read a recording instead of querying Prometheus")
    source_opts(c, required=False)
    c.add_argument("--out", default="-", help="recommendations file ('-' for stdout, the default)")
    c.add_argument("--cpu-percentile", type=float, default=95.0)
    c.add_argument("--memory-percentile", type=float, default=99.0)
    c.add_argument("--memory-headroom", type=float, default=0.10)
    c.add_argument("-q", "--quiet", action="store_true", help="skip the summary table")
    c.set_defaults(func=cmd_recommend)

    pt = sub.add_parser("patch", help="write Kustomize patches and a diff from recommendations")
    pt.add_argument("--recommendations", default="out/recommendations.json", help="output of recommend")
    pt.add_argument("--out", default="out/patches", help="directory for the patches (default out/patches)")
    pt.add_argument("--base", help="kustomization directory to list under resources:")
    pt.add_argument("--cpu-limits", choices=("keep", "drop"), default="keep",
                    help="keep existing CPU limits (default) or remove them as recommended")
    pt.add_argument("--min-confidence", choices=("none", "low", "medium", "high"), default="low",
                    help="skip recommendations below this confidence (default low)")
    pt.add_argument("-q", "--quiet", action="store_true", help="do not print the diff")
    pt.set_defaults(func=cmd_patch)

    v = sub.add_parser("validate", help="apply patches, replay load and check OOM/throttling/error gates")
    v.add_argument("--patches", default="out/patches", help="directory written by patch")
    v.add_argument("--namespace", help="namespace to patch (default: the one in the patches)")
    v.add_argument("--context", help="kubectl context (default: current)")
    v.add_argument("--prometheus", default="http://127.0.0.1:29090", help="Prometheus base URL")
    v.add_argument("--window", type=parse_duration, default=parse_duration("5m"),
                   help="length of the baseline and replay load windows (default 5m)")
    v.add_argument("--loadgenerator", default="loadgenerator", help="load generator deployment")
    v.add_argument("--max-throttle", type=float, default=Gates.max_throttle,
                   help="highest share of throttled CFS periods allowed (default 0.25)")
    v.add_argument("--max-error-increase", type=float, default=Gates.max_error_increase,
                   help="largest allowed rise in error rate, absolute (default 0.01)")
    v.add_argument("--min-requests", type=int, default=Gates.min_requests)
    v.add_argument("--recommendations", default="out/recommendations.json",
                   help="recommendations the patches came from, copied into the results if present")
    v.add_argument("--out", default="out/validate.json", help="results file")
    v.set_defaults(func=cmd_validate)
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "recommend" and not args.from_recording and not args.namespace:
        build_parser().error("recommend needs --from-recording or --namespace")
    if args.command == "recommend" and args.out == "-":
        args.quiet = True
    return args.func(args)
