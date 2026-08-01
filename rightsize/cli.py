"""Command line: ``python -m rightsize record|recommend``."""

import argparse
import json
import os
import re
import sys

from .prom import Prometheus, PrometheusError
from .recommend import Policy, recommend
from .record import record

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
    return p


def main(argv=None):
    args = build_parser().parse_args(argv)
    if args.command == "recommend" and not args.from_recording and not args.namespace:
        build_parser().error("recommend needs --from-recording or --namespace")
    if args.command == "recommend" and args.out == "-":
        args.quiet = True
    return args.func(args)
