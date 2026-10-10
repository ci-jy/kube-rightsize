# kube-rightsize

`rightsize` is a small Python CLI that reads how much CPU and memory each container in a namespace actually used, from Prometheus, and suggests requests and a memory limit with a written reason for every number. On Google's Online Boutique demo it cut requested CPU across 12 containers from 1570m to 140m and memory from 1180Mi to 692Mi, and a load replay with all 12 patches applied had no failed requests and no OOM kills. I built it because requests on shared clusters tend to get copied from the service next door and then doubled after the first OOM kill, and I wanted them based on measurements.

![The report for the latest Online Boutique run](docs/report.png)

That page ([docs/index.html](docs/index.html)) comes out of the validation step, which applies the patches to a kind cluster, replays load and fails if anything gets OOM-killed, throttled or starts returning errors.

## The one service it made bigger

Most of the savings are dull: services asking for 100m and using a few millicores. The interesting one is `recommendationservice`. `scripts/up.sh --squeeze` gives it a 40Mi memory limit, a bit under what it needs, so the recording has real OOM kills in it. Its P99 of 41.2Mi is just the limit plus noise, and a plain "P99 plus 10%" rule would have kept it crash-looping. Instead it gets flagged and raised:

```json
"reasons": [
  "Memory P99 is 41.2Mi over 132 samples; +10% headroom gives 45.3Mi.",
  "Memory is censored: OOM-killed 2 time(s), so the real peak is unknown. Raised to 140Mi (old limit +20% or +100Mi, whichever is larger) instead of trusting the percentile.",
  "CPU P95 is 8.9m over 108 samples.",
  "CPU raised to the 10m floor."
]
```

The unpatched baseline had 4 OOM kills and, which surprised me, no failed requests at all. The frontend seems to just render the page without recommendations when that call fails, so only the OOM gate noticed. I also gave adservice a deliberately undersized limit to check the gates can fail. They did. Per-container numbers are in [docs/boutique-results.md](docs/boutique-results.md) and the runs are written up in [docs/validation.md](docs/validation.md).

## How it works

`rightsize record` runs range queries against the Prometheus HTTP API and saves the raw per-pod series (CPU, memory, throttling, restarts, OOM reasons, current requests and limits) as JSON. Everything after that can be recomputed offline, which is how the tests work.

`recommend` pools the replicas of each container and applies a fairly plain rule: P95 of CPU, P99 of memory plus headroom, a small floor, a memory limit a bit above the request, no CPU limit. The hard part was censored data. If a container was OOM-killed or sat at its limit, the percentile you measure is the limit, not the demand, and sizing from it shrinks exactly the service that is starving. Those containers get raised the way the Vertical Pod Autoscaler reacts to an OOM kill. Restarts caused a second problem: the CPU burst right after a container starts looks like throttling, so those samples are skipped. Full rules are in [docs/rules.md](docs/rules.md).

`patch` writes strategic-merge patches that only touch `resources`. `validate` compares a baseline load window with a replay after the rollout. `report` turns that run into a static page with no JavaScript.

Getting kind running on my VM took longer than the tool itself. runc failed a sysctl permission check there, so `validate.sh` can fall back to a private Docker daemon running `crun`.

## Trying it, and what's still weak

You need Python 3.10 or newer and nothing else at runtime. The committed recording is 30 minutes of Online Boutique with its load generator running:

```sh
python3 -m venv .venv && . .venv/bin/activate
pip install -e '.[test]'
python3 -m pytest    # offline, against the committed recording
python3 -m rightsize recommend --from-recording recordings/boutique-30m.json --out out/recommendations.json
python3 -m rightsize patch    # one patch per Deployment in out/patches/, plus diff.txt
```

Recording your own data or validating patches also needs Docker, kind, kubectl and Helm 3:

```sh
scripts/up.sh --squeeze                       # kind + Prometheus + Online Boutique
WARMUP=120 DURATION=30m scripts/record.sh recordings/my-run.json
scripts/validate.sh                           # applies the patches, replays load, exits 1 if a gate fails
python3 -m rightsize report --run out/validate.json --out docs/index.html
scripts/down.sh
```

Flags, file formats and the Docker image: [docs/cli.md](docs/cli.md). Cluster scripts and VM workarounds: [docs/cluster-setup.md](docs/cluster-setup.md). The GitHub Actions workflows: [docs/ci.md](docs/ci.md).

Things I'd fix next:

- A 5-minute replay at light load is a smoke test, not proof. Memory that creeps up over hours won't show up, and 10 simulated users is nothing like a real peak.
- OOM detection relies on kube-state-metrics' `last_terminated_reason`. In the latest run the unpatched recommendationservice restarted twice but the OOM gate counted zero kills, and I haven't pinned down why. Gating on restarts as well would catch it.
- Workloads are found by stripping the hash off pod names rather than following owner references, so a bare pod whose name ends in something like `-12345` gets grouped wrongly.
- Patches are Kustomize only. Online Boutique also ships a Helm chart, so a values file would be a natural next step.

Project period: 2026-08-01 to 2026-10-13.
