# CI

Two workflows live in `.github/workflows/`.

## ci.yml

Runs on every push to `main` and every pull request, in three jobs:

- `test`: installs the package with its test extras, runs `pytest` and `recommend` on the committed recording. Fully offline.
- `actionlint`: lints the workflow files themselves with [actionlint](https://github.com/rhysd/actionlint).
- `image`: builds the CLI image from the `Dockerfile` and runs `recommend` inside it against the committed recording.

## rightsize.yml

The slow one. It runs on pull requests that change `scripts/` (the kind config, Prometheus values and the scripts that apply the Online Boutique manifests), `rightsize/`, `recordings/` or the workflow itself, and can be started by hand with `workflow_dispatch`.

It installs kind and Helm and runs `scripts/validate.sh` with `RECORD=10m`, so the recommendations come from usage recorded in that same job, not from the committed recording:

1. `up.sh --squeeze` builds the cluster (about 6 minutes).
2. After a 2-minute warm-up, 10 minutes of load are recorded.
3. `recommend` and `patch` run on that recording, and the patches are rendered as a Kustomize overlay over the live Deployments to check they merge.
4. `validate` runs a 5-minute baseline window, applies the patches and runs a 5-minute replay.

The job fails if any gate fails. Whatever happened, it renders `rightsize report` into `out/report/index.html`, writes the gate table into the job summary and uploads `out/` (run, recommendations, recording, patches and report) as the `rightsize-run` artifact. Expect about 30 minutes in total. GitHub's standard Linux runners have 16 GB, so the node gets `NODE_MEMORY=6g` there.

The page at [index.html](index.html) is the same report, rendered from the latest validated run on my machine.

## Running the same thing locally

```sh
RECORD=10m scripts/validate.sh
python3 -m rightsize report --run out/validate.json --out docs/index.html
```

To lint the workflows locally, download actionlint (its repository has a `download-actionlint.bash` script) and run `actionlint` from the repository root.
