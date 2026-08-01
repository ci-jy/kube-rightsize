#!/usr/bin/env bash
# Delete the kind cluster created by up.sh.
set -euo pipefail
kind delete cluster --name "${CLUSTER:-rightsize}"
